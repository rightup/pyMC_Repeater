"""End-to-end: repeater ACL role -> the bytes a MeshCore client actually reads.

The login reply is 13 bytes; byte 6 is the legacy is_admin flag and byte 7 is
the ACL permissions byte. A stock companion forwards both verbatim into
PUSH_CODE_LOGIN_SUCCESS, and modern clients derive the displayed role from
byte 7 with ``(perms & 3) == 3``. openhop_repeater issue #388 was exactly this
pair disagreeing: byte 6 said admin while byte 7 decoded as read-write.

These tests drive the real openhop_core LoginServerHandler with the real
repeater ACL as its authenticate callback, then decrypt the reply and assert
the wire bytes.
"""

import struct
import time

import pytest
from openhop_core.node.handlers.login_server import LoginServerHandler
from openhop_core.protocol import CryptoUtils, Identity, LocalIdentity, Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_RESPONSE,
    ROUTE_TYPE_FLOOD,
)

from repeater.handler_helpers.acl import (
    ACL,
    PERM_ACL_ADMIN,
    PERM_ACL_GUEST,
    PERM_ACL_READ_WRITE,
)

ROOM_CFG = {
    "type": "room_server",
    "settings": {"admin_password": "room-admin", "guest_password": "room-guest"},
}


def _build_login_packet(
    client: LocalIdentity, server: LocalIdentity, password: str, *, room: bool = False
) -> Packet:
    """Build an ANON_REQ login packet the way a client does.

    Repeater format is ``timestamp(4) + password + NUL``; a room server inserts
    a 4-byte ``sync_since`` before the password (firmware reads it at data[8]).
    """
    server_id = Identity(server.get_public_key())
    shared_secret = server_id.calc_shared_secret(client.get_private_key())
    plaintext = struct.pack("<I", int(time.time()))
    if room:
        plaintext += struct.pack("<I", 0)  # sync_since
    plaintext += password.encode("utf-8") + b"\x00"
    encrypted = CryptoUtils.encrypt_then_mac(shared_secret[:16], shared_secret, plaintext)

    payload = bytes([server.get_public_key()[0]]) + client.get_public_key() + encrypted
    pkt = Packet()
    pkt.header = (PAYLOAD_TYPE_ANON_REQ << 2) | ROUTE_TYPE_FLOOD
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray()
    pkt.path_len = 0
    return pkt


async def _login_reply(acl: ACL, password: str, *, room_config: dict = None) -> bytes:
    """Run a real login through the ACL + handler and return the 13 reply bytes."""
    server = LocalIdentity()
    client = LocalIdentity()

    def authenticate(client_identity, shared_secret, password_, timestamp, sync_since=None):
        return acl.authenticate_client(
            client_identity=client_identity,
            shared_secret=shared_secret,
            password=password_,
            timestamp=timestamp,
            sync_since=sync_since,
            target_identity_name="node",
            target_identity_config=room_config or {},
        )

    sent = []
    handler = LoginServerHandler(
        local_identity=server,
        log_fn=lambda _msg: None,
        authenticate_callback=authenticate,
        is_room_server=bool(room_config),
    )
    handler.set_send_packet_callback(lambda pkt, *a, **kw: sent.append(pkt))

    await handler(_build_login_packet(client, server, password, room=bool(room_config)))
    assert sent, "handler sent no login response"

    shared_secret = Identity(client.get_public_key()).calc_shared_secret(server.get_private_key())
    plaintext = CryptoUtils.mac_then_decrypt(
        shared_secret[:16], shared_secret, bytes(sent[0].payload[2:])
    )
    # PATH return envelope: path_len(1) + path(0) + extra_type(1) + reply(13)
    assert plaintext[0] == 0
    assert plaintext[1] & 0x0F == PAYLOAD_TYPE_RESPONSE
    return plaintext[2:15]


@pytest.mark.asyncio
async def test_repeater_admin_login_is_admin_on_the_wire():
    """The bug from #388: byte 7 must decode to ADMIN, not READ_WRITE."""
    acl = ACL(admin_password="rpt-admin", guest_password="rpt-guest")
    reply = await _login_reply(acl, "rpt-admin")

    assert reply[6] == 1  # legacy is_admin flag
    assert reply[7] == PERM_ACL_ADMIN  # ACL byte modern clients read
    assert reply[7] & 0x03 == 3  # what a stock client computes
    assert reply[7] != PERM_ACL_READ_WRITE  # the value we used to send


@pytest.mark.asyncio
async def test_repeater_guest_login_is_guest_on_the_wire():
    acl = ACL(admin_password="rpt-admin", guest_password="rpt-guest")
    reply = await _login_reply(acl, "rpt-guest")

    assert reply[6] == 0
    assert reply[7] == PERM_ACL_GUEST


@pytest.mark.asyncio
async def test_room_server_guest_login_is_read_write_on_the_wire():
    """Room guests post messages, so they must decode as READ_WRITE, not admin."""
    reply = await _login_reply(ACL(), "room-guest", room_config=ROOM_CFG)

    assert reply[6] == 0  # read-write is not admin
    assert reply[7] == PERM_ACL_READ_WRITE


@pytest.mark.asyncio
async def test_room_server_admin_login_is_admin_on_the_wire():
    reply = await _login_reply(ACL(), "room-admin", room_config=ROOM_CFG)

    assert reply[6] == 1
    assert reply[7] == PERM_ACL_ADMIN


@pytest.mark.asyncio
async def test_blank_password_read_only_login_is_guest_on_the_wire():
    reply = await _login_reply(ACL(allow_read_only=True), "")

    assert reply[6] == 0
    assert reply[7] == PERM_ACL_GUEST
