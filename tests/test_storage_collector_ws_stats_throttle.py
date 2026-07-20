import builtins
import importlib.util
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from repeater.data_acquisition.storage_collector import StorageCollector

sys.modules.setdefault("psutil", types.ModuleType("psutil"))

nacl_module = types.ModuleType("nacl")
nacl_signing_module = types.ModuleType("nacl.signing")


class _SigningKeyStub:
    pass


nacl_signing_module.SigningKey = _SigningKeyStub
nacl_module.signing = nacl_signing_module

sys.modules.setdefault("nacl", nacl_module)
sys.modules.setdefault("nacl.signing", nacl_signing_module)


def _make_collector() -> StorageCollector:
    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler"),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={"storage": {"storage_dir": "/tmp/openhop_repeater_test"}}
        )

    # Stop any real stats-broadcast thread started during construction so the tests
    # drive the loop deterministically.
    collector._stats_stop_event.set()
    if collector._stats_thread is not None:
        collector._stats_thread.join(timeout=1)
    collector._stats_stop_event = threading.Event()
    collector._stats_thread = None

    collector.sqlite_handler = MagicMock()
    collector.sqlite_handler.get_packet_stats.return_value = {"total_packets": 1}
    collector.websocket_available = True
    collector.websocket_broadcast_packet = MagicMock()
    collector.websocket_broadcast_stats = MagicMock()
    collector.websocket_has_connected_clients = MagicMock(return_value=True)
    collector.repeater_handler = SimpleNamespace(start_time=100.0)
    return collector


def _stop_collector_threads(collector: StorageCollector) -> None:
    collector._stats_stop_event.set()
    if collector._stats_thread is not None:
        collector._stats_thread.join(timeout=1)


def test_publish_packet_sync_broadcasts_packet_event_not_stats():
    # The per-packet path must stay fast: it broadcasts the packet event but never
    # runs the heavy aggregate or the stats broadcast (those moved to the loop).
    collector = _make_collector()

    collector._publish_packet_sync({"type": 1, "transmitted": True}, skip_mqtt=False)

    assert collector.websocket_broadcast_packet.call_count == 1
    assert collector.sqlite_handler.get_packet_stats.call_count == 0
    assert collector.websocket_broadcast_stats.call_count == 0


def test_broadcast_stats_once_queries_and_broadcasts():
    collector = _make_collector()

    collector._broadcast_stats_once()

    collector.sqlite_handler.get_packet_stats.assert_called_once_with(hours=24)
    assert collector.websocket_broadcast_stats.call_count == 1
    payload = collector.websocket_broadcast_stats.call_args.args[0]
    assert payload["packet_stats"] == {"total_packets": 1}
    assert "uptime_seconds" in payload["system_stats"]


def test_stats_loop_broadcasts_when_clients_connected():
    collector = _make_collector()
    collector._broadcast_stats_once = MagicMock()
    collector.websocket_has_connected_clients = MagicMock(return_value=True)
    # Drive exactly one iteration: wait() returns False (proceed) then True (exit).
    collector._stats_stop_event = MagicMock()
    collector._stats_stop_event.wait.side_effect = [False, True]

    collector._stats_broadcast_loop()

    assert collector._broadcast_stats_once.call_count == 1


def test_stats_loop_skips_when_no_clients():
    collector = _make_collector()
    collector._broadcast_stats_once = MagicMock()
    collector.websocket_has_connected_clients = MagicMock(return_value=False)
    collector._stats_stop_event = MagicMock()
    collector._stats_stop_event.wait.side_effect = [False, True]

    collector._stats_broadcast_loop()

    assert collector._broadcast_stats_once.call_count == 0


def test_rrd_defaults_to_enabled_when_metrics_config_missing():
    rrd_candidate = MagicMock()
    rrd_candidate.available = True
    rrd_candidate.rrd_path.exists.return_value = True

    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch(
            "repeater.data_acquisition.storage_collector.RRDToolHandler",
            return_value=rrd_candidate,
        ) as rrd_cls,
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={"storage": {"storage_dir": "/tmp/openhop_repeater_test"}}
        )

    _stop_collector_threads(collector)

    assert collector.rrd_enabled is True
    rrd_cls.assert_called_once()
    assert collector.rrd_handler is rrd_candidate


def test_rrd_disabled_does_not_instantiate_handler():
    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler") as rrd_cls,
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={
                "storage": {"storage_dir": "/tmp/openhop_repeater_test"},
                "metrics": {"rrd_enabled": False},
            }
        )

    _stop_collector_threads(collector)

    assert collector.rrd_enabled is False
    assert collector.rrd_handler is None
    rrd_cls.assert_not_called()


def test_record_packet_skips_rrd_work_when_disabled():
    collector = _make_collector()
    collector.rrd_enabled = False
    collector.rrd_handler = None
    collector.sqlite_handler.store_packet.return_value = 123
    collector.sqlite_handler.get_cumulative_counts = MagicMock()
    collector._publish_packet_sync = MagicMock()

    collector._record_packet_blocking({"type": 1, "transmitted": False}, skip_mqtt=False)

    collector.sqlite_handler.store_packet.assert_called_once()
    collector.sqlite_handler.get_cumulative_counts.assert_not_called()
    collector._publish_packet_sync.assert_called_once()


def test_requested_but_unavailable_rrd_falls_back_to_sqlite():
    rrd_candidate = MagicMock()
    rrd_candidate.available = False
    rrd_candidate.rrd_path.exists.return_value = False

    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch(
            "repeater.data_acquisition.storage_collector.RRDToolHandler",
            return_value=rrd_candidate,
        ),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={"storage": {"storage_dir": "/tmp/openhop_repeater_test"}}
        )

    _stop_collector_threads(collector)
    collector.sqlite_handler.get_metrics_data.return_value = {
        "start_time": 0,
        "end_time": 0,
        "step": 60,
        "timestamps": [],
        "metrics": {},
        "packet_types": {},
        "data_source": "sqlite",
        "counter_mode": "bucket_count",
    }

    result = collector.get_metrics_data()

    assert collector.rrd_handler is None
    collector.sqlite_handler.get_metrics_data.assert_called_once_with(None, None, "average")
    assert result["data_source"] == "sqlite"


def test_metrics_data_falls_back_when_rrd_read_raises():
    collector = _make_collector()
    collector.rrd_handler = MagicMock()
    collector.rrd_handler.get_data.side_effect = RuntimeError("boom")
    collector.sqlite_handler.get_metrics_data.return_value = {
        "start_time": 0,
        "end_time": 60,
        "step": 60,
        "timestamps": [0, 60],
        "metrics": {"rx_count": [1, 2]},
        "packet_types": {},
        "data_source": "sqlite",
        "counter_mode": "bucket_count",
    }

    result = collector.get_metrics_data(start_time=0, end_time=60, resolution="average")

    collector.sqlite_handler.get_metrics_data.assert_called_once_with(0, 60, "average")
    assert result["data_source"] == "sqlite"


def test_metrics_data_prefers_healthy_rrd_result():
    collector = _make_collector()
    collector.rrd_handler = MagicMock()
    collector.rrd_handler.get_data.return_value = {
        "start_time": 0,
        "end_time": 60,
        "step": 60,
        "timestamps": [0, 60],
        "metrics": {"rx_count": [1, 2]},
    }

    result = collector.get_metrics_data(start_time=0, end_time=60, resolution="average")

    collector.sqlite_handler.get_metrics_data.assert_not_called()
    assert result["data_source"] == "rrd"


def test_metrics_data_falls_back_when_rrd_result_is_malformed():
    collector = _make_collector()
    collector.rrd_handler = MagicMock()
    collector.rrd_handler.get_data.return_value = {"start_time": 0, "end_time": 60}
    collector.sqlite_handler.get_metrics_data.return_value = {
        "start_time": 0,
        "end_time": 60,
        "step": 60,
        "timestamps": [0, 60],
        "metrics": {"rx_count": [0, 1]},
        "packet_types": {},
        "data_source": "sqlite",
        "counter_mode": "bucket_count",
    }

    result = collector.get_metrics_data(start_time=0, end_time=60, resolution="average")

    collector.sqlite_handler.get_metrics_data.assert_called_once_with(0, 60, "average")
    assert result["data_source"] == "sqlite"


def test_rrdtool_handler_import_succeeds_without_dependency(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[1] / "repeater" / "data_acquisition" / "rrdtool_handler.py"
    )
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "rrdtool":
            raise ImportError("missing rrdtool")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    spec = importlib.util.spec_from_file_location("rrdtool_handler_missing_dep", module_path)
    module = importlib.util.module_from_spec(spec)

    assert spec is not None
    assert spec.loader is not None

    spec.loader.exec_module(module)

    assert module.RRDTOOL_AVAILABLE is False
