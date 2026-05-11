"""
GPS/NMEA acquisition and diagnostics support.

The service intentionally keeps the NMEA parser local and dependency-light.  It
accepts raw NMEA sentences from a serial receiver or from a file source used by
external bridge processes, then exposes a JSON-serializable snapshot for the
HTTP API.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("GPSService")


FIX_QUALITY_LABELS = {
    0: "no fix",
    1: "GPS",
    2: "DGPS",
    4: "RTK fixed",
    5: "RTK float",
    6: "estimated",
    7: "manual",
    8: "simulation",
}

GSA_FIX_TYPE_LABELS = {
    1: "no fix",
    2: "2D fix",
    3: "3D fix",
}


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_valid_latitude(value: Optional[float]) -> bool:
    return value is not None and -90.0 <= value <= 90.0


def _is_valid_longitude(value: Optional[float]) -> bool:
    return value is not None and -180.0 <= value <= 180.0


def _is_zero_coordinate(latitude: Optional[float], longitude: Optional[float]) -> bool:
    return latitude == 0.0 and longitude == 0.0


def _normalize_precision_digits(value: Any) -> Optional[int]:
    digits = _to_int(value)
    if digits is None:
        return None
    return max(0, min(8, digits))


def _nmea_checksum(payload: str) -> int:
    checksum = 0
    for char in payload:
        checksum ^= ord(char)
    return checksum


def _parse_lat_lon(value: str, hemisphere: str) -> Optional[float]:
    if not value or not hemisphere:
        return None

    dot = value.find(".")
    if dot < 0:
        dot = len(value)

    # Latitude is ddmm.mmmm, longitude is dddmm.mmmm.
    degree_digits = dot - 2
    if degree_digits <= 0:
        return None

    try:
        degrees = float(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError:
        return None

    decimal = degrees + minutes / 60.0
    if hemisphere.upper() in ("S", "W"):
        decimal *= -1
    return round(decimal, 8)


def _format_time(value: str) -> Optional[str]:
    if not value or len(value) < 6:
        return None
    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        second_float = float(value[4:])
    except ValueError:
        return None
    second = int(second_float)
    microsecond = int(round((second_float - second) * 1_000_000))
    if microsecond >= 1_000_000:
        second += 1
        microsecond -= 1_000_000
    try:
        return f"{hour:02d}:{minute:02d}:{second:02d}.{microsecond:06d}Z"
    except ValueError:
        return None


def _format_date(value: str) -> Optional[str]:
    if not value or len(value) != 6:
        return None
    try:
        day = int(value[0:2])
        month = int(value[2:4])
        year_2 = int(value[4:6])
    except ValueError:
        return None

    # NMEA RMC uses two-digit years.  GPS modules in this project are modern,
    # but keep 1980-2079 rollover behavior for old sample data.
    year = 2000 + year_2 if year_2 < 80 else 1900 + year_2
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return None


def _combine_datetime_utc(date_value: Optional[str], time_value: Optional[str]) -> Optional[str]:
    if not date_value or not time_value:
        return None
    try:
        time_part = time_value.rstrip("Z")
        return f"{date_value}T{time_part}+00:00"
    except Exception:
        return None


def _parse_datetime_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _set_system_clock_from_datetime(value: datetime):
    if not hasattr(time, "clock_settime") or not hasattr(time, "CLOCK_REALTIME"):
        raise RuntimeError("time.clock_settime(CLOCK_REALTIME) is not available")
    time.clock_settime(time.CLOCK_REALTIME, value.timestamp())


class NMEAParser:
    """Small NMEA parser focused on diagnostics fields used by GPS receivers."""

    def __init__(
        self,
        *,
        validate_checksum: bool = True,
        require_checksum: bool = False,
        retain_sentences: int = 25,
        stale_after_seconds: float = 10.0,
    ):
        self.validate_checksum = validate_checksum
        self.require_checksum = require_checksum
        self.retain_sentences = max(0, int(retain_sentences))
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self._lock = threading.RLock()
        self._reset_unlocked()

    def _reset_unlocked(self):
        self.position: Dict[str, Any] = {
            "latitude": None,
            "longitude": None,
            "altitude_m": None,
            "geoid_separation_m": None,
        }
        self.motion: Dict[str, Any] = {
            "speed_knots": None,
            "speed_kmh": None,
            "course_degrees": None,
            "magnetic_variation_degrees": None,
        }
        self.accuracy: Dict[str, Any] = {
            "hdop": None,
            "pdop": None,
            "vdop": None,
        }
        self.time_data: Dict[str, Any] = {
            "utc_time": None,
            "date": None,
            "datetime_utc": None,
        }
        self.fix: Dict[str, Any] = {
            "valid": False,
            "status": None,
            "quality": None,
            "quality_label": FIX_QUALITY_LABELS[0],
            "gsa_fix_type": None,
            "gsa_fix_type_label": None,
        }
        self.satellites: Dict[str, Any] = {
            "used_count": None,
            "used_prns": [],
            "in_view_count": None,
            "in_view": [],
            "snr": {
                "min": None,
                "max": None,
                "avg": None,
            },
        }
        self.nmea: Dict[str, Any] = {
            "last_sentence": None,
            "last_sentence_type": None,
            "last_talker": None,
            "seen_sentence_types": [],
            "sentence_counters": {},
            "valid_checksum_count": 0,
            "invalid_checksum_count": 0,
            "missing_checksum_count": 0,
            "recent_sentences": [],
        }
        self.raw_attributes: Dict[str, Any] = {}
        self.last_update = None
        self.last_error = None
        self._sentence_counters: Counter = Counter()
        self._recent_sentences = deque(maxlen=self.retain_sentences)
        self._unhandled_sentence_types = set()

    def reset(self):
        with self._lock:
            self._reset_unlocked()

    def ingest_many(self, lines: Iterable[str]):
        for line in lines:
            self.ingest_sentence(line)

    def ingest_sentence(self, sentence: str) -> bool:
        parsed = self._split_sentence(sentence)
        with self._lock:
            if parsed is None:
                return False

            raw, payload, checksum_valid, talker, sentence_type, fields = parsed
            if checksum_valid is False:
                self.nmea["invalid_checksum_count"] += 1
                self.last_error = "NMEA checksum mismatch"
                if self.validate_checksum:
                    return False
            elif checksum_valid is True:
                self.nmea["valid_checksum_count"] += 1
            else:
                self.nmea["missing_checksum_count"] += 1
                if self.require_checksum:
                    self.last_error = "NMEA checksum missing"
                    return False

            now = time.time()
            self.last_update = now
            self.last_error = None
            self._sentence_counters[sentence_type] += 1
            self.nmea["last_sentence"] = raw
            self.nmea["last_sentence_type"] = sentence_type
            self.nmea["last_talker"] = talker
            self.nmea["seen_sentence_types"] = sorted(self._sentence_counters.keys())
            self.nmea["sentence_counters"] = dict(self._sentence_counters)
            if self.retain_sentences:
                self._recent_sentences.append(
                    {
                        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                        "sentence_type": sentence_type,
                        "sentence": raw,
                    }
                )
                self.nmea["recent_sentences"] = list(self._recent_sentences)

            handler = getattr(self, f"_parse_{sentence_type.lower()}", None)
            if handler:
                handler(fields)
            else:
                self._unhandled_sentence_types.add(sentence_type)
                self.raw_attributes["unhandled_sentence_types"] = sorted(
                    self._unhandled_sentence_types
                )

            self._refresh_derived_unlocked()
            return True

    def _split_sentence(
        self, sentence: str
    ) -> Optional[Tuple[str, str, Optional[bool], str, str, List[str]]]:
        raw = (sentence or "").strip()
        if not raw:
            return None

        if raw.startswith("$"):
            raw_no_dollar = raw[1:]
        else:
            raw_no_dollar = raw

        payload = raw_no_dollar
        checksum_valid: Optional[bool] = None
        if "*" in raw_no_dollar:
            payload, supplied_checksum = raw_no_dollar.split("*", 1)
            supplied_checksum = supplied_checksum[:2]
            try:
                expected = int(supplied_checksum, 16)
                checksum_valid = _nmea_checksum(payload) == expected
            except ValueError:
                checksum_valid = False

        fields = payload.split(",")
        if not fields or len(fields[0]) < 3:
            return None

        sentence_id = fields[0].upper()
        talker = sentence_id[:-3] or None
        sentence_type = sentence_id[-3:]
        return raw, payload, checksum_valid, talker, sentence_type, fields

    def _parse_rmc(self, fields: List[str]):
        # $GPRMC,time,status,lat,N,lon,E,sog,cog,date,magvar,E/W,...
        self.raw_attributes["RMC"] = {
            "utc_time_raw": self._field(fields, 1),
            "status": self._field(fields, 2),
            "date_raw": self._field(fields, 9),
            "mode": self._field(fields, 12),
            "nav_status": self._field(fields, 13),
        }
        status = (self._field(fields, 2) or "").upper()
        self.fix["status"] = "valid" if status == "A" else "invalid" if status == "V" else status
        self.fix["valid"] = status == "A" or bool(self.fix.get("quality"))
        if status == "A" and self.fix.get("quality") is None:
            self.fix["quality_label"] = "RMC valid"

        latitude = _parse_lat_lon(self._field(fields, 3), self._field(fields, 4))
        longitude = _parse_lat_lon(self._field(fields, 5), self._field(fields, 6))
        if latitude is not None:
            self.position["latitude"] = latitude
        if longitude is not None:
            self.position["longitude"] = longitude

        speed_knots = _to_float(self._field(fields, 7))
        if speed_knots is not None:
            self.motion["speed_knots"] = speed_knots
            self.motion["speed_kmh"] = round(speed_knots * 1.852, 3)

        course = _to_float(self._field(fields, 8))
        if course is not None:
            self.motion["course_degrees"] = course

        mag_var = _to_float(self._field(fields, 10))
        mag_dir = (self._field(fields, 11) or "").upper()
        if mag_var is not None:
            self.motion["magnetic_variation_degrees"] = -mag_var if mag_dir == "W" else mag_var

        utc_time = _format_time(self._field(fields, 1))
        date = _format_date(self._field(fields, 9))
        if utc_time:
            self.time_data["utc_time"] = utc_time
        if date:
            self.time_data["date"] = date
        self.time_data["datetime_utc"] = _combine_datetime_utc(
            self.time_data.get("date"), self.time_data.get("utc_time")
        )

    def _parse_gga(self, fields: List[str]):
        # $GPGGA,time,lat,N,lon,E,quality,num_sats,hdop,alt,M,geoid,M,...
        quality = _to_int(self._field(fields, 6))
        satellites_used = _to_int(self._field(fields, 7))
        self.raw_attributes["GGA"] = {
            "utc_time_raw": self._field(fields, 1),
            "fix_quality_raw": self._field(fields, 6),
            "dgps_age": self._field(fields, 13),
            "dgps_station_id": self._field(fields, 14),
        }
        if quality is not None:
            self.fix["quality"] = quality
            self.fix["quality_label"] = FIX_QUALITY_LABELS.get(quality, f"quality {quality}")
            self.fix["valid"] = quality > 0 or self.fix.get("status") == "valid"

        latitude = _parse_lat_lon(self._field(fields, 2), self._field(fields, 3))
        longitude = _parse_lat_lon(self._field(fields, 4), self._field(fields, 5))
        if latitude is not None:
            self.position["latitude"] = latitude
        if longitude is not None:
            self.position["longitude"] = longitude

        if satellites_used is not None:
            self.satellites["used_count"] = satellites_used

        hdop = _to_float(self._field(fields, 8))
        if hdop is not None:
            self.accuracy["hdop"] = hdop

        altitude = _to_float(self._field(fields, 9))
        if altitude is not None:
            self.position["altitude_m"] = altitude

        geoid_sep = _to_float(self._field(fields, 11))
        if geoid_sep is not None:
            self.position["geoid_separation_m"] = geoid_sep

        utc_time = _format_time(self._field(fields, 1))
        if utc_time:
            self.time_data["utc_time"] = utc_time
            self.time_data["datetime_utc"] = _combine_datetime_utc(
                self.time_data.get("date"), self.time_data.get("utc_time")
            )

    def _parse_gsa(self, fields: List[str]):
        # $GPGSA,mode,fix_type,sv1..sv12,pdop,hdop,vdop
        fix_type = _to_int(self._field(fields, 2))
        used_prns = [value for value in fields[3:15] if value]
        self.raw_attributes["GSA"] = {
            "mode": self._field(fields, 1),
        }
        if fix_type is not None:
            self.fix["gsa_fix_type"] = fix_type
            self.fix["gsa_fix_type_label"] = GSA_FIX_TYPE_LABELS.get(fix_type, f"type {fix_type}")
            self.fix["valid"] = fix_type > 1 or self.fix.get("valid", False)

        self.satellites["used_prns"] = used_prns
        if used_prns:
            self.satellites["used_count"] = len(used_prns)

        pdop = _to_float(self._field(fields, 15))
        hdop = _to_float(self._field(fields, 16))
        vdop = _to_float(self._field(fields, 17))
        if pdop is not None:
            self.accuracy["pdop"] = pdop
        if hdop is not None:
            self.accuracy["hdop"] = hdop
        if vdop is not None:
            self.accuracy["vdop"] = vdop

    def _parse_gsv(self, fields: List[str]):
        # $GPGSV,total_msgs,msg_num,sats_in_view,prn,elev,az,snr,...
        total_messages = _to_int(self._field(fields, 1))
        message_number = _to_int(self._field(fields, 2))
        satellites_in_view = _to_int(self._field(fields, 3))
        current = [] if message_number == 1 else list(self.satellites.get("in_view") or [])

        for idx in range(4, len(fields), 4):
            prn = self._field(fields, idx)
            if not prn:
                continue
            satellite = {
                "prn": prn,
                "elevation_degrees": _to_int(self._field(fields, idx + 1)),
                "azimuth_degrees": _to_int(self._field(fields, idx + 2)),
                "snr_db": _to_float(self._field(fields, idx + 3)),
            }
            current.append(satellite)

        # Deduplicate by PRN while preserving last value for each satellite.
        by_prn = {sat["prn"]: sat for sat in current}
        in_view = sorted(by_prn.values(), key=lambda item: item["prn"])
        self.satellites["in_view"] = in_view
        self.satellites["in_view_count"] = satellites_in_view
        self.raw_attributes["GSV"] = {
            "total_messages": total_messages,
            "last_message_number": message_number,
        }

    def _parse_vtg(self, fields: List[str]):
        # $GPVTG,cog,T,cog_magnetic,M,sog_knots,N,sog_kmh,K,...
        course_true = _to_float(self._field(fields, 1))
        course_magnetic = _to_float(self._field(fields, 3))
        speed_knots = _to_float(self._field(fields, 5))
        speed_kmh = _to_float(self._field(fields, 7))
        if course_true is not None:
            self.motion["course_degrees"] = course_true
        if speed_knots is not None:
            self.motion["speed_knots"] = speed_knots
        if speed_kmh is not None:
            self.motion["speed_kmh"] = speed_kmh
        self.raw_attributes["VTG"] = {
            "course_magnetic_degrees": course_magnetic,
            "mode": self._field(fields, 9),
        }

    @staticmethod
    def _field(fields: List[str], index: int) -> str:
        return fields[index] if index < len(fields) else ""

    def _normalize_raw_attributes_unlocked(self):
        if self._unhandled_sentence_types:
            self.raw_attributes["unhandled_sentence_types"] = sorted(self._unhandled_sentence_types)

    def _refresh_derived_unlocked(self):
        snrs = [
            sat.get("snr_db")
            for sat in self.satellites.get("in_view", [])
            if sat.get("snr_db") is not None
        ]
        if snrs:
            self.satellites["snr"] = {
                "min": min(snrs),
                "max": max(snrs),
                "avg": round(sum(snrs) / len(snrs), 3),
            }
        else:
            self.satellites["snr"] = {"min": None, "max": None, "avg": None}
        self._normalize_raw_attributes_unlocked()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            age = now - self.last_update if self.last_update else None
            if age is not None and age < 0:
                age = 0.0
            stale = age is None or age > self.stale_after_seconds
            fix_valid = bool(self.fix.get("valid")) and not stale
            if self.last_error:
                state = "error"
            elif age is None:
                state = "no_data"
            elif stale:
                state = "stale"
            elif fix_valid:
                state = "valid_fix"
            else:
                state = "invalid_fix"

            return {
                "status": {
                    "state": state,
                    "fix_valid": fix_valid,
                    "stale": stale,
                    "age_seconds": round(age, 3) if age is not None else None,
                    "last_update": (
                        datetime.fromtimestamp(self.last_update, timezone.utc).isoformat()
                        if self.last_update
                        else None
                    ),
                    "last_error": self.last_error,
                },
                "fix": deepcopy(self.fix),
                "position": deepcopy(self.position),
                "motion": deepcopy(self.motion),
                "accuracy": deepcopy(self.accuracy),
                "time": deepcopy(self.time_data),
                "satellites": deepcopy(self.satellites),
                "nmea": deepcopy(self.nmea),
                "raw_attributes": deepcopy(self.raw_attributes),
            }


class GPSService:
    """Runtime GPS acquisition service."""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        clock_setter: Optional[Callable[[datetime], None]] = None,
        time_provider: Optional[Callable[[], float]] = None,
        location_update_callback: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ):
        gps_config = config.get("gps", {}) if isinstance(config, dict) else {}
        repeater_config = config.get("repeater", {}) if isinstance(config, dict) else {}
        self.config = gps_config
        self.enabled = bool(gps_config.get("enabled", False))
        self.api_fallback_to_config_location = bool(
            gps_config.get("api_fallback_to_config_location", True)
        )
        self.advertise_gps_location = bool(
            gps_config.get("advertise_gps_location", False)
        )
        self.location_precision_digits = _normalize_precision_digits(
            gps_config.get("location_precision_digits")
        )
        self.source = str(gps_config.get("source", "serial")).lower()
        self.device = gps_config.get("device", "/dev/serial0")
        self.baud_rate = int(gps_config.get("baud_rate", 9600))
        self.read_timeout_seconds = float(gps_config.get("read_timeout_seconds", 1.0))
        self.reconnect_interval_seconds = float(gps_config.get("reconnect_interval_seconds", 5.0))
        self.poll_interval_seconds = float(gps_config.get("poll_interval_seconds", 2.0))
        self.source_path = gps_config.get("source_path") or gps_config.get("snapshot_path")
        self.repeater_config = repeater_config
        self.time_sync_enabled = bool(gps_config.get("time_sync_enabled", True))
        self.time_sync_interval_seconds = max(
            1.0, float(gps_config.get("time_sync_interval_seconds", 3600.0))
        )
        self.time_sync_min_offset_seconds = max(
            0.0, float(gps_config.get("time_sync_min_offset_seconds", 1.0))
        )
        self.time_sync_min_valid_year = int(gps_config.get("time_sync_min_valid_year", 2020))
        self._clock_setter = clock_setter or _set_system_clock_from_datetime
        self._time_provider = time_provider or time.time
        self.persist_gps_fix_enabled = bool(
            gps_config.get("persist_gps_fix_to_config", False)
        )
        self.persist_gps_fix_interval_seconds = max(
            1.0, float(gps_config.get("persist_gps_fix_interval_seconds", 600.0))
        )
        self._location_update_callback = location_update_callback
        self._location_update_lock = threading.RLock()
        self._last_location_update_monotonic: Optional[float] = None
        self._location_update_status: Dict[str, Any] = {
            "enabled": self.persist_gps_fix_enabled,
            "state": "disabled" if not self.persist_gps_fix_enabled else "waiting_for_fix",
            "last_attempt": None,
            "last_success": None,
            "last_error": None,
            "last_latitude": None,
            "last_longitude": None,
            "interval_seconds": self.persist_gps_fix_interval_seconds,
        }
        self._time_sync_lock = threading.RLock()
        self._last_time_sync_monotonic: Optional[float] = None
        self._time_sync_status: Dict[str, Any] = {
            "enabled": self.time_sync_enabled,
            "state": "disabled" if not self.time_sync_enabled else "waiting_for_fix",
            "last_attempt": None,
            "last_success": None,
            "last_error": None,
            "last_gps_time": None,
            "last_offset_seconds": None,
            "interval_seconds": self.time_sync_interval_seconds,
            "min_offset_seconds": self.time_sync_min_offset_seconds,
            "min_valid_year": self.time_sync_min_valid_year,
        }
        self.parser = NMEAParser(
            validate_checksum=bool(gps_config.get("validate_checksum", True)),
            require_checksum=bool(gps_config.get("require_checksum", False)),
            retain_sentences=int(gps_config.get("retain_sentences", 25)),
            stale_after_seconds=float(gps_config.get("stale_after_seconds", 10.0)),
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_source_error: Optional[str] = None
        self._last_file_content: Optional[str] = None

    def start(self):
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return

        target = self._run_file_loop if self.source == "file" else self._run_serial_loop
        self._stop_event.clear()
        self._thread = threading.Thread(target=target, name="gps-service", daemon=True)
        self._thread.start()
        self._running = True
        logger.info("GPS service started using %s source", self.source)

    def stop(self, timeout: float = 2.0):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._running = False
        logger.info("GPS service stopped")

    def ingest_sentence(self, sentence: str) -> bool:
        accepted = self.parser.ingest_sentence(sentence)
        if accepted:
            self._maybe_sync_system_time()
            self._maybe_update_repeater_location()
        return accepted

    def get_summary(self) -> Dict[str, Any]:
        snapshot = self.get_snapshot()
        return {
            "enabled": snapshot["enabled"],
            "source": snapshot["source"],
            "status": snapshot["status"],
            "fix": {
                "valid": snapshot["fix"].get("valid"),
                "quality": snapshot["fix"].get("quality"),
                "quality_label": snapshot["fix"].get("quality_label"),
            },
            "position": snapshot["position"],
            "position_meta": snapshot.get("position_meta"),
            "gps_position": snapshot.get("gps_position"),
            "manual_position": snapshot.get("manual_position"),
            "time_sync": snapshot.get("time_sync"),
            "location_update": snapshot.get("location_update"),
            "satellites": {
                "used_count": snapshot["satellites"].get("used_count"),
                "in_view_count": snapshot["satellites"].get("in_view_count"),
                "snr": snapshot["satellites"].get("snr"),
            },
            "repeater_location": snapshot.get("repeater_location"),
        }

    def _apply_precision(self, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if self.location_precision_digits is None:
            return value
        return round(value, self.location_precision_digits)

    def _resolve_repeater_location(self, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fallback_lat = _to_float(self.repeater_config.get("latitude"))
        fallback_lon = _to_float(self.repeater_config.get("longitude"))
        fallback_location = {
            "latitude": fallback_lat if fallback_lat is not None else 0.0,
            "longitude": fallback_lon if fallback_lon is not None else 0.0,
            "source": "config",
            "advertise_gps_location": self.advertise_gps_location,
            "location_precision_digits": self.location_precision_digits,
        }

        if not self.advertise_gps_location:
            return fallback_location

        snapshot = snapshot or {}
        gps_fix_valid = bool(snapshot.get("status", {}).get("fix_valid"))
        gps_position = snapshot.get("gps_position") or {}
        gps_lat = _to_float(gps_position.get("latitude"))
        gps_lon = _to_float(gps_position.get("longitude"))

        if gps_fix_valid and _is_valid_latitude(gps_lat) and _is_valid_longitude(gps_lon):
            return {
                "latitude": self._apply_precision(gps_lat),
                "longitude": self._apply_precision(gps_lon),
                "source": "gps",
                "advertise_gps_location": True,
                "location_precision_digits": self.location_precision_digits,
            }

        return {
            **fallback_location,
            "source": "config_fallback_no_valid_gps_fix",
        }

    def get_repeater_location(self) -> Dict[str, Any]:
        """Return coordinates used for repeater-originated location fields.

        This is intentionally opt-in for GPS-derived coordinates so deployments
        can keep static site coordinates unless explicitly configured.
        """
        snapshot = self.parser.snapshot()
        self._apply_effective_position(snapshot)
        return self._resolve_repeater_location(snapshot)

    def get_snapshot(self) -> Dict[str, Any]:
        snapshot = self.parser.snapshot()
        self._apply_effective_position(snapshot)
        snapshot.update(
            {
                "enabled": self.enabled,
                "running": self._running and bool(self._thread and self._thread.is_alive()),
                "source": {
                    "type": self.source,
                    "device": self.device if self.source == "serial" else None,
                    "baud_rate": self.baud_rate if self.source == "serial" else None,
                    "source_path": self.source_path if self.source == "file" else None,
                    "read_timeout_seconds": self.read_timeout_seconds,
                    "poll_interval_seconds": self.poll_interval_seconds,
                    "stale_after_seconds": self.parser.stale_after_seconds,
                    "advertise_gps_location": self.advertise_gps_location,
                    "location_precision_digits": self.location_precision_digits,
                },
                "time_sync": self._get_time_sync_status(snapshot),
                "repeater_location": self._resolve_repeater_location(snapshot),
                "location_update": self._get_location_update_status(snapshot),
            }
        )
        if not self.enabled:
            snapshot["status"]["state"] = "disabled"
            snapshot["status"]["last_error"] = None
        elif self._last_source_error:
            snapshot["status"]["last_error"] = self._last_source_error
            if snapshot["status"]["state"] in ("no_data", "stale"):
                snapshot["status"]["state"] = "error"
        return snapshot

    def _get_location_update_status(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        with self._location_update_lock:
            status = deepcopy(self._location_update_status)

        if not self.enabled or not self.persist_gps_fix_enabled:
            status["state"] = "disabled"
            return status
        if self._location_update_callback is None:
            status["state"] = "unconfigured"
            return status
        if status.get("state") in ("updated", "error", "skipped"):
            return status
        if not snapshot.get("status", {}).get("fix_valid"):
            status["state"] = "waiting_for_fix"
        else:
            position = snapshot.get("gps_position") or snapshot.get("position") or {}
            latitude = _to_float(position.get("latitude"))
            longitude = _to_float(position.get("longitude"))
            if not _is_valid_latitude(latitude) or not _is_valid_longitude(longitude):
                status["state"] = "waiting_for_position"
            elif _is_zero_coordinate(latitude, longitude):
                status["state"] = "waiting_for_position"
            else:
                status["state"] = "ready"
        return status

    def _record_location_update_status(
        self,
        *,
        state: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        error: Optional[str] = None,
        success: bool = False,
    ):
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._location_update_lock:
            self._location_update_status.update(
                {
                    "enabled": self.persist_gps_fix_enabled,
                    "state": state,
                    "last_attempt": timestamp,
                    "last_error": error,
                    "last_latitude": latitude,
                    "last_longitude": longitude,
                    "interval_seconds": self.persist_gps_fix_interval_seconds,
                }
            )
            if success:
                self._location_update_status["last_success"] = timestamp

    def _maybe_update_repeater_location(self):
        if not self.enabled or not self.persist_gps_fix_enabled:
            return
        if self._location_update_callback is None:
            return

        now_monotonic = time.monotonic()
        with self._location_update_lock:
            if (
                self._last_location_update_monotonic is not None
                and now_monotonic - self._last_location_update_monotonic
                < self.persist_gps_fix_interval_seconds
            ):
                return

        snapshot = self.parser.snapshot()
        if not snapshot.get("status", {}).get("fix_valid"):
            return

        position = snapshot.get("position") or {}
        latitude = _to_float(position.get("latitude"))
        longitude = _to_float(position.get("longitude"))
        if not _is_valid_latitude(latitude) or not _is_valid_longitude(longitude):
            return
        if _is_zero_coordinate(latitude, longitude):
            return

        effective_latitude = self._apply_precision(latitude)
        effective_longitude = self._apply_precision(longitude)
        if not _is_valid_latitude(effective_latitude) or not _is_valid_longitude(
            effective_longitude
        ):
            return

        self._last_location_update_monotonic = now_monotonic
        payload = {
            "latitude": effective_latitude,
            "longitude": effective_longitude,
            "altitude_m": _to_float(position.get("altitude_m")),
            "fix": deepcopy(snapshot.get("fix") or {}),
            "status": deepcopy(snapshot.get("status") or {}),
            "time": deepcopy(snapshot.get("time") or {}),
            "location_precision_digits": self.location_precision_digits,
        }
        try:
            updated = bool(self._location_update_callback(payload))
        except Exception as exc:
            self._record_location_update_status(
                state="error",
                latitude=effective_latitude,
                longitude=effective_longitude,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning("GPS repeater location update failed: %s", exc)
            return

        self._record_location_update_status(
            state="updated" if updated else "skipped",
            latitude=effective_latitude,
            longitude=effective_longitude,
            success=updated,
        )

    @staticmethod
    def _extract_manual_position(repeater_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        latitude = _to_float(repeater_config.get("latitude"))
        longitude = _to_float(repeater_config.get("longitude"))
        if not _is_valid_latitude(latitude) or not _is_valid_longitude(longitude):
            return None
        # The packaged default is 0,0. Treat it as unset so a fresh install does
        # not show Null Island while the GPS is still searching for a fix.
        if _is_zero_coordinate(latitude, longitude):
            return None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "altitude_m": None,
            "geoid_separation_m": None,
            "source": "manual_config",
        }

    def _apply_effective_position(self, snapshot: Dict[str, Any]):
        gps_position = deepcopy(snapshot.get("position") or {})
        manual_position = deepcopy(self._extract_manual_position(self.repeater_config))
        gps_fix_valid = bool(snapshot.get("status", {}).get("fix_valid"))
        use_manual = (
            self.api_fallback_to_config_location
            and manual_position is not None
            and not gps_fix_valid
        )

        if use_manual:
            effective_position = {
                **gps_position,
                "latitude": manual_position["latitude"],
                "longitude": manual_position["longitude"],
            }
            position_source = "manual_config"
            position_source_label = "manual config until GPS fix"
        else:
            effective_position = gps_position
            position_source = "gps"
            position_source_label = "GPS fix" if gps_fix_valid else "GPS estimate"

        snapshot["gps_position"] = gps_position
        snapshot["manual_position"] = manual_position
        snapshot["position"] = effective_position
        snapshot["position_meta"] = {
            "source": position_source,
            "source_label": position_source_label,
            "policy": "fallback_to_config"
            if self.api_fallback_to_config_location
            else "gps_only",
            "manual_config_available": manual_position is not None,
            "gps_fix_valid": gps_fix_valid,
        }

    def _set_source_error(self, message: Optional[str]):
        self._last_source_error = message
        if message:
            self.parser.last_error = message
            logger.warning("GPS source error: %s", message)
        else:
            self.parser.last_error = None

    def _get_time_sync_status(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        with self._time_sync_lock:
            status = deepcopy(self._time_sync_status)

        if not self.enabled:
            status["state"] = "disabled"
            return status
        if not self.time_sync_enabled:
            status["state"] = "disabled"
            return status
        if status.get("state") in ("synced", "error", "in_sync", "ignored"):
            return status
        if not snapshot.get("status", {}).get("fix_valid"):
            status["state"] = "waiting_for_fix"
        elif not snapshot.get("time", {}).get("datetime_utc"):
            status["state"] = "waiting_for_time"
        else:
            status["state"] = "ready"
        return status

    def _record_time_sync_status(
        self,
        *,
        state: str,
        gps_time: Optional[datetime] = None,
        offset_seconds: Optional[float] = None,
        error: Optional[str] = None,
        success: bool = False,
    ):
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._time_sync_lock:
            self._time_sync_status.update(
                {
                    "enabled": self.time_sync_enabled,
                    "state": state,
                    "last_attempt": timestamp,
                    "last_error": error,
                    "last_gps_time": gps_time.isoformat() if gps_time else None,
                    "last_offset_seconds": (
                        round(offset_seconds, 3) if offset_seconds is not None else None
                    ),
                    "interval_seconds": self.time_sync_interval_seconds,
                    "min_offset_seconds": self.time_sync_min_offset_seconds,
                    "min_valid_year": self.time_sync_min_valid_year,
                }
            )
            if success:
                self._time_sync_status["last_success"] = timestamp

    def _maybe_sync_system_time(self):
        if not self.enabled or not self.time_sync_enabled:
            return

        now_monotonic = time.monotonic()
        with self._time_sync_lock:
            if (
                self._last_time_sync_monotonic is not None
                and now_monotonic - self._last_time_sync_monotonic
                < self.time_sync_interval_seconds
            ):
                return

        snapshot = self.parser.snapshot()
        if not snapshot.get("status", {}).get("fix_valid"):
            return

        gps_time = _parse_datetime_utc(snapshot.get("time", {}).get("datetime_utc"))
        if gps_time is None:
            return

        if gps_time.year < self.time_sync_min_valid_year:
            self._record_time_sync_status(
                state="ignored",
                gps_time=gps_time,
                error=(
                    f"GPS time year {gps_time.year} is older than "
                    f"minimum {self.time_sync_min_valid_year}"
                ),
            )
            return

        system_now = self._time_provider()
        offset_seconds = gps_time.timestamp() - system_now
        self._last_time_sync_monotonic = now_monotonic
        if abs(offset_seconds) < self.time_sync_min_offset_seconds:
            self._record_time_sync_status(
                state="in_sync",
                gps_time=gps_time,
                offset_seconds=offset_seconds,
                success=True,
            )
            return

        try:
            self._clock_setter(gps_time)
        except Exception as exc:
            self._record_time_sync_status(
                state="error",
                gps_time=gps_time,
                offset_seconds=offset_seconds,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning("GPS system time sync failed: %s", exc)
            return

        self._record_time_sync_status(
            state="synced",
            gps_time=gps_time,
            offset_seconds=offset_seconds,
            success=True,
        )
        self.parser.last_update = self._time_provider()
        logger.info(
            "System clock synchronized from GPS time %s (offset %.3fs)",
            gps_time.isoformat(),
            offset_seconds,
        )

    def _run_serial_loop(self):
        try:
            import serial  # type: ignore
        except ImportError:
            self._set_source_error("pyserial is not installed")
            self._running = False
            return

        while not self._stop_event.is_set():
            try:
                with serial.Serial(
                    self.device,
                    self.baud_rate,
                    timeout=self.read_timeout_seconds,
                ) as port:
                    self._set_source_error(None)
                    while not self._stop_event.is_set():
                        line = port.readline()
                        if not line:
                            continue
                        sentence = line.decode("ascii", errors="ignore").strip()
                        if sentence:
                            self.ingest_sentence(sentence)
            except Exception as exc:
                self._set_source_error(f"{type(exc).__name__}: {exc}")
                self._stop_event.wait(self.reconnect_interval_seconds)

        self._running = False

    def _run_file_loop(self):
        if not self.source_path:
            self._set_source_error("gps.source_path is required for file source")
            self._running = False
            return

        path = Path(self.source_path)
        while not self._stop_event.is_set():
            try:
                content = path.read_text(encoding="utf-8")
                if content != self._last_file_content:
                    self._last_file_content = content
                    self._set_source_error(None)
                    lines = self._extract_file_sentences(content)
                    for line in lines:
                        self.ingest_sentence(line)
            except FileNotFoundError:
                self._set_source_error(f"GPS source file not found: {path}")
            except Exception as exc:
                self._set_source_error(f"{type(exc).__name__}: {exc}")
            self._stop_event.wait(self.poll_interval_seconds)

        self._running = False

    @staticmethod
    def _extract_file_sentences(content: str) -> List[str]:
        stripped = content.strip()
        if not stripped:
            return []
        if stripped.startswith("{"):
            payload = json.loads(stripped)
            if isinstance(payload.get("sentences"), list):
                return [str(item) for item in payload["sentences"]]
            if payload.get("last_sentence"):
                return [str(payload["last_sentence"])]
            if payload.get("nmea"):
                nmea = payload["nmea"]
                if isinstance(nmea, dict) and nmea.get("last_sentence"):
                    return [str(nmea["last_sentence"])]
                if isinstance(nmea, list):
                    return [str(item) for item in nmea]
            return []
        return [line.strip() for line in stripped.splitlines() if line.strip()]
