"""Compatibility boundary for openHop Modem transport configuration names."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

LEGACY_MODEM_RADIO_TYPES = {
    "pymc_tcp": "modem_tcp",
    "pymc_usb": "modem_usb",
}

LEGACY_MODEM_SENSOR_TYPES = {
    "pymc_modem": "openhop_modem",
}

LEGACY_MODEM_GPS_SOURCES = {
    "pymc_modem": "modem_http",
}


def _normalize_board_config(config: dict[str, Any], *, warn: bool) -> None:
    radio_type = config.get("radio_type")
    if isinstance(radio_type, str):
        normalized_type = radio_type.lower().strip()
        if normalized_type in LEGACY_MODEM_RADIO_TYPES:
            config["radio_type"] = LEGACY_MODEM_RADIO_TYPES[normalized_type]

    for legacy_key, canonical_key in LEGACY_MODEM_RADIO_TYPES.items():
        if legacy_key not in config:
            continue
        if canonical_key in config:
            if warn:
                logger.warning(
                    "Both legacy configuration key '%s' and canonical key '%s' are present; canonical key wins",
                    legacy_key,
                    canonical_key,
                )
        else:
            config[canonical_key] = config[legacy_key]
        del config[legacy_key]


def _normalize_sensor_and_gps_aliases(config: dict[str, Any]) -> None:
    sensors = config.get("sensors")
    if isinstance(sensors, dict):
        for definitions_key in ("definitions", "sensors"):
            definitions = sensors.get(definitions_key)
            if not isinstance(definitions, list):
                continue
            for definition in definitions:
                if not isinstance(definition, dict):
                    continue
                sensor_type = definition.get("type")
                if isinstance(sensor_type, str):
                    normalized_type = sensor_type.strip().lower()
                    canonical_type = LEGACY_MODEM_SENSOR_TYPES.get(normalized_type)
                    if canonical_type is not None:
                        definition["type"] = canonical_type

    gps = config.get("gps")
    if isinstance(gps, dict):
        source = gps.get("source")
        if isinstance(source, str):
            normalized_source = source.strip().lower()
            canonical_source = LEGACY_MODEM_GPS_SOURCES.get(normalized_source)
            if canonical_source is not None:
                gps["source"] = canonical_source


def redact_modem_tokens_in_place(config: dict, *, replacement: Any = None) -> dict:
    """Remove or replace modem TCP tokens in top-level and multi-radio sections."""
    if not isinstance(config, dict):
        return config

    sections = [config.get("modem_tcp")]
    radios = config.get("radios")
    if isinstance(radios, list):
        sections.extend(entry.get("modem_tcp") for entry in radios if isinstance(entry, dict))

    for section in sections:
        if not isinstance(section, dict) or "token" not in section:
            continue
        if replacement is None:
            section.pop("token", None)
        else:
            section["token"] = replacement
    return config


def normalize_modem_config_in_place(config: dict, *, warn: bool = True) -> dict:
    """Canonicalize modem transport keys in place without replacing unrelated objects."""
    if not isinstance(config, dict):
        return config

    _normalize_board_config(config, warn=warn)
    radios = config.get("radios")
    if isinstance(radios, list):
        for entry in radios:
            if isinstance(entry, dict):
                _normalize_board_config(entry, warn=warn)
    _normalize_sensor_and_gps_aliases(config)
    return config


def normalize_modem_config(config: dict, *, warn: bool = True) -> dict:
    """Return a deep-copied canonical runtime config; canonical values win conflicts."""
    if not isinstance(config, dict):
        return config

    normalized = copy.deepcopy(config)
    normalize_modem_config_in_place(normalized, warn=warn)
    return normalized
