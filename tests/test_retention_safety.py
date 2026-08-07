"""Fail-safe validation for destructive packet and companion retention."""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock, patch

import pytest

from repeater.config import load_config
from repeater.companion.rf_window import packets_retention_days
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.retention import (
    DEFAULT_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    storage_retention_days,
    validate_positive_seconds,
    validate_retention_days,
)

_HASH = "0x01"
_INVALID_DAYS = (-1, 0, True, "31", 31.0, MAX_RETENTION_DAYS + 1)


def _row_count(handler: SQLiteHandler, table: str) -> int:
    with handler._connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_cleanup_rows(handler: SQLiteHandler) -> None:
    handler.store_packet(
        {
            "timestamp": time.time(),
            "type": 1,
            "route": 1,
            "length": 1,
            "packet_hash": "packet-retention-test",
        }
    )
    handler.companion_append_event(_HASH, "message", {})
    assert handler.companion_push_message(
        _HASH,
        {
            "sender_key": b"\x11" * 32,
            "timestamp": int(time.time()),
            "text": "retention test",
            "packet_hash": "1111111111111111",
        },
    )
    assert handler.companion_pop_message(_HASH) is not None


def test_retention_defaults_and_boundaries_are_explicit():
    assert storage_retention_days({}) == (
        DEFAULT_RETENTION_DAYS,
        DEFAULT_RETENTION_DAYS,
    )
    assert validate_retention_days(1, "retention") == 1
    assert validate_retention_days(MAX_RETENTION_DAYS, "retention") == (MAX_RETENTION_DAYS)


@pytest.mark.parametrize("value", _INVALID_DAYS)
def test_retention_days_rejects_coercion_and_unsafe_ranges(value):
    with pytest.raises(ValueError, match="integer between 1 and 36500"):
        validate_retention_days(value, "retention")


@pytest.mark.parametrize(
    "config, message",
    [
        ({"storage": None}, "storage must be an object"),
        ({"storage": []}, "storage must be an object"),
        (
            {"storage": {"retention": None}},
            "storage.retention must be an object",
        ),
        (
            {"storage": {"retention": []}},
            "storage.retention must be an object",
        ),
    ],
)
def test_retention_sections_must_be_objects(config, message):
    with pytest.raises(ValueError, match=message):
        storage_retention_days(config)


def test_rf_retention_rejects_a_falsy_non_object_config():
    with pytest.raises(ValueError, match="configuration must be an object"):
        packets_retention_days([])  # type: ignore[arg-type]


def test_load_config_normalizes_retention_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "repeater:\n  identity_key: explicit\n",
        encoding="utf-8",
    )

    config = load_config(str(config_path))

    assert config["storage"]["retention"] == {
        "sqlite_cleanup_days": 31,
        "companion_events_days": 31,
    }


@pytest.mark.parametrize(
    "yaml_text",
    [
        "storage: []\n",
        "storage:\n  retention: []\n",
        "storage:\n  retention:\n    sqlite_cleanup_days: -1\n",
        "storage:\n  retention:\n    companion_events_days: true\n",
    ],
)
def test_load_config_rejects_malformed_retention_before_startup(tmp_path, yaml_text):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml_text + "repeater:\n  identity_key: explicit\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(str(config_path))


@pytest.mark.parametrize("field", ["sqlite_cleanup_days", "companion_events_days"])
@pytest.mark.parametrize("value", _INVALID_DAYS)
def test_engine_rejects_invalid_retention_at_construction(field, value):
    from repeater.engine import RepeaterHandler

    config = {"storage": {"retention": {field: value}}}
    with (
        patch("repeater.engine.StorageCollector") as storage_collector,
        patch.object(RepeaterHandler, "_start_background_tasks"),
        pytest.raises(ValueError),
    ):
        RepeaterHandler(config, MagicMock(), 1)
    storage_collector.assert_not_called()


@pytest.mark.parametrize("bad_days", _INVALID_DAYS)
def test_cleanup_rejects_invalid_packet_retention_before_any_delete(
    tmp_path,
    bad_days,
):
    handler = SQLiteHandler(tmp_path)
    _seed_cleanup_rows(handler)
    before = {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    }

    with pytest.raises(ValueError):
        handler.cleanup_old_data(
            days=bad_days,
            companion_events_days=DEFAULT_RETENTION_DAYS,
        )

    assert {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    } == before


@pytest.mark.parametrize("bad_days", _INVALID_DAYS)
def test_cleanup_rejects_invalid_companion_retention_before_any_delete(
    tmp_path,
    bad_days,
):
    handler = SQLiteHandler(tmp_path)
    _seed_cleanup_rows(handler)
    before = {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    }

    with pytest.raises(ValueError):
        handler.cleanup_old_data(
            days=DEFAULT_RETENTION_DAYS,
            companion_events_days=bad_days,
        )

    assert {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    } == before


@pytest.mark.parametrize("bad_days", _INVALID_DAYS)
def test_low_level_companion_pruners_reject_before_delete(tmp_path, bad_days):
    handler = SQLiteHandler(tmp_path)
    _seed_cleanup_rows(handler)
    before_events = _row_count(handler, "companion_events")
    before_messages = _row_count(handler, "companion_messages")

    with pytest.raises(ValueError):
        handler.companion_prune_events(bad_days)
    with pytest.raises(ValueError):
        handler.companion_prune_consumed_messages(bad_days)

    assert _row_count(handler, "companion_events") == before_events
    assert _row_count(handler, "companion_messages") == before_messages


@pytest.mark.parametrize(
    "value",
    [0, -1, True, "172800", math.nan, math.inf, -math.inf],
)
def test_idempotency_prune_rejects_unsafe_age_and_preserves_replay(
    tmp_path,
    value,
):
    handler = SQLiteHandler(tmp_path)
    assert handler.companion_idempotency_put(
        "device-a",
        "send-a",
        "request-a",
        '{"result":"complete"}',
    )

    with pytest.raises(ValueError, match="finite number greater than zero"):
        handler.companion_idempotency_prune(value)

    assert handler.companion_idempotency_get("device-a", "send-a") is not None


def test_positive_seconds_accepts_real_finite_numbers():
    assert validate_positive_seconds(1, "seconds") == 1.0
    assert validate_positive_seconds(0.5, "seconds") == 0.5


def test_default_cleanup_retains_recent_rows(tmp_path):
    handler = SQLiteHandler(tmp_path)
    _seed_cleanup_rows(handler)
    before = {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    }

    handler.cleanup_old_data()

    assert {
        table: _row_count(handler, table)
        for table in ("packets", "companion_events", "companion_messages")
    } == before


def test_cleanup_database_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    handler = SQLiteHandler(tmp_path)

    def unavailable():
        raise OSError("storage unavailable")

    monkeypatch.setattr(handler, "_connect", unavailable)

    with pytest.raises(OSError, match="storage unavailable"):
        handler.cleanup_old_data()
