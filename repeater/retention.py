"""Strict, shared validation for destructive data-retention settings."""

from __future__ import annotations

import math
from typing import Any, Mapping

DEFAULT_RETENTION_DAYS = 31
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 36_500

_MISSING = object()


def validate_retention_days(value: Any, field_name: str) -> int:
    """Return one explicit retention period without coercing YAML types."""

    if type(value) is not int or value < MIN_RETENTION_DAYS or value > MAX_RETENTION_DAYS:
        raise ValueError(
            f"{field_name} must be an integer between "
            f"{MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS} days"
        )
    return value


def storage_retention_days(config: Mapping[str, Any]) -> tuple[int, int]:
    """Return validated packet and companion-history retention periods."""

    if not isinstance(config, Mapping):
        raise ValueError("configuration must be an object")

    storage = config.get("storage", _MISSING)
    if storage is _MISSING:
        storage = {}
    elif not isinstance(storage, Mapping):
        raise ValueError("storage must be an object")

    retention = storage.get("retention", _MISSING)
    if retention is _MISSING:
        retention = {}
    elif not isinstance(retention, Mapping):
        raise ValueError("storage.retention must be an object")

    return (
        validate_retention_days(
            retention.get("sqlite_cleanup_days", DEFAULT_RETENTION_DAYS),
            "storage.retention.sqlite_cleanup_days",
        ),
        validate_retention_days(
            retention.get("companion_events_days", DEFAULT_RETENTION_DAYS),
            "storage.retention.companion_events_days",
        ),
    )


def validate_positive_seconds(value: Any, field_name: str) -> float:
    """Return a finite positive duration without coercing strings or booleans."""

    if type(value) not in (int, float):
        raise ValueError(f"{field_name} must be a finite number greater than zero")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise ValueError(f"{field_name} must be a finite number greater than zero")
    return seconds
