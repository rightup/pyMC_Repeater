import json

from repeater.sensors.openhop_modem import OpenHopModemSensor
from repeater.sensors.pymc_modem import PymcModemSensor
from repeater.sensors.registry import SensorRegistry


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_openhop_modem_sensor_exposes_full_flat_diagnostics(monkeypatch):
    payload = {
        "battery_voltage_mv": 4112,
        "battery_voltage_v": 4.112,
        "solar_charge_rate_percent_per_hour": 4.5,
        "system": {
            "board": "Photon",
            "firmware": "1.2.3",
            "uptime": "1d 2h",
            "uptime_sec": 93600,
            "die_temperature_c": 54,
        },
        "counters": {
            "rx_packets": 101,
            "tx_packets": 22,
            "crc_errors": 3,
            "last_rssi_dbm": -91,
            "last_snr_db": 7.5,
            "noise_floor_dbm": -116.2,
        },
        "radio": {
            "state": "rx",
            "standby": False,
            "frequency_hz": 910525000,
            "frequency_mhz": 910.525,
            "bandwidth_hz": 62500,
            "bandwidth_khz": 62.5,
            "spreading_factor": 7,
            "coding_rate": 5,
            "tx_power_dbm": 18,
            "preamble_len": 32,
            "syncword": "0x12",
            "syncword_value": 18,
            "auto_cad_enabled": True,
        },
        "network": {
            "interface": "ethernet",
            "mode": "static",
            "live": True,
            "current_ip": "192.0.2.10",
            "tcp_port": 5055,
            "pymc_token_set": True,
        },
        "gps": {
            "available": True,
            "enabled": True,
            "seen": True,
            "fix": {"valid": True, "quality": 1},
            "position": {"latitude": 42.3, "longitude": -71.0, "altitude_m": 10.5},
            "satellites": {"used_count": 8, "in_view_count": 12},
            "time": {"datetime_utc": "2026-08-20T14:00:00Z"},
            "motion": {"speed_kmh": 1.2, "course_degrees": 180.0},
            "nmea": {
                "age_ms": 250,
                "valid_sentence_count": 400,
                "invalid_checksum_count": 2,
                "raw_byte_count": 12000,
                "last_sentence_type": "GGA",
            },
        },
    }
    monkeypatch.setattr(
        "repeater.sensors.openhop_modem.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    reading = OpenHopModemSensor(
        "modem", {"settings": {"base_url": "http://openhop-modem.local"}}
    ).read()

    assert reading["ok"] is True
    data = reading["data"]
    assert data["source"] == "openhop_modem"
    assert data["die_temperature_c"] == 54
    assert data["temperature_c"] == 54
    assert data["system_board"] == "Photon"
    assert data["system_firmware"] == "1.2.3"
    assert data["system_uptime"] == "1d 2h"
    assert data["uptime_seconds"] == 93600
    assert data["rx_packets"] == 101
    assert data["tx_packets"] == 22
    assert data["crc_errors"] == 3
    assert data["last_rssi_dbm"] == -91
    assert data["last_snr_db"] == 7.5
    assert data["noise_floor_dbm"] == -116.2
    assert data["radio_state"] == "rx"
    assert data["radio_standby"] is False
    assert data["frequency_hz"] == 910525000
    assert data["bandwidth_hz"] == 62500
    assert data["spreading_factor"] == 7
    assert data["coding_rate"] == 5
    assert data["tx_power_dbm"] == 18
    assert data["preamble_length"] == 32
    assert data["auto_cad_enabled"] is True
    assert data["network_interface"] == "ethernet"
    assert data["network_live"] is True
    assert data["tcp_port"] == 5055
    assert data["token_configured"] is True
    assert data["gps_available"] is True
    assert data["gps_nmea_age_ms"] == 250
    assert data["gps_nmea_valid_sentences"] == 400
    assert data["gps_nmea_invalid_checksums"] == 2
    assert data["gps_nmea_raw_bytes"] == 12000
    assert data["gps_nmea_last_sentence"] == "GGA"
    assert data["battery_voltage_mv"] == 4112
    assert data["solar_charge_rate_percent_per_hour"] == 4.5


def test_legacy_pymc_modem_module_and_registry_create_canonical_sensor():
    assert PymcModemSensor is OpenHopModemSensor
    legacy = SensorRegistry.create(
        "pymc_modem",
        name="legacy",
        config={"settings": {"base_url": "http://legacy-modem.local"}},
    )
    canonical = SensorRegistry.create(
        "openhop_modem",
        name="canonical",
        config={"settings": {"base_url": "http://openhop-modem.local"}},
    )

    assert isinstance(legacy, OpenHopModemSensor)
    assert legacy.sensor_type == "openhop_modem"
    assert canonical.sensor_type == "openhop_modem"
