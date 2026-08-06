"""Shared helpers for radio hardware presets (radio-settings.json).

Used by the first-run setup wizard and the authenticated
/api/update_radio_hardware_config endpoint so that preset fields are
mapped into the config in exactly one place. When a preset gains a new
field, only apply_hardware_preset() (plus the radio driver that consumes
the field) needs to learn it — the web UI applies presets by key and
never has to enumerate their fields.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..config import resolve_storage_dir

logger = logging.getLogger("HTTPServer")


def resolve_hardware_settings_path(config: dict, config_path: str | None) -> str:
    """Return the radio-settings.json path: installed copy first, then repo."""
    config_dir = resolve_storage_dir(config, config_path=config_path)
    installed_path = Path(config_dir) / "radio-settings.json"
    dev_path = os.path.join(os.path.dirname(__file__), "..", "..", "radio-settings.json")
    return str(installed_path) if installed_path.exists() else dev_path


def load_hardware_presets(config: dict, config_path: str | None) -> dict[str, dict]:
    """Load the hardware preset table from radio-settings.json.

    Returns {} when the file is missing (matching the historical
    hardware_options behavior). A file that exists but cannot be read or
    parsed raises OSError/ValueError instead of being swallowed: callers
    surface the error, so a corrupted file is distinguishable from an
    empty preset list. Non-dict entries are dropped so callers can trust
    the value shapes.
    """
    path = resolve_hardware_settings_path(config, config_path)
    if not os.path.exists(path):
        logger.debug(f"Hardware settings file not found: {path}")
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    hardware = data.get("hardware", {})
    if not isinstance(hardware, dict):
        return {}
    return {key: cfg for key, cfg in hardware.items() if isinstance(cfg, dict)}


# sx1262-section fields copied verbatim from a preset when present.
_SX1262_PRESET_FIELDS = (
    "bus_id",
    "cs_id",
    "reset_pin",
    "busy_pin",
    "irq_pin",
    "txen_pin",
    "rxen_pin",
    "en_pin",
    "en_pins",
    "cs_pin",
    "txled_pin",
    "rxled_pin",
    "use_dio3_tcxo",
    "dio3_tcxo_voltage",
    "use_dio2_rf",
    "is_waveshare",
    "gpio_chip",
    "use_gpiod_backend",
)


def apply_hardware_preset(
    config_yaml: dict,
    hw_config: dict,
    tx_power_override: int | None = None,
) -> None:
    """Apply a radio-settings.json hardware preset onto a config dict.

    Mutates config_yaml in place:
      * radio_type from the preset (defaults to "sx1262" — SPI presets
        historically omit the key);
      * ch341.vid/pid when the preset carries them;
      * radio.tx_power (tx_power_override wins when provided) and
        radio.preamble_length;
      * every known sx1262-section field present in the preset,
        including the ones the web UI has no widgets for
        (use_dio3_tcxo, use_dio2_rf, gpio backend, ...).

    Serial/network modem presets (pymc_usb / pymc_tcp / kiss) carry only
    radio_type + radio-section hints, so the sx1262 loop is a no-op for
    them; their port/host settings come from the caller (wizard request
    fields or endpoint overrides).
    """
    if "radio" not in config_yaml:
        config_yaml["radio"] = {}

    if "radio_type" in hw_config:
        config_yaml["radio_type"] = hw_config.get("radio_type")
    else:
        config_yaml["radio_type"] = "sx1262"

    ch341_cfg = hw_config.get("ch341") if isinstance(hw_config.get("ch341"), dict) else None
    vid = (ch341_cfg or {}).get("vid", hw_config.get("vid"))
    pid = (ch341_cfg or {}).get("pid", hw_config.get("pid"))
    if vid is not None or pid is not None:
        if "ch341" not in config_yaml:
            config_yaml["ch341"] = {}
        if vid is not None:
            config_yaml["ch341"]["vid"] = vid
        if pid is not None:
            config_yaml["ch341"]["pid"] = pid

    if tx_power_override is not None:
        config_yaml["radio"]["tx_power"] = tx_power_override
    elif "tx_power" in hw_config:
        config_yaml["radio"]["tx_power"] = hw_config.get("tx_power", 22)
    if "preamble_length" in hw_config:
        config_yaml["radio"]["preamble_length"] = hw_config.get("preamble_length", 32)

    if any(field in hw_config for field in _SX1262_PRESET_FIELDS):
        if "sx1262" not in config_yaml:
            config_yaml["sx1262"] = {}
        for field in _SX1262_PRESET_FIELDS:
            if field in hw_config:
                config_yaml["sx1262"][field] = hw_config.get(field)


# ─── Validation for /api/update_radio_hardware_config ───────────────

SUPPORTED_RADIO_TYPES = ("sx1262", "sx1262_ch341", "kiss", "pymc_usb", "pymc_tcp")

# Config section owned by each radio_type (what the endpoint may write).
SECTION_BY_RADIO_TYPE = {
    "kiss": "kiss",
    "pymc_usb": "pymc_usb",
    "pymc_tcp": "pymc_tcp",
    "sx1262": "sx1262",
    "sx1262_ch341": "sx1262",
}


def _require_int(section: str, field: str, value: Any, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{section}.{field} must be an integer")
    if number < lo or number > hi:
        raise ValueError(f"{section}.{field} must be between {lo} and {hi}")
    return number


def _validate_kiss(fields: dict) -> dict:
    out = {}
    if "port" in fields:
        port = str(fields["port"]).strip()
        if not port:
            raise ValueError("kiss.port must not be empty")
        out["port"] = port
    if "baud_rate" in fields:
        out["baud_rate"] = _require_int("kiss", "baud_rate", fields["baud_rate"], 1200, 4_000_000)
    return out


def _validate_pymc_usb(fields: dict) -> dict:
    out = {}
    if "port" in fields:
        port = str(fields["port"]).strip()
        if not port:
            raise ValueError("pymc_usb.port must not be empty")
        out["port"] = port
    if "baudrate" in fields:
        out["baudrate"] = _require_int("pymc_usb", "baudrate", fields["baudrate"], 1200, 4_000_000)
    return out


def _validate_pymc_tcp(fields: dict) -> dict:
    out = {}
    if "host" in fields:
        host = str(fields["host"]).strip()
        if not host:
            raise ValueError("pymc_tcp.host must not be empty")
        out["host"] = host
    if "port" in fields:
        out["port"] = _require_int("pymc_tcp", "port", fields["port"], 1, 65535)
    if "token" in fields:
        out["token"] = str(fields["token"])
    return out


_SX1262_ID_FIELDS = ("bus_id", "cs_id")
_SX1262_PIN_FIELDS = (
    "cs_pin",
    "reset_pin",
    "busy_pin",
    "irq_pin",
    "txen_pin",
    "rxen_pin",
    "en_pin",
    "txled_pin",
    "rxled_pin",
)


def _validate_sx1262(fields: dict) -> dict:
    out = {}
    for field in _SX1262_ID_FIELDS:
        if field in fields:
            out[field] = _require_int("sx1262", field, fields[field], 0, 15)
    for field in _SX1262_PIN_FIELDS:
        if field in fields:
            out[field] = _require_int("sx1262", field, fields[field], -1, 255)
    if "en_pins" in fields:
        pins = fields["en_pins"]
        if not isinstance(pins, list):
            raise ValueError("sx1262.en_pins must be a list of integers")
        out["en_pins"] = [_require_int("sx1262", "en_pins", pin, 0, 255) for pin in pins]
    return out


def _validate_ch341(fields: dict) -> dict:
    out = {}
    for field in ("vid", "pid"):
        if field in fields:
            out[field] = _require_int("ch341", field, fields[field], 0, 65535)
    return out


SECTION_VALIDATORS = {
    "kiss": _validate_kiss,
    "pymc_usb": _validate_pymc_usb,
    "pymc_tcp": _validate_pymc_tcp,
    "sx1262": _validate_sx1262,
    "ch341": _validate_ch341,
}


def sections_allowed_for(radio_type: str) -> set:
    """Sections the endpoint may accept for a given radio_type."""
    allowed = {SECTION_BY_RADIO_TYPE[radio_type]}
    if radio_type == "sx1262_ch341":
        allowed.add("ch341")
    return allowed


def validate_section(section: str, fields: Any) -> dict:
    """Validate one section payload; raises ValueError with a user-facing
    message, returns the sanitized field dict."""
    if not isinstance(fields, dict):
        # ValueError on purpose (not TypeError): the endpoint maps
        # ValueError to a user-facing 200 {"success": false, "error": ...}.
        raise ValueError(f"'{section}' must be an object")  # noqa: TRY004
    validator = SECTION_VALIDATORS.get(section)
    if validator is None:
        raise ValueError(f"Unsupported section '{section}'")
    return validator(fields)
