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
    return config


def normalize_modem_config(config: dict, *, warn: bool = True) -> dict:
    """Return a deep-copied canonical runtime config; canonical values win conflicts."""
    if not isinstance(config, dict):
        return config

    normalized = copy.deepcopy(config)
    normalize_modem_config_in_place(normalized, warn=warn)
    return normalized
