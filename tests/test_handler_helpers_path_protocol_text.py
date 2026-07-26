import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhop_core.node.handlers.result import HandlerResult
from openhop_core.protocol import Identity, LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.handler_helpers.acl import ACL, PERM_ACL_ADMIN, ClientInfo
from repeater.handler_helpers.path import PathHelper
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper
from repeater.handler_helpers.text import TextHelper

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
    acl = _FakeACL(
        [
            _FakeClient(
                pubkey=bytes([0x21]) + b"x" * 31, shared_secret=b"k" * 32, permissions=0x02
            ),
            _FakeClient(
                pubkey=bytes([0x22]) + b"x" * 31, shared_secret=b"k" * 32, permissions=0x01
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
        + encode_current(TELEM_CHANNEL_SELF + 2, 0.5)
        + encode_power(TELEM_CHANNEL_SELF + 3, 6)
        + encode_temperature(TELEM_CHANNEL_SELF + 4, 21.5)
        + encode_relative_humidity(TELEM_CHANNEL_SELF + 4, 55.0)
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


def test_telemetry_includes_ina219_power_channels_before_environment_sensors():
    """INA219 voltage/current/power values are emitted before env sensors."""
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

    lpp = helper._handle_get_telemetry(admin, 0, b"\x00")

    expected = (
        encode_voltage(TELEM_CHANNEL_SELF, 12.6)
        + encode_voltage(TELEM_CHANNEL_SELF + 1, 12.6)
        + encode_current(TELEM_CHANNEL_SELF + 2, 0.5)
        + encode_power(TELEM_CHANNEL_SELF + 3, 6)
        + encode_temperature(TELEM_CHANNEL_SELF + 4, 21.5)
        + encode_relative_humidity(TELEM_CHANNEL_SELF + 4, 55.0)
    )
    assert lpp == expected


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
