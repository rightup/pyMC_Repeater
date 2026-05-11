import importlib.util
import time
from datetime import datetime, timezone
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "repeater" / "data_acquisition" / "gps_service.py"
_SPEC = importlib.util.spec_from_file_location("repeater_gps_service", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
GPSService = _MODULE.GPSService
NMEAParser = _MODULE.NMEAParser


def _sentence(payload: str) -> str:
    checksum = 0
    for char in payload:
        checksum ^= ord(char)
    return f"${payload}*{checksum:02X}"


def test_nmea_parser_combines_rmc_gga_gsa_gsv_attributes():
    parser = NMEAParser()

    assert parser.ingest_sentence(
        _sentence("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W")
    )
    assert parser.ingest_sentence(
        _sentence("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    )
    assert parser.ingest_sentence(
        _sentence("GPGSA,A,3,04,05,09,12,24,25,29,,,,,,1.8,1.0,1.5")
    )
    assert parser.ingest_sentence(
        _sentence("GPGSV,1,1,03,04,77,045,42,05,13,180,35,09,07,095,29")
    )

    snapshot = parser.snapshot()

    assert snapshot["status"]["state"] == "valid_fix"
    assert snapshot["position"]["latitude"] == 48.1173
    assert snapshot["position"]["longitude"] == 11.51666667
    assert snapshot["position"]["altitude_m"] == 545.4
    assert snapshot["motion"]["speed_knots"] == 22.4
    assert snapshot["motion"]["speed_kmh"] == 41.485
    assert snapshot["motion"]["course_degrees"] == 84.4
    assert snapshot["motion"]["magnetic_variation_degrees"] == -3.1
    assert snapshot["accuracy"]["hdop"] == 1.0
    assert snapshot["accuracy"]["pdop"] == 1.8
    assert snapshot["accuracy"]["vdop"] == 1.5
    assert snapshot["fix"]["quality"] == 1
    assert snapshot["fix"]["quality_label"] == "GPS"
    assert snapshot["fix"]["gsa_fix_type_label"] == "3D fix"
    assert snapshot["satellites"]["used_count"] == 7
    assert snapshot["satellites"]["in_view_count"] == 3
    assert snapshot["satellites"]["snr"]["max"] == 42.0
    assert snapshot["time"]["date"] == "1994-03-23"
    assert snapshot["time"]["datetime_utc"] == "1994-03-23T12:35:19.000000+00:00"
    assert set(snapshot["nmea"]["seen_sentence_types"]) == {"GGA", "GSA", "GSV", "RMC"}


def test_nmea_parser_rejects_bad_checksum_when_validation_enabled():
    parser = NMEAParser(validate_checksum=True)

    accepted = parser.ingest_sentence(
        "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*00"
    )

    snapshot = parser.snapshot()
    assert accepted is False
    assert snapshot["status"]["state"] == "error"
    assert snapshot["nmea"]["invalid_checksum_count"] == 1
    assert snapshot["status"]["last_error"] == "NMEA checksum mismatch"


def test_gps_service_file_source_reads_nmea_lines(tmp_path):
    path = tmp_path / "gps_nmea.txt"
    path.write_text(
        "\n".join(
            [
                _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,"),
                _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,"),
            ]
        ),
        encoding="utf-8",
    )

    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "source": "file",
                "source_path": str(path),
                "poll_interval_seconds": 0.05,
                "stale_after_seconds": 5.0,
                "time_sync_enabled": False,
            }
        }
    )
    service.start()
    try:
        deadline = time.time() + 1.0
        snapshot = service.get_snapshot()
        while snapshot["status"]["state"] == "no_data" and time.time() < deadline:
            time.sleep(0.05)
            snapshot = service.get_snapshot()
    finally:
        service.stop()

    assert snapshot["status"]["state"] == "valid_fix"
    assert snapshot["position"]["latitude"] == 42.83538333
    assert snapshot["position"]["longitude"] == -71.1076
    assert snapshot["satellites"]["used_count"] == 5


def test_rmc_only_fix_has_non_conflicting_quality_label():
    parser = NMEAParser()

    assert parser.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )

    snapshot = parser.snapshot()

    assert snapshot["status"]["state"] == "valid_fix"
    assert snapshot["fix"]["valid"] is True
    assert snapshot["fix"]["quality"] is None
    assert snapshot["fix"]["quality_label"] == "RMC valid"
    assert snapshot["position"]["latitude"] == 42.83538333
    assert snapshot["position"]["longitude"] == -71.1076


def test_snapshot_clamps_negative_age_after_system_clock_step():
    parser = NMEAParser()

    assert parser.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )
    parser.last_update = time.time() + 120

    snapshot = parser.snapshot()

    assert snapshot["status"]["state"] == "valid_fix"
    assert snapshot["status"]["age_seconds"] == 0.0


def test_gps_service_uses_manual_location_until_gps_fix():
    service = GPSService(
        {
            "repeater": {
                "latitude": 42.123456,
                "longitude": -71.654321,
            },
            "gps": {
                "enabled": True,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,0,00,8.8,32.0,M,0.0,M,,")
    )

    snapshot = service.get_snapshot()

    assert snapshot["status"]["state"] == "invalid_fix"
    assert snapshot["position"]["latitude"] == 42.123456
    assert snapshot["position"]["longitude"] == -71.654321
    assert snapshot["gps_position"]["latitude"] == 42.83538333
    assert snapshot["gps_position"]["longitude"] == -71.1076
    assert snapshot["manual_position"]["latitude"] == 42.123456
    assert snapshot["manual_position"]["longitude"] == -71.654321
    assert snapshot["position_meta"]["source"] == "manual_config"
    assert snapshot["position_meta"]["source_label"] == "manual config until GPS fix"
    assert snapshot["position_meta"]["manual_config_available"] is True
    assert snapshot["position_meta"]["gps_fix_valid"] is False


def test_gps_service_sets_system_time_from_valid_gps_datetime():
    clock_calls = []
    system_now = datetime(2026, 4, 23, 1, 1, 0, tzinfo=timezone.utc).timestamp()
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": True,
                "time_sync_interval_seconds": 60.0,
                "time_sync_min_offset_seconds": 1.0,
            },
        },
        clock_setter=clock_calls.append,
        time_provider=lambda: system_now,
    )

    assert service.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )

    snapshot = service.get_snapshot()

    assert len(clock_calls) == 1
    assert clock_calls[0] == datetime(2026, 4, 23, 1, 2, 3, tzinfo=timezone.utc)
    assert snapshot["time_sync"]["state"] == "synced"
    assert snapshot["time_sync"]["last_error"] is None
    assert snapshot["time_sync"]["last_gps_time"] == "2026-04-23T01:02:03+00:00"
    assert snapshot["time_sync"]["last_offset_seconds"] == 63.0


def test_gps_service_does_not_set_system_time_without_valid_fix():
    clock_calls = []
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": True,
            },
        },
        clock_setter=clock_calls.append,
    )

    assert service.ingest_sentence(
        _sentence("GPRMC,010203,V,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )

    snapshot = service.get_snapshot()

    assert clock_calls == []
    assert snapshot["status"]["state"] == "invalid_fix"
    assert snapshot["time_sync"]["state"] == "waiting_for_fix"


def test_gps_service_ignores_old_gps_time_without_throttling_valid_time():
    clock_calls = []
    system_now = datetime(2026, 4, 23, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": True,
                "time_sync_min_valid_year": 2020,
            },
        },
        clock_setter=clock_calls.append,
        time_provider=lambda: system_now,
    )

    assert service.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230394,,")
    )
    assert clock_calls == []
    assert service.get_snapshot()["time_sync"]["state"] == "ignored"

    assert service.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )

    assert clock_calls == [datetime(2026, 4, 23, 1, 2, 3, tzinfo=timezone.utc)]
    assert service.get_snapshot()["time_sync"]["state"] == "synced"


def test_gps_service_file_source_uses_time_sync_hook(tmp_path):
    path = tmp_path / "gps_nmea.txt"
    path.write_text(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,"),
        encoding="utf-8",
    )
    clock_calls = []
    system_now = datetime(2026, 4, 23, 1, 0, 0, tzinfo=timezone.utc).timestamp()

    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "source": "file",
                "source_path": str(path),
                "poll_interval_seconds": 0.05,
                "stale_after_seconds": 5.0,
                "time_sync_enabled": True,
            }
        },
        clock_setter=clock_calls.append,
        time_provider=lambda: system_now,
    )
    service.start()
    try:
        deadline = time.time() + 1.0
        while not clock_calls and time.time() < deadline:
            time.sleep(0.05)
    finally:
        service.stop()

    assert clock_calls == [datetime(2026, 4, 23, 1, 2, 3, tzinfo=timezone.utc)]


def test_gps_service_uses_gps_location_after_valid_fix():
    service = GPSService(
        {
            "repeater": {
                "latitude": 42.123456,
                "longitude": -71.654321,
            },
            "gps": {
                "enabled": True,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,")
    )

    snapshot = service.get_snapshot()

    assert snapshot["status"]["state"] == "valid_fix"
    assert snapshot["position"]["latitude"] == 42.83538333
    assert snapshot["position"]["longitude"] == -71.1076
    assert snapshot["manual_position"]["latitude"] == 42.123456
    assert snapshot["manual_position"]["longitude"] == -71.654321
    assert snapshot["position_meta"]["source"] == "gps"
    assert snapshot["position_meta"]["source_label"] == "GPS fix"
    assert snapshot["position_meta"]["manual_config_available"] is True
    assert snapshot["position_meta"]["gps_fix_valid"] is True


def test_gps_service_treats_zero_zero_manual_location_as_unset():
    service = GPSService(
        {
            "repeater": {
                "latitude": 0.0,
                "longitude": 0.0,
            },
            "gps": {
                "enabled": True,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,0,00,8.8,32.0,M,0.0,M,,")
    )

    snapshot = service.get_snapshot()

    assert snapshot["status"]["state"] == "invalid_fix"
    assert snapshot["position"]["latitude"] == 42.83538333
    assert snapshot["position"]["longitude"] == -71.1076
    assert snapshot["manual_position"] is None
    assert snapshot["position_meta"]["source"] == "gps"
    assert snapshot["position_meta"]["source_label"] == "GPS estimate"
    assert snapshot["position_meta"]["manual_config_available"] is False
    assert snapshot["position_meta"]["gps_fix_valid"] is False


def test_gps_service_reflects_runtime_manual_location_updates():
    config = {
        "repeater": {
            "latitude": 0.0,
            "longitude": 0.0,
        },
        "gps": {
            "enabled": True,
        },
    }
    service = GPSService(config)

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,0,00,8.8,32.0,M,0.0,M,,")
    )
    assert service.get_snapshot()["position_meta"]["source"] == "gps"

    config["repeater"]["latitude"] = 42.123456
    config["repeater"]["longitude"] = -71.654321
    snapshot = service.get_snapshot()

    assert snapshot["position"]["latitude"] == 42.123456
    assert snapshot["position"]["longitude"] == -71.654321
    assert snapshot["gps_position"]["latitude"] == 42.83538333
    assert snapshot["gps_position"]["longitude"] == -71.1076
    assert snapshot["position_meta"]["source"] == "manual_config"

def test_repeater_location_uses_config_when_gps_opt_in_disabled():
    service = GPSService(
        {
            "repeater": {
                "latitude": 42.123456,
                "longitude": -71.654321,
            },
            "gps": {
                "enabled": True,
                "use_gps_for_repeater_location": False,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,")
    )

    location = service.get_repeater_location()

    assert location["source"] == "config"
    assert location["latitude"] == 42.123456
    assert location["longitude"] == -71.654321


def test_repeater_location_uses_gps_with_optional_precision_rounding():
    service = GPSService(
        {
            "repeater": {
                "latitude": 42.123456,
                "longitude": -71.654321,
            },
            "gps": {
                "enabled": True,
                "use_gps_for_repeater_location": True,
                "repeater_location_precision_digits": 3,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,")
    )

    location = service.get_repeater_location()

    assert location["source"] == "gps"
    assert location["latitude"] == 42.835
    assert location["longitude"] == -71.108
    assert location["precision_digits"] == 3


def test_repeater_location_falls_back_to_config_without_valid_gps_fix():
    service = GPSService(
        {
            "repeater": {
                "latitude": 42.123456,
                "longitude": -71.654321,
            },
            "gps": {
                "enabled": True,
                "use_gps_for_repeater_location": True,
            },
        }
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,0,00,8.8,32.0,M,0.0,M,,")
    )

    location = service.get_repeater_location()

    assert location["source"] == "config_fallback_no_valid_gps_fix"
    assert location["latitude"] == 42.123456
    assert location["longitude"] == -71.654321


def test_gps_service_updates_repeater_location_from_valid_fix(monkeypatch):
    monotonic_now = [1000.0]
    monkeypatch.setattr(_MODULE.time, "monotonic", lambda: monotonic_now[0])
    location_updates = []
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": False,
                "update_repeater_location_from_fix": True,
                "location_update_interval_seconds": 600.0,
            }
        },
        location_update_callback=lambda payload: location_updates.append(payload) or True,
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,")
    )

    assert len(location_updates) == 1
    assert location_updates[0]["latitude"] == 42.83538333
    assert location_updates[0]["longitude"] == -71.1076
    snapshot = service.get_snapshot()
    assert snapshot["location_update"]["state"] == "updated"
    assert snapshot["location_update"]["last_latitude"] == 42.83538333
    assert snapshot["location_update"]["last_longitude"] == -71.1076

    assert service.ingest_sentence(
        _sentence("GPGGA,010204,4251.000,N,07107.000,W,1,05,1.4,33.0,M,0.0,M,,")
    )
    assert len(location_updates) == 1

    monotonic_now[0] += 601.0
    assert service.ingest_sentence(
        _sentence("GPGGA,010205,4251.000,N,07107.000,W,1,05,1.4,33.0,M,0.0,M,,")
    )
    assert len(location_updates) == 2
    assert location_updates[1]["latitude"] == 42.85
    assert location_updates[1]["longitude"] == -71.11666667


def test_gps_service_does_not_update_repeater_location_without_valid_fix():
    location_updates = []
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": False,
                "update_repeater_location_from_fix": True,
            }
        },
        location_update_callback=lambda payload: location_updates.append(payload) or True,
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,0,00,8.8,32.0,M,0.0,M,,")
    )

    assert location_updates == []
    assert service.get_snapshot()["location_update"]["state"] == "waiting_for_fix"


def test_gps_service_location_update_is_opt_in_by_default():
    location_updates = []
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": False,
            }
        },
        location_update_callback=lambda payload: location_updates.append(payload) or True,
    )

    assert service.ingest_sentence(
        _sentence("GPRMC,010203,A,4250.123,N,07106.456,W,000.0,180.0,230426,,")
    )

    assert location_updates == []
    assert service.get_snapshot()["location_update"]["state"] == "disabled"


def test_gps_service_fuzzes_persisted_repeater_location():
    location_updates = []
    service = GPSService(
        {
            "gps": {
                "enabled": True,
                "time_sync_enabled": False,
                "update_repeater_location_from_fix": True,
                "repeater_location_precision_digits": 3,
            }
        },
        location_update_callback=lambda payload: location_updates.append(payload) or True,
    )

    assert service.ingest_sentence(
        _sentence("GPGGA,010203,4250.123,N,07106.456,W,1,05,1.4,32.0,M,0.0,M,,")
    )

    assert len(location_updates) == 1
    assert location_updates[0]["latitude"] == 42.835
    assert location_updates[0]["longitude"] == -71.108
    assert location_updates[0]["precision_digits"] == 3
    snapshot = service.get_snapshot()
    assert snapshot["location_update"]["last_latitude"] == 42.835
    assert snapshot["location_update"]["last_longitude"] == -71.108
