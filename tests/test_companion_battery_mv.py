"""Companion core-stats battery reporting.

The MeshCore companion frame packs ``battery_mv``; clients show it as the
connected device's battery. The repeater used to hardcode 0, so companion apps
always showed no battery even when a sensor plug-in was reporting one.
"""

from types import SimpleNamespace

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
    ):
        assert _daemon([_reading("modem", data)])._companion_battery_mv() == 0


def test_never_raises_when_the_sensor_manager_misbehaves():
    def boom():
        raise RuntimeError("sensor manager exploded")

    daemon = RepeaterDaemon(_base_config(), radio=object())
    daemon.sensor_manager = SimpleNamespace(get_summary=boom)
    assert daemon._companion_battery_mv() == 0
