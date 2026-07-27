import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.handler_helpers.path import PathHelper
from repeater.handler_helpers.protocol_request import ProtocolRequestHelper
from repeater.handler_helpers.text import TextHelper


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

    assert handled is False
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
        return response_packet

    helper.handlers[dest] = {"handler": _core_handler}
    pkt = _ReqPacket(payload=bytes([dest, 0x01, 0x02]))

    with patch("repeater.handler_helpers.protocol_request.asyncio.sleep", new_callable=AsyncMock):
        handled = await helper.process_request_packet(pkt)

    assert handled is True
    pkt.mark_do_not_retransmit.assert_called_once()
    injector.assert_awaited_once_with(response_packet, wait_for_ack=False)


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

    h = AsyncMock()
    helper.handlers[0x10] = {"handler": h, "name": "id-a", "type": "repeater"}
    helper._on_message_received = AsyncMock()
    pkt = SimpleNamespace(payload=bytearray([0x10, 0x66]), mark_do_not_retransmit=MagicMock())

    handled = await helper.process_text_packet(pkt)

    assert handled is True
    h.assert_awaited_once_with(pkt)
    helper._on_message_received.assert_awaited_once()
    pkt.mark_do_not_retransmit.assert_called_once()


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
