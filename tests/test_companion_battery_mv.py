"""Companion core-stats battery reporting.

The MeshCore companion frame packs ``battery_mv``; clients show it as the
connected device's battery. The repeater used to hardcode 0, so companion apps
always showed no battery even when a sensor plug-in was reporting one.
"""

import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openhop_core.companion.constants import (
    CMD_GET_BATT_AND_STORAGE,
    RESP_CODE_BATT_AND_STORAGE,
)

from repeater.main import RepeaterDaemon


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


def _daemon(readings=None):
    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.sensor_manager = (
        None if readings is None else SimpleNamespace(get_summary=lambda: {"readings": readings})
    )
    return daemon


def _reading(name, data, ok=True):
    return {"name": name, "type": name, "ok": ok, "timestamp": "t", "data": data}


def test_returns_zero_without_a_sensor_manager():
    assert _daemon()._companion_battery_mv() == 0


def test_reads_battery_voltage_mv():
    daemon = _daemon([_reading("modem", {"battery_voltage_mv": 4213})])
    assert daemon._companion_battery_mv() == 4213


def test_derives_millivolts_from_volts():
    daemon = _daemon([_reading("modem", {"battery_voltage_v": 3.72})])
    assert daemon._companion_battery_mv() == 3720


def test_skips_failed_and_batteryless_readings():
    daemon = _daemon(
        [
            _reading("broken", {"battery_voltage_mv": 4100}, ok=False),
            _reading("system-health", {"cpu_percent": 12.0}),
            _reading("modem", {"battery_voltage_mv": 3980}),
        ]
    )
    assert daemon._companion_battery_mv() == 3980


def test_ignores_out_of_range_and_unparseable_values():
    for data in (
        {"battery_voltage_mv": 0},
        {"battery_voltage_mv": 70000},
        {"battery_voltage_mv": "n/a"},
        {"battery_voltage_v": None},
        {"battery_voltage_mv": float("nan")},
        {"battery_voltage_mv": float("inf")},
        {"battery_voltage_v": float("-inf")},
    ):
        assert _daemon([_reading("modem", data)])._companion_battery_mv() == 0


def test_never_raises_when_the_sensor_manager_misbehaves():
    def boom():
        raise RuntimeError("sensor manager exploded")

    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.sensor_manager = SimpleNamespace(get_summary=boom)
    assert daemon._companion_battery_mv() == 0


def test_companion_storage_dir_uses_default_path():
    assert _daemon()._companion_storage_dir() == "/var/lib/openhop_repeater"


def test_companion_storage_dir_supports_legacy_top_level_setting(tmp_path):
    daemon = _daemon()
    daemon.config["storage_dir"] = str(tmp_path)
    assert daemon._companion_storage_dir() == str(tmp_path)


def test_companion_storage_dir_resolves_relative_to_config(tmp_path):
    daemon = _daemon()
    daemon.config["storage"] = {"storage_dir": "data"}
    daemon.__dict__["config_path"] = str(tmp_path / "config.yaml")
    assert daemon._companion_storage_dir() == str(tmp_path / "data")


def test_frame_server_reports_battery_and_storage(tmp_path):
    """The companion BATT_AND_STORAGE hook must return real values, not zeros."""
    from repeater.companion.frame_server import CompanionFrameServer

    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.batt_getter = lambda: 4221
    fs.storage_dir = str(tmp_path)

    mv, used_kb, total_kb = fs._get_batt_and_storage()
    assert mv == 4221
    assert total_kb > 0
    assert 0 <= used_kb <= total_kb


def test_frame_server_battery_survives_a_broken_getter(tmp_path):
    from repeater.companion.frame_server import CompanionFrameServer

    fs = CompanionFrameServer.__new__(CompanionFrameServer)

    def boom():
        raise RuntimeError("sensor exploded")

    fs.batt_getter = boom
    fs.storage_dir = str(tmp_path)
    mv, _, _ = fs._get_batt_and_storage()
    assert mv == 0


def test_frame_server_without_getters_returns_zeros():
    from repeater.companion.frame_server import CompanionFrameServer

    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.batt_getter = None
    fs.storage_dir = None
    assert fs._get_batt_and_storage() == (0, 0, 0)


@pytest.mark.asyncio
async def test_get_batt_and_storage_command_dispatches_real_values(tmp_path):
    """Exercise the real command registry and response-frame encoding path."""
    from repeater.companion.frame_server import CompanionFrameServer

    fs = CompanionFrameServer(
        bridge=SimpleNamespace(),
        companion_hash="test",
        batt_getter=lambda: 4221,
        storage_dir=str(tmp_path),
    )
    fs._write_frame = MagicMock()

    await fs._handle_cmd(bytes([CMD_GET_BATT_AND_STORAGE]))

    frame = fs._write_frame.call_args.args[0]
    response_code, millivolts, used_kb, total_kb = struct.unpack("<BHII", frame)
    assert response_code == RESP_CODE_BATT_AND_STORAGE
    assert millivolts == 4221
    assert total_kb > 0
    assert 0 <= used_kb <= total_kb
