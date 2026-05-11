import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("psutil", types.ModuleType("psutil"))

nacl_module = types.ModuleType("nacl")
nacl_signing_module = types.ModuleType("nacl.signing")


class _SigningKeyStub:
    pass


nacl_signing_module.SigningKey = _SigningKeyStub
nacl_module.signing = nacl_signing_module

sys.modules.setdefault("nacl", nacl_module)
sys.modules.setdefault("nacl.signing", nacl_signing_module)

from repeater.data_acquisition.storage_collector import StorageCollector


def _make_collector() -> StorageCollector:
    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler"),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(config={"storage": {"storage_dir": "/tmp/pymc_repeater_test"}})

    collector.sqlite_handler = MagicMock()
    collector.sqlite_handler.get_packet_stats.return_value = {"total_packets": 1}
    collector.websocket_available = True
    collector.websocket_broadcast_packet = MagicMock()
    collector.websocket_broadcast_stats = MagicMock()
    collector.websocket_has_connected_clients = MagicMock(return_value=True)
    collector.repeater_handler = SimpleNamespace(start_time=100.0)
    return collector


def test_publish_packet_sync_first_call_broadcasts_stats_immediately():
    collector = _make_collector()

    with patch("repeater.data_acquisition.storage_collector.time.monotonic", return_value=1000.0):
        collector._publish_packet_sync({"type": 1, "transmitted": True}, skip_mqtt=False)

    assert collector.sqlite_handler.get_packet_stats.call_count == 1
    assert collector.websocket_broadcast_stats.call_count == 1
    assert collector.websocket_broadcast_packet.call_count == 1


def test_publish_packet_sync_throttles_stats_to_interval():
    collector = _make_collector()
    call_times = [1000.0 + (i * 0.1) for i in range(10)] + [1005.1]

    with patch(
        "repeater.data_acquisition.storage_collector.time.monotonic",
        side_effect=call_times,
    ):
        for _ in call_times:
            collector._publish_packet_sync({"type": 1, "transmitted": True}, skip_mqtt=False)

    assert collector.sqlite_handler.get_packet_stats.call_count == 2
    assert collector.websocket_broadcast_stats.call_count == 2
    assert collector.websocket_broadcast_packet.call_count == len(call_times)


def test_publish_packet_sync_always_broadcasts_packet_event_even_without_clients():
    collector = _make_collector()
    collector.websocket_has_connected_clients.return_value = False

    for _ in range(5):
        collector._publish_packet_sync({"type": 1, "transmitted": True}, skip_mqtt=False)

    assert collector.websocket_broadcast_packet.call_count == 5
    assert collector.sqlite_handler.get_packet_stats.call_count == 0
    assert collector.websocket_broadcast_stats.call_count == 0
