import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from repeater.companion.constants import STATS_TYPE_CORE, STATS_TYPE_PACKETS, STATS_TYPE_RADIO
from repeater.exceptions import ConfigurationError
from repeater.identity_manager import IdentityConfigurationError
from repeater.main import RepeaterDaemon
from repeater.main import main as repeater_main
from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol import PacketBuilder
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_RAW_CUSTOM,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


class _FakeIdentity:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


def _base_config():
    return {
        "repeater": {
            "node_name": "node-test",
            "mode": "forward",
            "latitude": 1.0,
            "longitude": 2.0,
        },
        "logging": {"level": "INFO"},
    }


@pytest.mark.asyncio
async def test_router_callback_enqueues_and_handles_enqueue_error():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    packet = object()

    daemon.router = SimpleNamespace(enqueue=AsyncMock())
    await daemon._router_callback(packet)
    daemon.router.enqueue.assert_awaited_once_with(packet)

    daemon.router = SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("boom")))
    await daemon._router_callback(packet)


def test_register_text_handler_for_identity_branches():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    identity = _FakeIdentity(b"A" * 32)

    daemon.text_helper = None
    assert daemon.register_text_handler_for_identity("room", identity) is False

    helper = SimpleNamespace(register_identity=MagicMock())
    daemon.text_helper = helper
    assert daemon.register_text_handler_for_identity("room", identity) is True
    helper.register_identity.assert_called_once()

    helper_fail = SimpleNamespace(register_identity=MagicMock(side_effect=RuntimeError("x")))
    daemon.text_helper = helper_fail
    assert daemon.register_text_handler_for_identity("room", identity) is False


def test_get_stats_includes_public_key_gps_sensors_and_radio_state():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.repeater_handler = SimpleNamespace(get_stats=lambda: {"rx": 1})
    daemon.local_identity = _FakeIdentity(b"B" * 32)
    daemon.gps_service = SimpleNamespace(get_summary=lambda: {"gps": "ok"})
    daemon.sensor_manager = SimpleNamespace(get_summary=lambda: {"loaded": 1})
    daemon.radio_status = "degraded"
    daemon.radio_error = "missing device"

    stats = daemon.get_stats()

    assert stats["rx"] == 1
    assert stats["public_key"] == (b"B" * 32).hex()
    assert stats["gps"]["gps"] == "ok"
    assert stats["sensors"]["loaded"] == 1
    assert stats["radio_status"] == "degraded"
    assert stats["radio_error"] == "missing device"


def test_register_duplicate_logging_hook_only_when_dispatcher_dedup_enabled():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    dispatcher = SimpleNamespace(add_raw_packet_subscriber=MagicMock())
    daemon.dispatcher = dispatcher

    daemon._register_duplicate_logging_hook(False)
    dispatcher.add_raw_packet_subscriber.assert_not_called()

    daemon._register_duplicate_logging_hook(True)
    dispatcher.add_raw_packet_subscriber.assert_called_once_with(
        daemon._on_raw_packet_for_dedup_logging
    )


def test_detect_container_from_proc_env_and_fallback_path():
    with patch("builtins.open", MagicMock()) as open_mock:
        open_mock.return_value.__enter__.return_value.read.return_value = b"container=docker"
        assert RepeaterDaemon._detect_container() is True

    with (
        patch("builtins.open", side_effect=OSError("no proc")),
        patch("os.path.exists", return_value=True),
    ):
        assert RepeaterDaemon._detect_container() is True

    with (
        patch("builtins.open", side_effect=OSError("no proc")),
        patch("os.path.exists", return_value=False),
    ):
        assert RepeaterDaemon._detect_container() is False


@pytest.mark.asyncio
async def test_get_companion_stats_core_radio_packets_and_unknown():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    engine = SimpleNamespace(
        airtime_mgr=SimpleNamespace(get_stats=lambda: {"total_airtime_ms": 5000}),
        start_time=0,
        get_cached_noise_floor=lambda: -110,
        rx_count=7,
        forwarded_count=4,
        dropped_count=2,
    )
    daemon.repeater_handler = engine
    daemon.companion_bridges = {
        1: SimpleNamespace(message_queue=SimpleNamespace(count=3)),
        2: SimpleNamespace(message_queue=SimpleNamespace(count=2)),
    }
    daemon.dispatcher = SimpleNamespace(
        radio=SimpleNamespace(get_last_rssi=lambda: -70, get_last_snr=lambda: 4.5)
    )

    with patch("time.time", return_value=100):
        core = await daemon._get_companion_stats(STATS_TYPE_CORE)
    assert core["queue_len"] == 5
    assert core["uptime_secs"] == 100

    radio = await daemon._get_companion_stats(STATS_TYPE_RADIO)
    assert radio["noise_floor"] == -110
    assert radio["last_rssi"] == -70
    assert radio["tx_air_secs"] == 5

    packets = await daemon._get_companion_stats(STATS_TYPE_PACKETS)
    assert packets["recv"] == 7
    assert packets["sent"] == 4
    assert packets["recv_errors"] == 2

    assert await daemon._get_companion_stats(999) == {}


@pytest.mark.asyncio
async def test_raw_rx_and_duplicate_logging_hooks():
    daemon = RepeaterDaemon(_base_config(), radio=object())

    fs_ok = SimpleNamespace(push_rx_raw=MagicMock())
    fs_fail = SimpleNamespace(push_rx_raw=MagicMock(side_effect=RuntimeError("x")))
    daemon.companion_frame_servers = [fs_ok, fs_fail]

    await daemon._on_raw_rx_for_companions(b"abc", rssi=-90, snr=2.0)
    fs_ok.push_rx_raw.assert_called_once()

    # exclude_hash skips the matching companion's own frame server (no self-echo)
    fs_self = SimpleNamespace(companion_hash="0x1a", push_rx_raw=MagicMock())
    fs_other = SimpleNamespace(companion_hash="0x2b", push_rx_raw=MagicMock())
    daemon.companion_frame_servers = [fs_self, fs_other]
    await daemon._on_raw_rx_for_companions(b"xyz", rssi=0, snr=0.0, exclude_hash="0x1a")
    fs_self.push_rx_raw.assert_not_called()
    fs_other.push_rx_raw.assert_called_once()
    daemon.companion_frame_servers = [fs_ok, fs_fail]

    engine = SimpleNamespace(
        is_duplicate=MagicMock(side_effect=[False, True]),
        record_duplicate=MagicMock(),
    )
    daemon.repeater_handler = engine

    pkt = SimpleNamespace(_rssi=-77, _snr=1.5)
    daemon._on_raw_packet_for_dedup_logging(pkt, b"", {})
    daemon._on_raw_packet_for_dedup_logging(pkt, b"", {})
    engine.record_duplicate.assert_called_once_with(pkt, rssi=-77, snr=1.5)


@pytest.mark.asyncio
async def test_raw_custom_route_type_matrix_is_direct_only_and_first_seen():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    dispatcher = SimpleNamespace(register_handler=MagicMock())
    daemon.dispatcher = dispatcher
    daemon.router = SimpleNamespace(enqueue=AsyncMock())
    first_bridge = SimpleNamespace(process_received_packet=AsyncMock())
    second_bridge = SimpleNamespace(process_received_packet=AsyncMock())
    daemon.companion_bridges = {1: first_bridge, 2: second_bridge}
    engine = SimpleNamespace(
        is_duplicate=MagicMock(side_effect=[False, True, False]), mark_seen=MagicMock()
    )
    daemon.repeater_handler = engine

    daemon._register_raw_custom_handler()
    dispatcher.register_handler.assert_called_once_with(
        PAYLOAD_TYPE_RAW_CUSTOM, daemon._on_raw_data_for_companions
    )

    # Firmware wire vector: RAW_CUSTOM (0x0f), version 0, DIRECT (0x02).
    direct_packet = PacketBuilder.create_raw_data(b"\xa5")
    assert direct_packet.header == 0x3E
    assert direct_packet.write_to() == b"\x3e\x00\xa5"
    await daemon._on_raw_data_for_companions(direct_packet)
    await daemon._on_raw_data_for_companions(direct_packet)

    # Firmware also treats TRANSPORT_DIRECT as direct routing.
    transport_direct_packet = PacketBuilder.create_raw_data(b"\xa6")
    transport_direct_packet.header = (PAYLOAD_TYPE_RAW_CUSTOM << 2) | ROUTE_TYPE_TRANSPORT_DIRECT
    transport_direct_packet.transport_codes = [0x1234, 0x5678]
    assert transport_direct_packet.header == 0x3F
    assert transport_direct_packet.write_to() == b"\x3f\x34\x12\x78\x56\x00\xa6"
    await daemon._on_raw_data_for_companions(transport_direct_packet)

    first_bridge.process_received_packet.assert_has_awaits(
        [call(direct_packet), call(transport_direct_packet)]
    )
    second_bridge.process_received_packet.assert_has_awaits(
        [call(direct_packet), call(transport_direct_packet)]
    )
    engine.mark_seen.assert_has_calls([call(direct_packet), call(transport_direct_packet)])

    # A direct packet with a remaining hop is router traffic, not local raw data.
    intermediate_direct_packet = PacketBuilder.create_raw_data(b"\xa9")
    intermediate_direct_packet.path_len = 1
    intermediate_direct_packet.path = bytearray([0x42])
    assert intermediate_direct_packet.write_to() == b"\x3e\x01\x42\xa9"
    await daemon._on_raw_data_for_companions(intermediate_direct_packet)
    daemon.router.enqueue.assert_awaited_once_with(intermediate_direct_packet)

    # The corresponding FLOOD vector is discarded rather than delivered or routed.
    flood_packet = PacketBuilder.create_raw_data(b"\xa7")
    flood_packet.header = (PAYLOAD_TYPE_RAW_CUSTOM << 2) | ROUTE_TYPE_FLOOD
    assert flood_packet.header == 0x3D
    assert flood_packet.write_to() == b"\x3d\x00\xa7"
    await daemon._on_raw_data_for_companions(flood_packet)

    transport_flood_packet = PacketBuilder.create_raw_data(b"\xa8")
    transport_flood_packet.header = (PAYLOAD_TYPE_RAW_CUSTOM << 2) | ROUTE_TYPE_TRANSPORT_FLOOD
    transport_flood_packet.transport_codes = [0x1234, 0x5678]
    assert transport_flood_packet.header == 0x3C
    assert transport_flood_packet.write_to() == b"\x3c\x34\x12\x78\x56\x00\xa8"
    await daemon._on_raw_data_for_companions(transport_flood_packet)

    assert engine.is_duplicate.call_count == 3
    daemon.router.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_raw_custom_dispatcher_handler_bypasses_router_and_repeater_forwarding():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    radio = SimpleNamespace(
        set_rx_callback=MagicMock(),
        get_last_rssi=lambda: -80,
        get_last_snr=lambda: 2.0,
    )
    dispatcher = Dispatcher(radio, dedupe_enabled=False)
    daemon.dispatcher = dispatcher
    daemon.router = SimpleNamespace(enqueue=AsyncMock())
    daemon.repeater_handler = SimpleNamespace(
        is_duplicate=MagicMock(return_value=False), mark_seen=MagicMock()
    )
    bridge = SimpleNamespace(process_received_packet=AsyncMock())
    daemon.companion_bridges = {1: bridge}
    dispatcher.register_fallback_handler(daemon._router_callback)
    daemon._register_raw_custom_handler()

    packet = PacketBuilder.create_raw_data(b"\xde\xad")
    await dispatcher._process_received_packet(packet.write_to(), rssi=-81, snr=3.0)

    bridge.process_received_packet.assert_awaited_once()
    delivered = bridge.process_received_packet.await_args.args[0]
    assert delivered.header == 0x3E
    assert bytes(delivered.payload) == b"\xde\xad"
    daemon.router.enqueue.assert_not_awaited()

    intermediate = PacketBuilder.create_raw_data(b"\xbe")
    intermediate.path_len = 1
    intermediate.path = bytearray([0x42])
    await dispatcher._process_received_packet(intermediate.write_to(), rssi=-81, snr=3.0)
    daemon.router.enqueue.assert_awaited_once()

    flood_packet = PacketBuilder.create_raw_data(b"\xef")
    flood_packet.header = (PAYLOAD_TYPE_RAW_CUSTOM << 2) | ROUTE_TYPE_FLOOD
    await dispatcher._process_received_packet(flood_packet.write_to(), rssi=-81, snr=3.0)
    daemon.router.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_deliver_control_data_filters_non_discovery_and_pushes_valid():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    fs_ok = SimpleNamespace(
        owns_response_tag=lambda kind, tag: kind == "control" and tag == 0x44332211,
        push_control_data=AsyncMock(),
    )
    fs_fail = SimpleNamespace(
        owns_response_tag=lambda _kind, _tag: False,
        push_control_data=AsyncMock(side_effect=RuntimeError("err")),
    )
    daemon.companion_frame_servers = [fs_ok, fs_fail]

    await daemon.deliver_control_data(1.0, -70, 0, b"", b"\x80\x00")
    fs_ok.push_control_data.assert_not_awaited()

    payload = bytes([0x90, 0x00, 0x11, 0x22, 0x33, 0x44])
    await daemon.deliver_control_data(1.0, -70, 2, b"\xaa\xbb", payload)
    fs_ok.push_control_data.assert_awaited_once()
    fs_fail.push_control_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_radio_response_tag_collision_fails_closed_globally():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    first = SimpleNamespace(
        owns_response_tag=lambda _kind, _tag: True,
        discard_response_tag=MagicMock(),
        push_control_data=AsyncMock(),
    )
    second = SimpleNamespace(
        owns_response_tag=lambda _kind, _tag: True,
        discard_response_tag=MagicMock(),
        push_control_data=AsyncMock(),
    )
    daemon.companion_frame_servers = [first, second]
    payload = bytes([0x90, 0x00, 0x11, 0x22, 0x33, 0x44])

    await daemon.deliver_control_data(1.0, -70, 0, b"", payload)

    first.push_control_data.assert_not_awaited()
    second.push_control_data.assert_not_awaited()
    first.discard_response_tag.assert_called_once_with("control", 0x44332211)
    second.discard_response_tag.assert_called_once_with("control", 0x44332211)


@pytest.mark.asyncio
async def test_trace_complete_for_companions_requires_valid_lengths():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    fs = SimpleNamespace(
        owns_response_tag=lambda kind, tag: kind == "trace" and tag == 1,
        push_trace_data_async=AsyncMock(),
    )
    daemon.companion_frame_servers = [fs]

    packet = SimpleNamespace(path=bytearray([1, 2, 3]), get_snr=lambda: 2.0)

    await daemon._on_trace_complete_for_companions(packet, {"trace_path_bytes": b""})
    fs.push_trace_data_async.assert_not_awaited()

    parsed = {
        "trace_path_bytes": b"\xaa\xbb\xcc\xdd",
        "flags": 0,
        "tag": 1,
        "auth_code": 2,
    }
    await daemon._on_trace_complete_for_companions(packet, parsed)
    fs.push_trace_data_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_trace_complete_prefers_companion_api_owner_over_frame():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    bridge = SimpleNamespace(resolve_trace_ping=MagicMock(return_value=True))
    frame = SimpleNamespace(
        owns_response_tag=lambda kind, tag: kind == "trace" and tag == 1,
        push_trace_data_async=AsyncMock(),
    )
    daemon.companion_bridges = {1: bridge}
    daemon.companion_frame_servers = [frame]
    packet = SimpleNamespace(path=bytearray([1]), get_snr=lambda: 2.0)
    parsed = {
        "trace_path_bytes": b"\xaa",
        "flags": 0,
        "tag": 1,
        "auth_code": 2,
    }

    await daemon._on_trace_complete_for_companions(packet, parsed)

    bridge.resolve_trace_ping.assert_called_once_with(packet, parsed)
    frame.push_trace_data_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_identity_everywhere_calls_helpers_and_respects_collision():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    identity = _FakeIdentity(b"Q" * 32)

    daemon.identity_manager = SimpleNamespace(register_identity=MagicMock(return_value=False))
    daemon.login_helper = SimpleNamespace(register_identity=MagicMock())
    daemon.text_helper = SimpleNamespace(register_identity=MagicMock())
    daemon.protocol_request_helper = SimpleNamespace(register_identity=MagicMock())

    assert daemon._register_identity_everywhere("x", identity, {}, "room_server") is False
    daemon.login_helper.register_identity.assert_not_called()

    daemon.identity_manager.register_identity = MagicMock(return_value=True)
    assert daemon._register_identity_everywhere("x", identity, {}, "room_server") is True
    daemon.login_helper.register_identity.assert_called_once()
    daemon.text_helper.register_identity.assert_called_once()
    daemon.protocol_request_helper.register_identity.assert_called_once()


@pytest.mark.asyncio
async def test_send_advert_branches_and_success_path():
    daemon = RepeaterDaemon(_base_config(), radio=object())

    # Missing dispatcher/local identity
    assert await daemon.send_advert() is False

    daemon.dispatcher = SimpleNamespace(
        send_packet=AsyncMock(), packet_filter=SimpleNamespace(track_packet=MagicMock())
    )
    daemon.local_identity = _FakeIdentity(b"\x21" + b"x" * 31)
    daemon.config["repeater"]["mode"] = "no_tx"
    assert await daemon.send_advert() is False

    daemon.config["repeater"]["mode"] = "forward"
    daemon.repeater_handler = SimpleNamespace(mark_seen=MagicMock())
    daemon.gps_service = SimpleNamespace(
        get_repeater_location=lambda: {"latitude": 9.1, "longitude": 8.2, "source": "gps"}
    )

    packet = SimpleNamespace(calculate_packet_hash=lambda: b"\xab" * 16)
    with patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet):
        ok = await daemon.send_advert()

    assert ok is True
    daemon.dispatcher.send_packet.assert_awaited_once_with(packet, wait_for_ack=False)
    daemon.repeater_handler.mark_seen.assert_called_once_with(packet)
    daemon.dispatcher.packet_filter.track_packet.assert_called_once()


@pytest.mark.asyncio
async def test_send_advert_returns_false_when_dispatch_rejects():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.dispatcher = SimpleNamespace(
        send_packet=AsyncMock(return_value=False),
        packet_filter=SimpleNamespace(track_packet=MagicMock()),
    )
    daemon.local_identity = _FakeIdentity(b"\x21" + b"x" * 31)
    daemon.config["repeater"]["mode"] = "forward"
    daemon.repeater_handler = SimpleNamespace(mark_seen=MagicMock())
    daemon.gps_service = SimpleNamespace(
        get_repeater_location=lambda: {"latitude": 9.1, "longitude": 8.2, "source": "gps"}
    )

    packet = SimpleNamespace(calculate_packet_hash=lambda: b"\xab" * 16)
    with patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet):
        ok = await daemon.send_advert()

    assert ok is False
    daemon.dispatcher.send_packet.assert_awaited_once_with(packet, wait_for_ack=False)
    daemon.repeater_handler.mark_seen.assert_not_called()
    daemon.dispatcher.packet_filter.track_packet.assert_not_called()


@pytest.mark.asyncio
async def test_send_advert_applies_transport_scope_when_default_region_set():
    from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD

    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.dispatcher = SimpleNamespace(
        send_packet=AsyncMock(), packet_filter=SimpleNamespace(track_packet=MagicMock())
    )
    daemon.local_identity = _FakeIdentity(b"\x22" + b"y" * 31)
    daemon.repeater_handler = SimpleNamespace(mark_seen=MagicMock())
    daemon.config["mesh"] = {"default_region": "alpha"}

    packet = SimpleNamespace(
        header=ROUTE_TYPE_FLOOD,
        transport_codes=[0, 0],
        get_payload_type=lambda: 3,
        get_payload=lambda: b"minimal_advert_payload",
        calculate_packet_hash=lambda: b"\xcd" * 16,
    )

    with (
        patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet),
        patch("openhop_core.protocol.transport_keys.get_auto_key_for", return_value=b"\x01" * 16),
        patch("openhop_core.protocol.transport_keys.calc_transport_code", return_value=0xBEEF),
    ):
        ok = await daemon.send_advert()

    assert ok is True
    assert packet.transport_codes == [0xBEEF, 0]
    assert (packet.header & 0x03) == ROUTE_TYPE_TRANSPORT_FLOOD
    daemon.dispatcher.send_packet.assert_awaited_once_with(packet, wait_for_ack=False)


def test_update_repeater_location_from_gps_branches():
    daemon = RepeaterDaemon(_base_config(), radio=object())

    assert daemon._update_repeater_location_from_gps({"latitude": None, "longitude": 1.0}) is False

    # No change in location should return False.
    unchanged = {"latitude": 1.0, "longitude": 2.0}
    assert daemon._update_repeater_location_from_gps(unchanged) is False

    # Without config manager, updates in-memory config.
    updated = {"latitude": 3.5, "longitude": 4.5}
    assert daemon._update_repeater_location_from_gps(updated) is True
    assert daemon.config["repeater"]["latitude"] == 3.5
    assert daemon.config["repeater"]["longitude"] == 4.5

    daemon.config_manager = SimpleNamespace(
        update_and_save=MagicMock(return_value={"success": False, "error": "nope"})
    )
    assert daemon._update_repeater_location_from_gps({"latitude": 5.5, "longitude": 6.5}) is False

    daemon.config_manager = SimpleNamespace(
        update_and_save=MagicMock(return_value={"success": True})
    )
    assert daemon._update_repeater_location_from_gps({"latitude": 6.5, "longitude": 7.5}) is True


def test_signal_shutdown_idempotence_and_task_cancel():
    daemon = RepeaterDaemon(_base_config(), radio=object())
    created_task = object()

    def capture_task(coro):
        coro.close()
        return created_task

    loop = SimpleNamespace(create_task=MagicMock(side_effect=capture_task))
    sig = SimpleNamespace(name="SIGTERM")

    daemon._shutdown_started = True
    daemon._signal_shutdown(sig, loop)
    loop.create_task.assert_not_called()

    daemon._shutdown_started = False
    daemon._main_task = SimpleNamespace(done=lambda: False, cancel=MagicMock())
    daemon._signal_shutdown(sig, loop)
    loop.create_task.assert_called_once()
    assert daemon._shutdown_task is created_task
    daemon._main_task.cancel.assert_called_once()

    # A second signal before the scheduled coroutine starts must still reuse
    # the first owner task.
    daemon._signal_shutdown(sig, loop)
    loop.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_run_fails_closed_when_enabled_http_api_cannot_start():
    """A configured API must not disappear behind an otherwise healthy daemon."""
    daemon = RepeaterDaemon(
        {
            **_base_config(),
            "http": {"enabled": True, "host": "127.0.0.1", "port": 8000},
        },
        radio=object(),
    )
    daemon.initialize = AsyncMock()
    daemon._shutdown = AsyncMock()
    loop = SimpleNamespace(add_signal_handler=MagicMock())
    server = SimpleNamespace(start=MagicMock(side_effect=OSError("address in use")))

    with (
        patch("asyncio.get_running_loop", return_value=loop),
        patch("asyncio.get_event_loop", return_value=loop),
        patch("repeater.main.HTTPStatsServer", return_value=server),
        patch.object(RepeaterDaemon, "_detect_container", return_value=False),
        patch("os.path.exists", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="Enabled HTTP API failed to start"):
            await daemon.run()

    daemon._shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_keyboard_interrupt_uses_single_shutdown_owner():
    daemon = RepeaterDaemon(
        {**_base_config(), "http": {"enabled": False}},
        radio=object(),
    )
    daemon.initialize = AsyncMock()
    daemon.dispatcher = SimpleNamespace(
        run_forever=AsyncMock(side_effect=KeyboardInterrupt)
    )
    frame_server = SimpleNamespace(stop=AsyncMock())
    bridge = SimpleNamespace(stop=AsyncMock())
    daemon.companion_frame_servers = [frame_server]
    daemon.companion_bridges = {1: bridge}
    daemon.router = SimpleNamespace(stop=AsyncMock())
    daemon._shutdown = AsyncMock()
    loop = SimpleNamespace(add_signal_handler=MagicMock())

    with (
        patch("asyncio.get_running_loop", return_value=loop),
        patch.object(RepeaterDaemon, "_detect_container", return_value=False),
        patch("os.path.exists", return_value=False),
    ):
        await daemon.run()

    daemon._shutdown.assert_awaited_once()
    frame_server.stop.assert_not_awaited()
    bridge.stop.assert_not_awaited()
    daemon.router.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_swallows_only_shutdown_requested_cancellation():
    fake_loop = SimpleNamespace(add_signal_handler=MagicMock())

    async def start_blocked_run(daemon):
        initialize_started = asyncio.Event()
        never_finish = asyncio.Event()

        async def blocked_initialize():
            initialize_started.set()
            await never_finish.wait()

        daemon.initialize = blocked_initialize
        task = asyncio.create_task(daemon.run())
        await initialize_started.wait()
        return task

    with (
        patch("asyncio.get_running_loop", return_value=fake_loop),
        patch.object(RepeaterDaemon, "_detect_container", return_value=False),
        patch("os.path.exists", return_value=False),
    ):
        requested = RepeaterDaemon(
            {**_base_config(), "http": {"enabled": False}},
            radio=object(),
        )
        completed_owner = asyncio.create_task(asyncio.sleep(0))
        await completed_owner
        requested._shutdown_task = completed_owner
        requested_task = await start_blocked_run(requested)
        requested_task.cancel()
        await requested_task

        unrelated = RepeaterDaemon(
            {**_base_config(), "http": {"enabled": False}},
            radio=object(),
        )
        unrelated_task = await start_blocked_run(unrelated)
        unrelated_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await unrelated_task


@pytest.mark.asyncio
async def test_shutdown_stops_components_and_handles_errors():
    daemon = RepeaterDaemon(_base_config(), radio=SimpleNamespace(cleanup=MagicMock()))
    daemon.config["radio_type"] = "none"

    stop_order = []
    frame_server = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda: stop_order.append("frame"))
    )
    bridge = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda: stop_order.append("bridge"))
    )
    daemon.companion_frame_servers = [frame_server]
    daemon.companion_bridges = {1: bridge}
    daemon.router = SimpleNamespace(
        stop=AsyncMock(side_effect=lambda: stop_order.append("router"))
    )
    daemon.http_server = SimpleNamespace(
        stop=MagicMock(side_effect=lambda: stop_order.append("http"))
    )
    daemon.push_notifier = SimpleNamespace(
        stop=MagicMock(side_effect=lambda: stop_order.append("notifier"))
    )
    daemon.glass_handler = SimpleNamespace(stop=AsyncMock())
    daemon.sensor_manager = SimpleNamespace(stop=MagicMock())
    daemon.gps_service = SimpleNamespace(stop=MagicMock())
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(close=MagicMock()))

    await daemon._shutdown()

    frame_server.stop.assert_awaited_once()
    bridge.stop.assert_awaited_once()
    daemon.router.stop.assert_awaited_once()
    daemon.radio.cleanup.assert_called_once()
    assert stop_order[:5] == ["http", "notifier", "frame", "bridge", "router"]


@pytest.mark.asyncio
async def test_run_finalizer_joins_signal_owned_shutdown_task():
    """The task scheduled by the signal handler remains the sole teardown owner."""
    daemon = RepeaterDaemon(_base_config(), radio=object())
    frame_stop_started = asyncio.Event()
    allow_frame_stop = asyncio.Event()

    async def slow_frame_stop():
        frame_stop_started.set()
        await allow_frame_stop.wait()

    daemon.http_server = SimpleNamespace(stop=MagicMock())
    daemon.companion_frame_servers = [
        SimpleNamespace(stop=AsyncMock(side_effect=slow_frame_stop))
    ]
    daemon.companion_bridges = {}

    daemon._signal_shutdown(
        SimpleNamespace(name="SIGTERM"),
        asyncio.get_running_loop(),
    )
    owner_task = daemon._shutdown_task
    assert owner_task is not None
    await frame_stop_started.wait()

    # Mirrors run()'s finally block while signal-owned teardown is paused.
    finalizer = asyncio.create_task(daemon._shutdown())
    await asyncio.sleep(0)
    assert not finalizer.done()

    allow_frame_stop.set()
    await finalizer
    assert owner_task.done()


def test_main_entrypoint_success_and_fatal_paths(monkeypatch):
    class _Args:
        config = "/tmp/test.yaml"
        log_level = "DEBUG"

    cfg = _base_config()
    fake_daemon = SimpleNamespace(run=MagicMock(return_value=object()))

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_Args()),
        patch("repeater.main.load_config", return_value=cfg),
        patch("repeater.main.RepeaterDaemon", return_value=fake_daemon),
        patch("asyncio.run", MagicMock()),
    ):
        repeater_main()

    assert cfg["logging"]["level"] == "DEBUG"

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_Args()),
        patch("repeater.main.load_config", return_value=_base_config()),
        patch("repeater.main.RepeaterDaemon", return_value=fake_daemon),
        patch("asyncio.run", side_effect=RuntimeError("fatal")),
        patch("sys.exit", side_effect=SystemExit(1)) as exit_mock,
    ):
        with pytest.raises(SystemExit):
            repeater_main()

    exit_mock.assert_called_once_with(1)


def test_main_identity_config_error_exits_cleanly_without_traceback(caplog):
    """A configured-identity collision (an IdentityConfigurationError, which is
    a ConfigurationError subclass) exits 1 with a clean message and no
    stack-trace dump (regression: the fatal handler used to log exc_info=True
    for every exception)."""

    class _Args:
        config = "/tmp/test.yaml"
        log_level = None

    fake_daemon = SimpleNamespace(run=MagicMock(return_value=object()))
    err = IdentityConfigurationError(
        "Local identity 'companion:B' (hash=0x77) conflicts with 'companion:A'"
    )

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_Args()),
        patch("repeater.main.load_config", return_value=_base_config()),
        patch("repeater.main.RepeaterDaemon", return_value=fake_daemon),
        patch("asyncio.run", side_effect=err),
        patch("sys.exit", side_effect=SystemExit(1)) as exit_mock,
        caplog.at_level(logging.ERROR),
    ):
        with pytest.raises(SystemExit):
            repeater_main()

    exit_mock.assert_called_once_with(1)
    config_errors = [r for r in caplog.records if "Configuration error" in r.getMessage()]
    assert len(config_errors) == 1
    assert config_errors[0].exc_info is None  # no traceback attached
    assert not any("Fatal error" in r.getMessage() for r in caplog.records)


def test_main_config_load_error_exits_cleanly_without_traceback(caplog):
    """A ConfigurationError from load_config (missing/invalid config file) is
    now inside the try, so it also exits 1 cleanly rather than dumping a trace
    from before the event loop starts."""

    class _Args:
        config = "/tmp/missing.yaml"
        log_level = None

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_Args()),
        patch(
            "repeater.main.load_config",
            side_effect=ConfigurationError("Configuration file not found: /tmp/missing.yaml"),
        ),
        patch("sys.exit", side_effect=SystemExit(1)) as exit_mock,
        caplog.at_level(logging.ERROR),
    ):
        with pytest.raises(SystemExit):
            repeater_main()

    exit_mock.assert_called_once_with(1)
    config_errors = [r for r in caplog.records if "Configuration error" in r.getMessage()]
    assert len(config_errors) == 1
    assert config_errors[0].exc_info is None
    assert not any("Fatal error" in r.getMessage() for r in caplog.records)


def test_main_unexpected_error_keeps_traceback(caplog):
    """A genuine, non-config failure still logs 'Fatal error' with a traceback."""

    class _Args:
        config = "/tmp/test.yaml"
        log_level = None

    fake_daemon = SimpleNamespace(run=MagicMock(return_value=object()))

    with (
        patch("argparse.ArgumentParser.parse_args", return_value=_Args()),
        patch("repeater.main.load_config", return_value=_base_config()),
        patch("repeater.main.RepeaterDaemon", return_value=fake_daemon),
        patch("asyncio.run", side_effect=RuntimeError("boom")),
        patch("sys.exit", side_effect=SystemExit(1)) as exit_mock,
        caplog.at_level(logging.ERROR),
    ):
        with pytest.raises(SystemExit):
            repeater_main()

    exit_mock.assert_called_once_with(1)
    fatal = [r for r in caplog.records if "Fatal error" in r.getMessage()]
    assert len(fatal) == 1
    assert fatal[0].exc_info is not None  # traceback preserved for real crashes
    assert not any("Configuration error" in r.getMessage() for r in caplog.records)
