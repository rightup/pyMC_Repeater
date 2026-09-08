import asyncio
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.node.handlers.result import HandlerResult
from openhop_core.node.handlers.text import TXT_ACK_DELAY_MS
from openhop_core.protocol import Identity, LocalIdentity
from openhop_core.protocol.constants import PAYLOAD_TYPE_ACK
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.handler_helpers.acl import (
    ACL,
    PERM_ACL_ADMIN,
    PERM_ACL_READ_WRITE,
    ClientInfo,
)
from repeater.handler_helpers.path import PathHelper
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper
from repeater.handler_helpers.text import CORE_ACCEPTS_ACK_POLICY, TextHelper, _may_ack

# ---------------------------------------------------------------------------
# Real-crypto collision scaffolding (BUG-053 "try all local candidates").
#
# A one-byte destination hash can collide: a packet addressed to a remote node
# (or a forged packet) can share its first public-key byte with a local
# identity. These helpers build genuinely-encrypted packets so we can prove the
# repeater consumes only when a local identity actually decrypts, and otherwise
# forwards.
# ---------------------------------------------------------------------------

TXT_TYPE_CLI_DATA = 1  # no delivery ACK -> deterministic (no background ACK task)


def _distinct_identities(n=3):
    """Return `n` identities whose public keys start with distinct bytes."""
    ids, seen = [], set()
    while len(ids) < n:
        idn = LocalIdentity()
        first = idn.get_public_key()[0]
        if first in seen:
            continue
        seen.add(first)
        ids.append(idn)
    return ids


class _SendDest:
    """Minimal contact object accepted by PacketBuilder as a send destination."""

    def __init__(self, pubkey: bytes):
        self.public_key = pubkey.hex()
        self.out_path = []
        self.out_path_len = -1


def _force_dest_hash(packet, hash_byte: int):
    """Rewrite the on-air one-byte dest hash to simulate a prefix collision."""
    packet.payload = bytearray(packet.payload)
    packet.payload[0] = hash_byte
    return packet


def _acl_with_client(sender, local_identity=None):
    """ACL holding `sender` as an admin client.

    When `local_identity` is given, the real shared secret is precomputed —
    the protocol-request handler reads `client.shared_secret` directly.
    """
    acl = ACL()
    sender_pub = sender.get_public_key()
    client = ClientInfo(Identity(sender_pub), permissions=PERM_ACL_ADMIN)
    if local_identity is not None:
        client.shared_secret = Identity(sender_pub).calc_shared_secret(
            local_identity.get_private_key()
        )
    acl.clients[sender_pub] = client
    return acl


class _FakeId:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


class _FakeClient:
    def __init__(self, pubkey: bytes, shared_secret: bytes, permissions=0):
        self.id = _FakeId(pubkey)
        self.shared_secret = shared_secret
        self.permissions = permissions
        self.out_path = bytearray()
        self.out_path_len = -1


class _FakeACL:
    def __init__(self, clients):
        self._clients = list(clients)

    def get_all_clients(self):
        return self._clients


class _PathPacket:
    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)
        self.mark_do_not_retransmit = MagicMock()


class _ReqPacket:
    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)
        self.mark_do_not_retransmit = MagicMock()


@pytest.mark.asyncio
async def test_path_helper_updates_client_out_path_on_valid_decrypt():
    client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32)
    acl = _FakeACL([client])
    helper = PathHelper(acl_dict={0x11: acl})

    # Payload: dest(0x11), src(0x22), mac+data...
    packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")

    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=b"\x02\x99\x88\x01",
    ):
        handled = await helper.process_path_packet(packet)

    assert handled is True
    packet.mark_do_not_retransmit.assert_called_once_with()
    assert client.out_path_len == 2
    assert bytes(client.out_path) == b"\x99\x88"
    assert isinstance(client.last_activity, int)


@pytest.mark.asyncio
async def test_path_helper_registers_embedded_ack():
    """Firmware path returns embed the delivery ACK after the path
    (extra_type=PAYLOAD_TYPE_ACK + 4-byte CRC); it must reach ack_received_callback
    so local waiters (e.g. room server pushes) resolve."""
    client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32)
    acl = _FakeACL([client])
    ack_fn = AsyncMock()
    helper = PathHelper(acl_dict={0x11: acl}, ack_received_callback=ack_fn)

    packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")
    # path_len(2) + path + extra_type(PAYLOAD_TYPE_ACK=3) + crc(4, LE)
    decrypted = b"\x02\x99\x88" + bytes([0x03]) + bytes.fromhex("4dabaf95")
    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=decrypted,
    ):
        await helper.process_path_packet(packet)

    ack_fn.assert_awaited_once_with(0x95AFAB4D)
    assert client.out_path_len == 2  # path update still applied


@pytest.mark.asyncio
async def test_path_helper_handles_encoded_path_len_with_embedded_ack():
    """path_len in a path return is the ENCODED wire byte: with 3-byte hashes an
    empty path is 0x80, not 0x00. Reading it as a raw count (128) made the
    helper bail as 'truncated' before the embedded ACK was registered, so
    room-server pushes to same-instance companions timed out forever.
    Bytes below are a decrypted path return captured from a live mesh."""
    client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32)
    acl = _FakeACL([client])
    ack_fn = AsyncMock()
    helper = PathHelper(acl_dict={0x11: acl}, ack_received_callback=ack_fn)

    packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")
    # path_len 0x80 (3-byte hashes, 0 hops) + extra_type ACK + crc + AES padding
    decrypted = bytes.fromhex("80038d48208500000000000000000000")
    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=decrypted,
    ):
        await helper.process_path_packet(packet)

    ack_fn.assert_awaited_once_with(0x8520488D)
    assert client.out_path_len == 0x80  # encoded byte preserved
    assert bytes(client.out_path) == b""


@pytest.mark.asyncio
async def test_path_helper_ignores_non_ack_extra():
    client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32)
    acl = _FakeACL([client])
    ack_fn = AsyncMock()
    helper = PathHelper(acl_dict={0x11: acl}, ack_received_callback=ack_fn)

    packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")
    # extra_type 0x08 (PATH) instead of ACK: nothing to register
    decrypted = b"\x02\x99\x88" + bytes([0x08]) + b"\x01\x02\x03\x04"
    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=decrypted,
    ):
        await helper.process_path_packet(packet)

    ack_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_path_helper_returns_false_for_non_matching_or_invalid_inputs():
    client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32)
    acl = _FakeACL([client])
    helper = PathHelper(acl_dict={0x11: acl})

    assert await helper.process_path_packet(_PathPacket(payload=b"\x11")) is False
    assert await helper.process_path_packet(_PathPacket(payload=b"\x33\x22\xaa\xbb")) is False

    no_secret_client = _FakeClient(pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"")
    helper_no_secret = PathHelper(acl_dict={0x11: _FakeACL([no_secret_client])})
    assert (
        await helper_no_secret.process_path_packet(_PathPacket(payload=b"\x11\x22\xaa\xbb"))
        is False
    )

    with patch("openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt", return_value=None):
        assert await helper.process_path_packet(_PathPacket(payload=b"\x11\x22\xaa\xbb")) is False

    # A valid MAC with an invalid or truncated PATH envelope is not local
    # ownership; the forwarding engine must remain eligible to handle it.
    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=b"\x7f\x99\x88\x01",
    ):
        invalid_packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")
        assert await helper.process_path_packet(invalid_packet) is False
        invalid_packet.mark_do_not_retransmit.assert_not_called()

    with patch(
        "openhop_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=b"\x02\x99",
    ):
        truncated_packet = _PathPacket(payload=b"\x11\x22\xaa\xbb\xcc")
        assert await helper.process_path_packet(truncated_packet) is False
        truncated_packet.mark_do_not_retransmit.assert_not_called()


@pytest.mark.asyncio
async def test_protocol_request_process_routes_and_marks_no_retransmit():
    injector = AsyncMock(return_value=True)
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=injector)

    assert await helper.process_request_packet(_ReqPacket(payload=b"\x01")) is False

    pkt_unknown = _ReqPacket(payload=b"\x99\x01")
    assert await helper.process_request_packet(pkt_unknown) is False

    dest = 0x42
    response_packet = object()

    async def _core_handler(_packet):
        return HandlerResult.consumed(response_packet)

    helper.handlers[dest] = {"handler": _core_handler}
    pkt = _ReqPacket(payload=bytes([dest, 0x01, 0x02]))

    with patch("repeater.handler_helpers.protocol_request.asyncio.sleep", new_callable=AsyncMock):
        handled = await helper.process_request_packet(pkt)

    assert handled is True
    pkt.mark_do_not_retransmit.assert_called_once()
    injector.assert_awaited_once_with(response_packet, wait_for_ack=False)


@pytest.mark.asyncio
async def test_protocol_request_forwards_on_dest_hash_collision():
    """A REQ whose dest prefix collides with ours but does not decrypt for a
    local client must not be consumed, so the engine can still forward it."""
    injector = AsyncMock(return_value=True)
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=injector)

    dest = 0x42

    async def _core_handler(_packet):
        # MAC failed for every same-hash candidate -> not for us.
        return HandlerResult.not_for_us()

    helper.handlers[dest] = {"handler": _core_handler}
    pkt = _ReqPacket(payload=bytes([dest, 0x01, 0x02]))

    handled = await helper.process_request_packet(pkt)

    assert handled is False
    pkt.mark_do_not_retransmit.assert_not_called()
    injector.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_request_consumes_authenticated_without_response():
    """A REQ that authenticates for us but yields no reply is still consumed."""
    injector = AsyncMock(return_value=True)
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=injector)

    dest = 0x42

    async def _core_handler(_packet):
        return HandlerResult.consumed()

    helper.handlers[dest] = {"handler": _core_handler}
    pkt = _ReqPacket(payload=bytes([dest, 0x01, 0x02]))

    handled = await helper.process_request_packet(pkt)

    assert handled is True
    pkt.mark_do_not_retransmit.assert_called_once()
    injector.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_request_process_exception_returns_false():
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=AsyncMock())

    async def _boom(_packet):
        raise RuntimeError("oops")

    helper.handlers[0x33] = {"handler": _boom}
    pkt = _ReqPacket(payload=b"\x33\x01")

    assert await helper.process_request_packet(pkt) is False


def test_protocol_request_handle_get_status_builds_56_byte_payload():
    engine = SimpleNamespace(
        start_time=time.time() - 120,
        rx_count=7,
        forwarded_count=5,
        sent_flood_count=2,
        sent_direct_count=3,
        recv_flood_count=4,
        recv_direct_count=1,
        direct_dup_count=6,
        flood_dup_count=8,
        airtime_mgr=SimpleNamespace(total_airtime_ms=9300, total_rx_airtime_ms=4200),
    )
    radio = SimpleNamespace(
        get_noise_floor=lambda: -110,
        get_last_rssi=lambda: -70,
        get_last_snr=lambda: 2.5,
        crc_error_count=11,
    )
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(),
        packet_injector=AsyncMock(),
        radio=radio,
        engine=engine,
    )

    data = helper._handle_get_status(client=None, timestamp=0, req_data=b"")

    assert isinstance(data, (bytes, bytearray))
    assert len(data) == 56


def test_protocol_request_access_list_admin_and_reserved_rules():
    admin = SimpleNamespace(is_admin=lambda: True)
    not_admin = SimpleNamespace(is_admin=lambda: False)
    c1 = _FakeClient(pubkey=b"A" * 32, shared_secret=b"k" * 32, permissions=0x02)
    c2 = _FakeClient(pubkey=b"B" * 32, shared_secret=b"k" * 32, permissions=0x00)
    acl = _FakeACL([c1, c2])
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=AsyncMock())

    assert helper._handle_get_access_list(not_admin, 0, b"\x00\x00", acl) is None
    assert helper._handle_get_access_list(admin, 0, b"\x01\x00", acl) is None

    out = helper._handle_get_access_list(admin, 0, b"\x00\x00", acl)
    assert isinstance(out, bytes)
    # One active entry only: 6-byte key prefix + 1-byte perms
    assert len(out) == 7
    assert out[-1] == 0x02


def test_protocol_request_get_neighbours_sort_and_pagination():
    neighbors = {
        "AA" * 16: {
            "is_repeater": True,
            "zero_hop": True,
            "last_seen": time.time() - 1,
            "snr": 5.0,
        },
        "BB" * 16: {
            "is_repeater": True,
            "zero_hop": True,
            "last_seen": time.time() - 10,
            "snr": 1.0,
        },
        "CC" * 16: {
            "is_repeater": False,
            "zero_hop": True,
            "last_seen": time.time() - 1,
            "snr": 9.0,
        },
    }
    storage = SimpleNamespace(get_neighbors=lambda: neighbors)
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(),
        packet_injector=AsyncMock(),
        neighbor_tracker=SimpleNamespace(storage=storage),
    )

    # version=0, count=2, offset=0, order_by=2(strongest), pubkey_prefix_len=4, random=0
    req = bytes([0, 2]) + struct.pack("<H", 0) + bytes([2, 4]) + b"\x00\x00\x00\x00"
    out = helper._handle_get_neighbours(client=None, timestamp=0, req_data=req)

    total, returned = struct.unpack_from("<HH", out, 0)
    assert total == 2
    assert returned == 2


def test_protocol_request_owner_info_fallback_version():
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(),
        packet_injector=AsyncMock(),
        config={"repeater": {"node_name": "node-x", "owner_info": "owner-y"}},
    )

    with patch("importlib.metadata.version", side_effect=Exception("no pkg")):
        blob = helper._handle_get_owner_info(client=None, timestamp=0, req_data=b"")

    text = blob.decode("utf-8")
    assert "node-x" in text
    assert "owner-y" in text


def test_text_helper_cli_prefix_and_admin_permission_checks():
    # 0x21 is ADMIN (role 3). 0x22 is READ_WRITE (role 2) — the case that must
    # NOT pass the admin gate: the old `permissions & 0x02` test let a
    # read-write client run admin CLI commands.
    acl = _FakeACL(
        [
            _FakeClient(
                pubkey=bytes([0x21]) + b"x" * 31,
                shared_secret=b"k" * 32,
                permissions=PERM_ACL_ADMIN,
            ),
            _FakeClient(
                pubkey=bytes([0x22]) + b"x" * 31,
                shared_secret=b"k" * 32,
                permissions=PERM_ACL_READ_WRITE,
            ),
        ]
    )
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={0x41: acl})

    assert helper._is_cli_command("get status") is True
    assert helper._is_cli_command("99|get status") is True
    assert helper._is_cli_command("04|discover.neighbors") is True
    assert helper._is_cli_command("hello world") is False

    assert helper._check_admin_permission_for_identity(0x21, 0x41) is True
    assert helper._check_admin_permission_for_identity(0x22, 0x41) is False
    assert helper._check_admin_permission_for_identity(0x23, 0x41) is False


class _TxtPacket:
    """A packet as the core TextMessageHandler leaves it.

    ``txt_type``/``sender_timestamp`` default to absent so a test can model a
    core too old to publish them.
    """

    def __init__(self, text: str, txt_type=None, sender_timestamp=None):
        self.decrypted = {"text": text}
        if txt_type is not None:
            self.decrypted["txt_type"] = txt_type
        if sender_timestamp is not None:
            self.decrypted["sender_timestamp"] = sender_timestamp
        self.payload = bytearray([0x41, 0x21])
        self.mark_do_not_retransmit = MagicMock()


def _room_helper(permissions=PERM_ACL_ADMIN):
    """A TextHelper wired as a room-server identity with a stub CLI and store."""
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={})
    room = MagicMock()
    room.add_post = AsyncMock(return_value=True)
    room.cli = MagicMock()
    room.cli.handle_command = MagicMock(return_value="ok")
    helper.room_servers = {0x41: room}
    helper.handlers = {0x41: {"name": "room", "type": "room_server", "identity": MagicMock()}}
    helper._resolve_sender_client = MagicMock(
        return_value=_FakeClient(
            pubkey=bytes([0x21]) + b"x" * 31,
            shared_secret=b"k" * 32,
            permissions=permissions,
        )
    )
    helper._check_admin_permission_for_identity = MagicMock(return_value=True)
    helper._send_cli_reply = AsyncMock()
    return helper, room


async def _deliver(helper, packet, identity_type="repeater", name="rep"):
    await helper._on_message_received(
        identity_name=name,
        identity_type=identity_type,
        packet=packet,
        dest_hash=0x41,
        src_hash=0x21,
    )


def _repeater_cli_helper():
    """A TextHelper wired as a repeater identity whose admin CLI is a stub."""
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={})
    helper.repeater_hash = 0x41
    helper.cli = MagicMock()
    helper.cli.handle_command = MagicMock(return_value="ok")
    helper.handlers = {0x41: {"name": "rep", "type": "repeater", "identity": MagicMock()}}
    helper._resolve_sender_client = MagicMock(
        return_value=_FakeClient(
            pubkey=bytes([0x21]) + b"x" * 31,
            shared_secret=b"k" * 32,
            permissions=PERM_ACL_ADMIN,
        )
    )
    helper._check_admin_permission_for_identity = MagicMock(return_value=True)
    helper._send_cli_reply = AsyncMock()
    return helper


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "txt_type,runs",
    [
        (0, True),  # PLAIN     -- legacy CLI, firmware still accepts it
        (1, True),  # CLI_DATA  -- what every released client sends
        (3, True),  # CLI_COMMAND -- the type firmware split out (2c0ace25)
        (2, False),  # SIGNED_PLAIN -- a room post, never a command
        (None, True),  # no type reported (older core): behave as before
    ],
)
async def test_text_helper_runs_cli_only_for_server_text_types(txt_type, runs):
    """[fails pre-fix for SIGNED_PLAIN] Only the accepted types reach the CLI.

    simple_repeater::onPeerDataRecv opens its TXT_MSG branch by rejecting any
    flags outside {PLAIN, CLI_DATA, CLI_COMMAND}. openHop dispatched purely on
    whether the text looked like a command, so a SIGNED_PLAIN -- a room post,
    whose 4-byte author prefix the core handler has already stripped, leaving
    bare text -- ran as an admin CLI command whenever it happened to start with
    a command prefix.

    All three accepted types run here because a repeater has no chat function;
    that a *plain* one runs with no text test is covered separately.

    ``txt_type is None`` is a core too old to report it; that must keep working
    rather than silently dropping every message.
    """
    helper = _repeater_cli_helper()
    packet = _TxtPacket("get status", txt_type)

    await helper._on_message_received(
        identity_name="rep",
        identity_type="repeater",
        packet=packet,
        dest_hash=0x41,
        src_hash=0x21,
    )

    assert helper.cli.handle_command.called is runs
    assert helper._send_cli_reply.await_count == (1 if runs else 0)


@pytest.mark.asyncio
async def test_text_helper_signed_plain_is_not_stored_as_a_room_post():
    """A SIGNED_PLAIN never reaches the room server either.

    simple_room_server::onPeerDataRecv carries the same filter as the repeater;
    only PLAIN becomes a post there. openHop stored whatever arrived.
    """
    helper, room = _room_helper(permissions=PERM_ACL_READ_WRITE)

    await _deliver(helper, _TxtPacket("hello room", 2, sender_timestamp=100), "room_server", "room")
    room.add_post.assert_not_awaited()

    # ...while a plain post from the same writer still lands.
    await _deliver(helper, _TxtPacket("hello room", 0, sender_timestamp=101), "room_server", "room")
    room.add_post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "txt_type,is_command",
    [
        (0, False),  # PLAIN       -> a post
        (1, True),  # CLI_DATA    -> the admin CLI
        (3, True),  # CLI_COMMAND -> the admin CLI
    ],
)
async def test_room_dispatches_on_type_not_text(txt_type, is_command):
    """[fails pre-fix] The room splits command from post by type, as firmware does.

    simple_room_server::onPeerDataRecv routes CLI_DATA/CLI_COMMAND through
    handleCommand and turns PLAIN into a post. openHop asked
    `_is_cli_command(message_text)` instead, so it got both wrong: a PLAIN
    "get status" ran as a command rather than being posted, and a CLI command
    whose text was not in the prefix list was published to the room.

    Both cases use the *same* text, so only the type can decide. The text is
    one `_is_cli_command` recognises, which is what makes the PLAIN row the
    discriminator; the unrecognised direction is covered separately below.
    """
    helper, room = _room_helper()

    await _deliver(
        helper,
        _TxtPacket("get status", txt_type, sender_timestamp=100),
        identity_type="room_server",
        name="room",
    )

    assert room.cli.handle_command.called is is_command
    assert room.add_post.await_count == (0 if is_command else 1)


@pytest.mark.asyncio
async def test_room_runs_a_command_whose_text_is_not_a_known_prefix():
    """[fails pre-fix] The other direction: an unrecognised CLI command still runs.

    Deciding from the text published a command to the room whenever it was not
    in `_is_cli_command`'s prefix list -- the room's members got to read what
    the admin meant to run. Firmware routes every CLI_DATA/CLI_COMMAND from an
    admin through handleCommand regardless of what it says
    (simple_room_server::onPeerDataRecv).
    """
    helper, room = _room_helper()

    await _deliver(
        helper,
        _TxtPacket("frobnicate", 1, sender_timestamp=100),
        identity_type="room_server",
        name="room",
    )

    room.cli.handle_command.assert_called_once()
    room.add_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeater_runs_any_accepted_type_without_a_text_test():
    """[fails pre-fix] A repeater has no chat, so every accepted type is a command.

    simple_repeater::onPeerDataRecv hands PLAIN, CLI_DATA and CLI_COMMAND alike
    to handleCommand with no text test. openHop dropped anything whose text did
    not start with a known prefix, so an admin's mistyped command vanished
    silently instead of being answered.
    """
    helper = _repeater_cli_helper()

    await _deliver(helper, _TxtPacket("frobnicate", 1, sender_timestamp=100))

    helper.cli.handle_command.assert_called_once()
    assert helper.cli.handle_command.call_args.kwargs["command"] == "frobnicate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_timestamp,label",
    [(99, "older"), (100, "equal")],
)
async def test_repeater_command_is_not_run_twice(second_timestamp, label):
    """[fails pre-fix] A resent command is not executed again.

    simple_repeater guards on `sender_timestamp >= client->last_timestamp` and
    treats equality as a retry it answers with an empty reply; an older stamp is
    dropped whole. openHop checked neither, so replaying an admin command with
    the same timestamp and different attempt bits re-ran it -- packet dedup does
    not catch that, because the bytes differ.
    """
    helper = _repeater_cli_helper()

    await _deliver(helper, _TxtPacket("reboot", 1, sender_timestamp=100))
    assert helper.cli.handle_command.call_count == 1

    await _deliver(helper, _TxtPacket("reboot", 1, sender_timestamp=second_timestamp))
    assert helper.cli.handle_command.call_count == 1, f"{label} timestamp re-ran the command"


@pytest.mark.asyncio
async def test_repeater_command_runs_again_for_a_newer_timestamp():
    """A guard against over-blocking: a newer timestamp is fresh work.

    Prerequisite rather than regression proof -- it passes with the guard
    removed too. It is here so a future tightening of _accept_once cannot
    quietly reject the legitimate case.
    """
    helper = _repeater_cli_helper()

    await _deliver(helper, _TxtPacket("reboot", 1, sender_timestamp=100))
    await _deliver(helper, _TxtPacket("reboot", 1, sender_timestamp=101))

    assert helper.cli.handle_command.call_count == 2


@pytest.mark.asyncio
async def test_room_post_is_not_stored_twice_for_a_retry():
    """[fails pre-fix] firmware calls addPost only when `!is_retry`."""
    helper, room = _room_helper(permissions=PERM_ACL_READ_WRITE)

    await _deliver(helper, _TxtPacket("hello", 0, sender_timestamp=100), "room_server", "room")
    await _deliver(helper, _TxtPacket("hello", 0, sender_timestamp=100), "room_server", "room")

    assert room.add_post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions,posts",
    [
        (PERM_ACL_ADMIN, True),
        (PERM_ACL_READ_WRITE, True),
        (0, False),  # PERM_ACL_GUEST -- read the room, do not write to it
    ],
)
async def test_text_helper_room_post_denies_guests(permissions, posts):
    """[fails pre-fix] A guest may read the room but not post to it.

    simple_room_server::onPeerDataRecv takes the
    `(client->permissions & PERM_ACL_ROLE_MASK) == PERM_ACL_GUEST` branch for a
    PLAIN message and stores nothing. openHop called add_post for anyone who
    authenticated, so the guest password was enough to write to the room --
    add_post enforces a length cap and a rate limit, but no role.
    """
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={})
    room = MagicMock()
    room.add_post = AsyncMock(return_value=True)
    room.cli = None
    helper.room_servers = {0x41: room}
    helper.handlers = {0x41: {"name": "room", "type": "room_server", "identity": MagicMock()}}
    helper._resolve_sender_client = MagicMock(
        return_value=_FakeClient(
            pubkey=bytes([0x21]) + b"x" * 31,
            shared_secret=b"k" * 32,
            permissions=permissions,
        )
    )

    await helper._on_message_received(
        identity_name="room",
        identity_type="room_server",
        packet=_TxtPacket("hello room", 0),
        dest_hash=0x41,
        src_hash=0x21,
    )

    assert room.add_post.await_count == (1 if posts else 0)


@pytest.mark.parametrize(
    "identity_type,permissions,txt_type,timestamp,allowed,why",
    [
        ("repeater", PERM_ACL_ADMIN, 0, 101, True, "PLAIN from an admin is the legacy CLI"),
        ("repeater", PERM_ACL_READ_WRITE, 0, 101, False, "the branch is gated on isAdmin()"),
        ("repeater", PERM_ACL_ADMIN, 1, 101, False, "no ACK for a CLI type"),
        ("repeater", PERM_ACL_ADMIN, 3, 101, False, "no ACK for a CLI type"),
        ("repeater", PERM_ACL_ADMIN, 2, 101, False, "SIGNED_PLAIN is not an accepted type"),
        ("repeater", PERM_ACL_ADMIN, 0, 99, False, "older stamp: the branch is skipped"),
        ("repeater", PERM_ACL_ADMIN, 0, 100, True, "equal stamp is a retry, still ACKed"),
        ("room_server", PERM_ACL_READ_WRITE, 0, 101, True, "a writer's post is ACKed"),
        ("room_server", 0, 0, 101, False, "a guest gets neither post nor ACK"),
        ("room_server", PERM_ACL_ADMIN, 1, 101, False, "no ACK for a CLI type"),
    ],
)
def test_may_ack_matches_firmware(identity_type, permissions, txt_type, timestamp, allowed, why):
    """A server ACKs only what it would accept, and decides before answering.

    simple_repeater builds an ACK only under `if (flags == TXT_TYPE_PLAIN)`,
    inside a branch already gated on `client->isAdmin()`. simple_room_server
    sets send_ack only on the non-guest PLAIN path. Neither answers a CLI type,
    an unsupported type, or a replayed timestamp -- but both still answer a
    retry, because firmware suppresses the work, not the reply.
    """
    client = _FakeClient(
        pubkey=bytes([0x21]) + b"x" * 31, shared_secret=b"k" * 32, permissions=permissions
    )
    client.last_timestamp = 100

    assert _may_ack(identity_type, client, txt_type, timestamp) is allowed, why


def test_may_ack_stays_quiet_for_an_unknown_client():
    """Firmware only reaches onPeerDataRecv for a client its ACL matched."""
    assert _may_ack("repeater", None, 0, 100) is False


def test_may_ack_keeps_acking_when_the_core_reports_no_type():
    """An older core publishes no txt_type; do not go silent on that account.

    Reached only through the compatibility fallback: a core old enough to omit
    the type is also too old to install the policy, so this pins _may_ack's own
    contract rather than a path the pair can take together.
    """
    client = _FakeClient(pubkey=bytes([0x21]) + b"x" * 31, shared_secret=b"k" * 32, permissions=0)
    assert _may_ack("room_server", client, None, 100) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions,expect_ack",
    [
        (PERM_ACL_READ_WRITE, True),  # a writer's post is stored and answered
        (0, False),  # PERM_ACL_GUEST -- neither stored nor answered
    ],
)
async def test_guest_plain_post_earns_no_ack_on_the_wire(permissions, expect_ack):
    """[fails pre-fix] End to end: a guest's post produces nothing on the air.

    This drives the whole MC-R4 seam with real crypto -- a genuinely encrypted
    PLAIN DM, a real registered room identity, core's real TextMessageHandler,
    the real ACL -- and watches the injector. Core schedules the ACK before
    this helper's gates run, so without the policy the guest would see delivery
    confirmed for a post that was then refused.

    simple_room_server::onPeerDataRecv gives a guest neither storage nor ACK.
    """
    if not CORE_ACCEPTS_ACK_POLICY:
        pytest.skip("installed openhop_core has no should_ack_fn hook")

    server, sender = _distinct_identities(2)
    acl = ACL()
    client = ClientInfo(Identity(sender.get_public_key()), permissions=permissions)
    client.shared_secret = Identity(sender.get_public_key()).calc_shared_secret(
        server.get_private_key()
    )
    acl.clients[sender.get_public_key()] = client

    injector = AsyncMock(return_value=True)
    helper = TextHelper(
        identity_manager=MagicMock(),
        packet_injector=injector,
        acl_dict={server.get_public_key()[0]: acl},
    )
    room = MagicMock()
    room.add_post = AsyncMock(return_value=True)
    room.cli = None
    helper.register_identity("room", server, identity_type="room_server", radio_config={})
    helper.room_servers[server.get_public_key()[0]] = room

    packet, _crc = PacketBuilder.create_text_message(
        _SendDest(server.get_public_key()), sender, "hello room", 0, "direct", None, 0
    )
    assert await helper.process_text_packet(packet) is True

    # The ACK is scheduled on a delay; give it room to fire if it was going to.
    await asyncio.sleep(TXT_ACK_DELAY_MS / 1000.0 + 0.2)

    acked = any(
        getattr(call.args[0], "get_payload_type", lambda: None)() == PAYLOAD_TYPE_ACK
        for call in injector.await_args_list
    )
    assert acked is expect_ack
    assert room.add_post.await_count == (1 if expect_ack else 0)


def test_register_identity_survives_a_core_without_the_ack_hook():
    """[fails pre-fix] An older core must not take the node off the air.

    Even with a pinned baseline, running against a core that predates
    should_ack_fn can happen. Passing the argument regardless raises TypeError
    out of register_identity, which leaves the node with no text handler at all
    -- a far worse outcome than an ACK it should have withheld.
    """
    acl = _FakeACL([_FakeClient(pubkey=bytes([0x35]) + b"x" * 31, shared_secret=b"k" * 32)])
    helper = TextHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), acl_dict={0x35: acl}
    )
    identity = _FakeId(bytes([0x35]) + b"x" * 31)

    with (
        patch("repeater.handler_helpers.text.CORE_ACCEPTS_ACK_POLICY", False),
        patch("repeater.handler_helpers.text.TextMessageHandler") as tmh,
        patch("repeater.handler_helpers.text.MeshCLI", return_value=MagicMock()),
    ):
        helper.register_identity("rep", identity, identity_type="repeater", radio_config={})

    assert "should_ack_fn" not in tmh.call_args.kwargs
    assert 0x35 in helper.handlers


def test_register_identity_hands_the_core_handler_an_ack_policy():
    """The policy has to reach the handler, or none of the above applies.

    Core decides the ACK before the repeater's gates run, so this seam is the
    only place a server's rule can be applied in time.
    """
    acl = _FakeACL(
        [
            _FakeClient(
                pubkey=bytes([0x33]) + b"x" * 31,
                shared_secret=b"k" * 32,
                permissions=PERM_ACL_ADMIN,
            )
        ]
    )
    helper = TextHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), acl_dict={0x33: acl}
    )
    identity = _FakeId(bytes([0x33]) + b"x" * 31)

    # Pin the capability flag: this test is about what the policy decides, not
    # about which core happens to be importable, and indexing the kwarg would
    # KeyError under an older one.
    with (
        patch("repeater.handler_helpers.text.CORE_ACCEPTS_ACK_POLICY", True),
        patch("repeater.handler_helpers.text.TextMessageHandler") as tmh,
        patch("repeater.handler_helpers.text.MeshCLI", return_value=MagicMock()),
    ):
        helper.register_identity("rep", identity, identity_type="repeater", radio_config={})

    policy = tmh.call_args.kwargs["should_ack_fn"]
    admin_key = bytes([0x33]) + b"x" * 31
    assert policy(admin_key, 0, 100) is True  # PLAIN from the admin
    assert policy(admin_key, 1, 100) is False  # CLI_DATA earns no ACK
    assert policy(bytes([0x44]) + b"x" * 31, 0, 100) is False  # not a client here


@pytest.mark.asyncio
async def test_text_helper_process_text_packet_routes_or_forwards():
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={})

    pkt_short = SimpleNamespace(payload=bytearray([0x01]))
    assert await helper.process_text_packet(pkt_short) is False

    pkt_unknown = SimpleNamespace(payload=bytearray([0x55, 0x66]))
    assert await helper.process_text_packet(pkt_unknown) is False

    # Handler decrypts successfully: consume and stop forwarding.
    h = AsyncMock(return_value=HandlerResult.consumed())
    helper.handlers[0x10] = {"handler": h, "name": "id-a", "type": "repeater"}
    helper._on_message_received = AsyncMock()
    pkt = SimpleNamespace(payload=bytearray([0x10, 0x66]), mark_do_not_retransmit=MagicMock())

    handled = await helper.process_text_packet(pkt)

    assert handled is True
    h.assert_awaited_once_with(pkt)
    helper._on_message_received.assert_awaited_once()
    pkt.mark_do_not_retransmit.assert_called_once()


@pytest.mark.asyncio
async def test_text_helper_process_text_packet_hash_collision_forwards():
    """dest hash matches a local identity but decryption fails (collision): the
    packet is not ours, so it must NOT be consumed and must be left to forward (#353)."""
    helper = TextHelper(identity_manager=MagicMock(), acl_dict={})

    # Handler could not decrypt for this identity.
    h = AsyncMock(return_value=HandlerResult.not_for_us())
    helper.handlers[0x10] = {"handler": h, "name": "id-a", "type": "repeater"}
    helper._on_message_received = AsyncMock()
    pkt = SimpleNamespace(payload=bytearray([0x10, 0x66]), mark_do_not_retransmit=MagicMock())

    handled = await helper.process_text_packet(pkt)

    assert handled is False
    h.assert_awaited_once_with(pkt)
    helper._on_message_received.assert_not_awaited()
    pkt.mark_do_not_retransmit.assert_not_called()


@pytest.mark.asyncio
async def test_text_helper_send_packet_success_and_failures():
    injector = AsyncMock(side_effect=[True, RuntimeError("fail")])
    helper = TextHelper(identity_manager=MagicMock(), packet_injector=injector)

    assert await helper._send_packet(object(), wait_for_ack=False) is True
    assert await helper._send_packet(object(), wait_for_ack=False) is False

    helper.packet_injector = None
    assert await helper._send_packet(object(), wait_for_ack=False) is False


def test_text_helper_register_identity_repeater_initializes_cli_and_handler():
    acl = _FakeACL([_FakeClient(pubkey=bytes([0x33]) + b"x" * 31, shared_secret=b"k" * 32)])
    helper = TextHelper(
        identity_manager=MagicMock(),
        packet_injector=AsyncMock(),
        acl_dict={0x33: acl},
        config_path="/tmp/config.yaml",
        config={"repeater": {}},
        config_manager=MagicMock(),
        sqlite_handler=MagicMock(),
    )
    identity = _FakeId(bytes([0x33]) + b"x" * 31)

    with (
        patch("repeater.handler_helpers.text.TextMessageHandler", return_value=MagicMock()) as tmh,
        patch("repeater.handler_helpers.text.MeshCLI", return_value=MagicMock()) as mesh_cli,
    ):
        helper.register_identity("rep", identity, identity_type="repeater", radio_config={})

    tmh.assert_called_once()
    mesh_cli.assert_called_once()
    assert helper.repeater_hash == 0x33
    assert 0x33 in helper.handlers


def test_text_helper_register_identity_room_server_without_event_loop_is_safe():
    acl = _FakeACL([_FakeClient(pubkey=bytes([0x34]) + b"x" * 31, shared_secret=b"k" * 32)])
    helper = TextHelper(
        identity_manager=MagicMock(),
        packet_injector=AsyncMock(),
        acl_dict={0x34: acl},
        config_path="/tmp/config.yaml",
        config={"repeater": {}},
        config_manager=MagicMock(),
        sqlite_handler=MagicMock(),
    )
    helper._loop = None
    identity = _FakeId(bytes([0x34]) + b"x" * 31)

    with (
        patch("repeater.handler_helpers.text.TextMessageHandler", return_value=MagicMock()),
        patch("repeater.handler_helpers.text.RoomServer") as room_server_cls,
        patch(
            "repeater.handler_helpers.text.asyncio.get_running_loop",
            side_effect=RuntimeError("no loop"),
        ),
    ):
        room_server_obj = MagicMock()
        room_server_cls.return_value = room_server_obj
        helper.register_identity(
            "room-a", identity, identity_type="room_server", radio_config={"max_posts": 2}
        )

    assert 0x34 in helper.room_servers


@pytest.mark.asyncio
async def test_text_helper_send_cli_reply_uses_direct_path_from_client():
    helper = TextHelper(identity_manager=MagicMock(), packet_injector=AsyncMock())
    sender = _FakeClient(
        pubkey=bytes([0x99]) + b"x" * 31, shared_secret=b"s" * 32, permissions=0x02
    )
    sender.out_path = bytearray([0xAA, 0xBB])
    sender.out_path_len = 2
    helper.acl_dict = {0x10: _FakeACL([sender])}
    helper._send_packet = AsyncMock(return_value=True)

    original_packet = SimpleNamespace(payload=bytearray([0x10, 0x99]), get_route_type=lambda: 1)
    reply_packet = SimpleNamespace(path=bytearray(), path_len=0)

    with (
        patch("openhop_core.protocol.PacketBuilder.create_datagram", return_value=reply_packet),
        patch("repeater.handler_helpers.text.asyncio.sleep", new_callable=AsyncMock),
    ):
        await helper._send_cli_reply(
            original_packet=original_packet,
            reply_text="ok",
            handler_info={"identity": _FakeId(bytes([0x10]) + b"i" * 31)},
        )

    assert bytes(reply_packet.path) == b"\xaa\xbb"
    assert reply_packet.path_len == 2
    helper._send_packet.assert_awaited_once_with(reply_packet, wait_for_ack=False)


# ---------------------------------------------------------------------------
# Real-crypto collision integration tests (BUG-053).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_helper_real_crypto_consume_vs_collision_forward():
    """A real, correctly-encrypted DM to a local room-server identity is consumed;
    a DM encrypted for a remote node that merely collides on the one-byte dest
    hash fails to decrypt and is left for the forwarding engine."""
    local, sender, remote = _distinct_identities()
    local_hash = local.get_public_key()[0]

    helper = TextHelper(
        identity_manager=MagicMock(),
        acl_dict={local_hash: _acl_with_client(sender)},
        packet_injector=AsyncMock(),
    )
    helper.register_identity("room-a", local, identity_type="room_server")
    helper._on_message_received = AsyncMock()

    # Genuine CLI_DATA DM to the local identity from a known sender.
    genuine, _ = PacketBuilder.create_text_message(
        _SendDest(local.get_public_key()),
        sender,
        "status",
        message_type="flood",
        txt_type=TXT_TYPE_CLI_DATA,
    )
    assert await helper.process_text_packet(genuine) is True
    assert genuine.is_marked_do_not_retransmit()
    helper._on_message_received.assert_awaited_once()

    # DM encrypted for a REMOTE node whose one-byte dest hash collides with ours.
    helper._on_message_received.reset_mock()
    collision, _ = PacketBuilder.create_text_message(
        _SendDest(remote.get_public_key()),
        sender,
        "not yours",
        message_type="flood",
        txt_type=TXT_TYPE_CLI_DATA,
    )
    _force_dest_hash(collision, local_hash)
    assert await helper.process_text_packet(collision) is False
    assert not collision.is_marked_do_not_retransmit()
    helper._on_message_received.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_request_real_crypto_consume_vs_collision_forward():
    """A real, correctly-encrypted REQ to a local identity is consumed and
    answered; a REQ encrypted for a colliding remote node is left to forward."""
    from openhop_core.node.handlers.protocol_request import REQ_TYPE_GET_STATUS

    local, sender, remote = _distinct_identities()
    local_hash = local.get_public_key()[0]

    injector = AsyncMock(return_value=True)
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(),
        packet_injector=injector,
        acl_dict={local_hash: _acl_with_client(sender, local_identity=local)},
    )
    helper.register_identity("rep", local, identity_type="repeater")

    genuine, _ = PacketBuilder.create_protocol_request(
        _SendDest(local.get_public_key()), sender, REQ_TYPE_GET_STATUS
    )
    with patch("repeater.handler_helpers.protocol_request.asyncio.sleep", new_callable=AsyncMock):
        assert await helper.process_request_packet(genuine) is True
    assert genuine.is_marked_do_not_retransmit()
    injector.assert_awaited()  # a response was transmitted

    injector.reset_mock()
    collision, _ = PacketBuilder.create_protocol_request(
        _SendDest(remote.get_public_key()), sender, REQ_TYPE_GET_STATUS
    )
    _force_dest_hash(collision, local_hash)
    assert await helper.process_request_packet(collision) is False
    assert not collision.is_marked_do_not_retransmit()
    injector.assert_not_awaited()


# ---------------------------------------------------------------------------
# REQ_TYPE_GET_TELEMETRY_DATA (matches simple_repeater / simple_room_server
# handleRequest). Response payload is CayenneLPP: a base voltage entry on
# TELEM_CHANNEL_SELF, then configured sensors gated by the (inverse) perm mask.
# ---------------------------------------------------------------------------

from openhop_core.node.handlers.protocol_request import (  # noqa: E402
    REQ_TYPE_GET_TELEMETRY_DATA,
)
from openhop_core.protocol.cayenne_lpp import (  # noqa: E402
    TELEM_CHANNEL_SELF,
    encode_barometric_pressure,
    encode_current,
    encode_power,
    encode_relative_humidity,
    encode_temperature,
    encode_voltage,
)


class _FakeSensorManager:
    """Minimal sensor manager exposing cached readings via get_summary()."""

    def __init__(self, readings):
        self._readings = readings

    def get_summary(self):
        return {"readings": self._readings}


def _reading(ok=True, **data):
    return {"name": "s", "type": "t", "ok": ok, "data": data}


def test_telemetry_base_voltage_floor_when_no_sensors():
    """No configured sensors -> base voltage-only floor of 0.0 V."""
    helper = ProtocolRequestHelper(identity_manager=MagicMock(), packet_injector=AsyncMock())
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    assert lpp == encode_voltage(TELEM_CHANNEL_SELF, 0.0)


def test_telemetry_base_voltage_from_ups_sensor():
    """The configured UPS bus voltage seeds the base entry."""
    sm = _FakeSensorManager([_reading(bus_voltage_v=12.6)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    # Base voltage stays on channel 1, and the additional INA219 voltage view
    # is emitted on the next channel when environment telemetry is allowed.
    assert lpp == encode_voltage(TELEM_CHANNEL_SELF, 12.6) + encode_voltage(
        TELEM_CHANNEL_SELF + 1, 12.6
    )


def test_telemetry_admin_full_mask_includes_environment_sensors():
    """Admin with full mask gets configured env sensors after the base entry.

    The UPS reading has no temperature/humidity, so channel 2 is assigned to the
    first environment sensor (firmware querySensors channel assignment).
    """
    sm = _FakeSensorManager(
        [
            _reading(bus_voltage_v=12.6, current_ma=500.0, power_mw=6000.0),
            _reading(temperature_c=21.5, humidity_pct=55.0),
        ]
    )
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    # req_data[0] = 0x00 inverse mask -> perm_mask 0xFF (environment allowed).
    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    expected = (
        encode_voltage(TELEM_CHANNEL_SELF, 12.6)
        + encode_voltage(TELEM_CHANNEL_SELF + 1, 12.6)
        + encode_current(TELEM_CHANNEL_SELF + 1, 0.5)
        + encode_power(TELEM_CHANNEL_SELF + 1, 6)
        + encode_temperature(TELEM_CHANNEL_SELF + 2, 21.5)
        + encode_relative_humidity(TELEM_CHANNEL_SELF + 2, 55.0)
    )
    assert lpp == expected


def test_station_g3_subwatt_power_uses_lpp_one_watt_resolution():
    sm = _FakeSensorManager([_reading(bus_voltage_v=12.46, current_ma=59.7, power_mw=744.0)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    expected = (
        encode_voltage(TELEM_CHANNEL_SELF, 12.46)
        + encode_voltage(TELEM_CHANNEL_SELF + 1, 12.46)
        + encode_current(TELEM_CHANNEL_SELF + 1, 0.0597)
        + encode_power(TELEM_CHANNEL_SELF + 1, 0)
    )
    assert lpp == expected


def test_negative_power_is_preserved_as_current_but_omitted_from_unsigned_lpp_power():
    sm = _FakeSensorManager([_reading(bus_voltage_v=12.46, current_ma=-59.7, power_mw=-744.0)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    expected = (
        encode_voltage(TELEM_CHANNEL_SELF, 12.46)
        + encode_voltage(TELEM_CHANNEL_SELF + 1, 12.46)
        + encode_current(TELEM_CHANNEL_SELF + 1, -0.0597)
    )
    assert lpp == expected


def test_telemetry_bme280_emits_temperature_humidity_and_pressure_on_one_channel():
    """A BME280-shaped reading emits all three measured values on one channel.

    Mirrors the firmware's query_bme280 order: temperature, relative humidity,
    then barometric pressure.
    """
    sm = _FakeSensorManager([_reading(temperature_c=21.5, humidity_pct=55.0, pressure_hpa=1013.25)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    expected = (
        encode_voltage(TELEM_CHANNEL_SELF, 0.0)
        + encode_temperature(TELEM_CHANNEL_SELF + 1, 21.5)
        + encode_relative_humidity(TELEM_CHANNEL_SELF + 1, 55.0)
        + encode_barometric_pressure(TELEM_CHANNEL_SELF + 1, 1013.25)
    )
    assert lpp == expected


def test_telemetry_guest_forced_to_base_only():
    """A guest is restricted to base telemetry even when requesting the full mask."""
    sm = _FakeSensorManager([_reading(temperature_c=21.5, humidity_pct=55.0)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    guest = SimpleNamespace(is_guest=lambda: True)

    lpp = helper._handle_get_telemetry(guest, 0, b"\x00")

    assert lpp == encode_voltage(TELEM_CHANNEL_SELF, 0.0)


def test_telemetry_inverse_mask_gates_environment():
    """An inverse mask that clears the environment bit strips env sensors.

    perm_mask = ~inverse_mask; inverse byte 0x04 clears TELEM_PERM_ENVIRONMENT.
    """
    sm = _FakeSensorManager([_reading(temperature_c=21.5)])
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(), packet_injector=AsyncMock(), sensor_manager=sm
    )
    admin = SimpleNamespace(is_guest=lambda: False)

    lpp = helper._handle_get_telemetry(admin, 0, b"\x04")

    assert lpp == encode_voltage(TELEM_CHANNEL_SELF, 0.0)


@pytest.mark.asyncio
async def test_telemetry_req_end_to_end_produces_response_and_advances_watermark():
    """A real, correctly-encrypted telemetry REQ is consumed, answered through
    the normal _build_response path, and advances the client replay watermark."""
    local, sender, _remote = _distinct_identities()
    local_hash = local.get_public_key()[0]

    acl = _acl_with_client(sender, local_identity=local)
    client = acl.clients[sender.get_public_key()]
    assert client.last_timestamp == 0

    sm = _FakeSensorManager([_reading(bus_voltage_v=12.6)])
    injector = AsyncMock(return_value=True)
    helper = ProtocolRequestHelper(
        identity_manager=MagicMock(),
        packet_injector=injector,
        acl_dict={local_hash: acl},
        sensor_manager=sm,
    )
    helper.register_identity("rep", local, identity_type="repeater")

    genuine, ts = PacketBuilder.create_protocol_request(
        _SendDest(local.get_public_key()),
        sender,
        REQ_TYPE_GET_TELEMETRY_DATA,
        b"\x00",
    )
    with patch("repeater.handler_helpers.protocol_request.asyncio.sleep", new_callable=AsyncMock):
        assert await helper.process_request_packet(genuine) is True

    assert genuine.is_marked_do_not_retransmit()
    injector.assert_awaited()  # a RESPONSE frame was transmitted
    assert client.last_timestamp == ts  # replay watermark advanced
