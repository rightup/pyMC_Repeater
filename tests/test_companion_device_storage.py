"""Storage-layer tests for the Mobile Companion API phase 2 (device pairing
and send idempotency).

Covers the ``companion_devices`` / ``companion_idempotency`` migration, the
device CRUD/touch/delete methods, the idempotency get/put/prune methods, and
the ``api_tokens.scope`` column (including NULL-scope legacy-token backward
compatibility). See docs/architecture/mobile-companion-api.md §5.4 (schema)
and §11.1 (token scope model).
"""

from __future__ import annotations

import sqlite3
import time

from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"
_MIGRATION = "add_companion_devices_and_idempotency"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


# --- Migration -----------------------------------------------------------


def test_migration_applies_once_and_is_idempotent(tmp_path):
    h1 = _handler(tmp_path)

    conn = sqlite3.connect(str(h1.sqlite_path))
    applied = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE migration_name = ?", (_MIGRATION,)
    ).fetchone()[0]
    assert applied == 1

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('companion_devices', 'companion_idempotency')"
        )
    }
    assert tables == {"companion_devices", "companion_idempotency"}

    columns = [row[1] for row in conn.execute("PRAGMA table_info(api_tokens)")]
    assert "scope" in columns
    conn.close()

    # Re-opening the same DB file re-runs migrations; must not error or
    # duplicate the migration row / re-create tables.
    h2 = _handler(tmp_path)
    conn = sqlite3.connect(str(h2.sqlite_path))
    applied = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE migration_name = ?", (_MIGRATION,)
    ).fetchone()[0]
    assert applied == 1
    conn.close()

    # Functional smoke check post-reopen.
    token_id = h2.create_api_token("t", "hash-smoke")
    assert h2.companion_device_create(_HASH, "dev-smoke", "Phone", token_id) is not None


def test_companion_devices_id_is_autoincrement(tmp_path):
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_devices'"
    ).fetchone()[0]
    conn.close()
    assert "AUTOINCREMENT" in sql.upper()


def test_companion_devices_device_id_is_unique(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-unique")
    assert h.companion_device_create(_HASH, "dup-device", "Phone A", token_id) is not None

    # A second row with the same device_id must fail (UNIQUE constraint) --
    # create() catches the IntegrityError and returns None rather than
    # raising, matching the file's error contract.
    assert h.companion_device_create(_HASH, "dup-device", "Phone B", token_id) is None


def test_idempotency_table_primary_key_is_device_and_key(tmp_path):
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_idempotency'"
    ).fetchone()[0]
    conn.close()
    assert "PRIMARY KEY (device_id, idempotency_key)" in sql


# --- Idempotency: put/get/duplicate/prune ---------------------------------


def test_idempotency_get_missing_returns_none(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_get("dev-1", "key-1") is None


def test_idempotency_put_then_get_round_trip(tmp_path):
    h = _handler(tmp_path)
    ok = h.companion_idempotency_put(
        "dev-1", "key-1", "hash-abc", '{"message_id": 7}'
    )
    assert ok is True

    record = h.companion_idempotency_get("dev-1", "key-1")
    assert record is not None
    assert record["request_hash"] == "hash-abc"
    assert record["response_json"] == '{"message_id": 7}'
    assert isinstance(record["created_at"], float)


def test_idempotency_duplicate_put_returns_false_and_leaves_original(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_put("dev-1", "key-1", "hash-abc", "{}") is True

    # A second put for the same (device_id, idempotency_key) -- e.g. a
    # concurrent retry racing the first -- must not raise, must report
    # False, and must not overwrite the stored response.
    assert h.companion_idempotency_put("dev-1", "key-1", "hash-xyz", '{"other": 1}') is False

    record = h.companion_idempotency_get("dev-1", "key-1")
    assert record["request_hash"] == "hash-abc"
    assert record["response_json"] == "{}"


def test_idempotency_scoped_to_device_id(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_put("dev-1", "key-1", "hash-a", '{"who": "1"}')
    assert h.companion_idempotency_put("dev-2", "key-1", "hash-b", '{"who": "2"}')

    rec1 = h.companion_idempotency_get("dev-1", "key-1")
    rec2 = h.companion_idempotency_get("dev-2", "key-1")
    assert rec1["response_json"] == '{"who": "1"}'
    assert rec2["response_json"] == '{"who": "2"}'


def test_idempotency_prune_deletes_rows_older_than_48h_boundary(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_put("dev-1", "old-key", "hash-1", "{}")
    assert h.companion_idempotency_put("dev-1", "fresh-key", "hash-2", "{}")

    # Backdate only the "old" row to just past the 48h boundary.
    old_time = time.time() - (48 * 3600 + 60)
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "UPDATE companion_idempotency SET created_at = ? WHERE idempotency_key = 'old-key'",
        (old_time,),
    )
    conn.commit()
    conn.close()

    deleted = h.companion_idempotency_prune()
    assert deleted == 1
    assert h.companion_idempotency_get("dev-1", "old-key") is None
    assert h.companion_idempotency_get("dev-1", "fresh-key") is not None


def test_idempotency_prune_respects_custom_max_age(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_put("dev-1", "key-1", "hash-1", "{}")

    ten_min_ago = time.time() - 600
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "UPDATE companion_idempotency SET created_at = ?", (ten_min_ago,)
    )
    conn.commit()
    conn.close()

    # Default 48h retention: row survives.
    assert h.companion_idempotency_prune() == 0
    # A tighter custom window (5 min) catches it.
    assert h.companion_idempotency_prune(max_age_seconds=300) == 1


def test_cleanup_old_data_wires_idempotency_prune(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_idempotency_put("dev-1", "old-key", "hash-1", "{}")

    old_time = time.time() - (49 * 3600)
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_idempotency SET created_at = ?", (old_time,))
    conn.commit()
    conn.close()

    h.cleanup_old_data(days=31)
    assert h.companion_idempotency_get("dev-1", "old-key") is None


# --- Device CRUD, touch, delete, get_by_token -----------------------------


def test_device_create_and_get(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("Adam's iPhone", "hash-1")
    device_id = h.companion_device_create(
        _HASH, "device-uuid-1", "Adam's iPhone", token_id, platform="ios"
    )
    assert device_id is not None

    device = h.companion_device_get("device-uuid-1")
    assert device is not None
    assert device["id"] == device_id
    assert device["companion_hash"] == _HASH
    assert device["device_id"] == "device-uuid-1"
    assert device["name"] == "Adam's iPhone"
    assert device["token_id"] == token_id
    assert device["platform"] == "ios"
    assert device["push_token"] is None
    assert device["push_relay_url"] is None
    assert isinstance(device["created_at"], float)
    assert device["last_seen"] is None
    assert device["last_synced_seq"] is None


def test_device_get_missing_returns_none(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_device_get("nonexistent") is None


def test_device_create_with_push_relay_url(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-2")
    h.companion_device_create(
        _HASH,
        "device-uuid-2",
        "Phone",
        token_id,
        platform="android",
        push_relay_url="https://relay.example/fcm",
    )
    device = h.companion_device_get("device-uuid-2")
    assert device["push_relay_url"] == "https://relay.example/fcm"
    assert device["platform"] == "android"


def test_device_get_by_token(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-3")
    h.companion_device_create(_HASH, "device-uuid-3", "Phone", token_id)

    device = h.companion_device_get_by_token(token_id)
    assert device is not None
    assert device["device_id"] == "device-uuid-3"

    assert h.companion_device_get_by_token(999999) is None


def test_device_list_all_and_filtered_by_companion_hash(tmp_path):
    h = _handler(tmp_path)
    t1 = h.create_api_token("t1", "hash-4")
    t2 = h.create_api_token("t2", "hash-5")
    t3 = h.create_api_token("t3", "hash-6")
    h.companion_device_create("0x01", "dev-a", "A", t1)
    h.companion_device_create("0x01", "dev-b", "B", t2)
    h.companion_device_create("0x02", "dev-c", "C", t3)

    all_devices = h.companion_device_list()
    assert {d["device_id"] for d in all_devices} == {"dev-a", "dev-b", "dev-c"}

    scoped = h.companion_device_list(companion_hash="0x01")
    assert {d["device_id"] for d in scoped} == {"dev-a", "dev-b"}

    scoped2 = h.companion_device_list(companion_hash="0x02")
    assert {d["device_id"] for d in scoped2} == {"dev-c"}

    scoped_none = h.companion_device_list(companion_hash="0x99")
    assert scoped_none == []


def test_device_touch_updates_last_seen_and_last_synced_seq(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-7")
    h.companion_device_create(_HASH, "device-uuid-7", "Phone", token_id)

    assert h.companion_device_touch("device-uuid-7", last_seen=1000.0, last_synced_seq=42) is True

    device = h.companion_device_get("device-uuid-7")
    assert device["last_seen"] == 1000.0
    assert device["last_synced_seq"] == 42


def test_device_touch_only_updates_provided_fields(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-8")
    h.companion_device_create(_HASH, "device-uuid-8", "Phone", token_id)

    h.companion_device_touch("device-uuid-8", last_seen=500.0, last_synced_seq=10)
    # Only bump last_seen this time; last_synced_seq must be untouched.
    h.companion_device_touch("device-uuid-8", last_seen=600.0)

    device = h.companion_device_get("device-uuid-8")
    assert device["last_seen"] == 600.0
    assert device["last_synced_seq"] == 10


def test_device_touch_bare_call_bumps_last_seen_to_now(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-9")
    h.companion_device_create(_HASH, "device-uuid-9", "Phone", token_id)

    before = time.time()
    assert h.companion_device_touch("device-uuid-9") is True
    after = time.time()

    device = h.companion_device_get("device-uuid-9")
    assert before <= device["last_seen"] <= after


def test_device_touch_missing_device_returns_false(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_device_touch("nonexistent", last_seen=1.0) is False


def test_device_delete(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("t", "hash-10")
    h.companion_device_create(_HASH, "device-uuid-10", "Phone", token_id)

    assert h.companion_device_get("device-uuid-10") is not None
    assert h.companion_device_delete("device-uuid-10") is True
    assert h.companion_device_get("device-uuid-10") is None

    # Deleting again (already gone) reports False, doesn't error.
    assert h.companion_device_delete("device-uuid-10") is False


# --- Token scope round-trip -------------------------------------------------


def test_token_created_with_scope_round_trips_through_verify(tmp_path):
    h = _handler(tmp_path)
    token_id = h.create_api_token("Adam's iPhone", "hash-scoped", scope="companion:home")

    verified = h.verify_api_token("hash-scoped")
    assert verified is not None
    assert verified["id"] == token_id
    assert verified["scope"] == "companion:home"


def test_token_created_with_wildcard_companion_scope(tmp_path):
    h = _handler(tmp_path)
    h.create_api_token("Any-companion device", "hash-wildcard", scope="companion:*")

    verified = h.verify_api_token("hash-wildcard")
    assert verified["scope"] == "companion:*"


def test_legacy_token_with_null_scope_verifies_as_admin(tmp_path):
    h = _handler(tmp_path)
    # create_api_token with no scope argument -- the legacy call pattern
    # every existing caller uses -- stores NULL.
    h.create_api_token("Legacy web-ui token", "hash-legacy")

    verified = h.verify_api_token("hash-legacy")
    assert verified is not None
    assert verified["scope"] == "admin"


def test_token_created_with_explicit_admin_scope_round_trips(tmp_path):
    h = _handler(tmp_path)
    h.create_api_token("Admin token", "hash-admin", scope="admin")

    verified = h.verify_api_token("hash-admin")
    assert verified["scope"] == "admin"


def test_list_api_tokens_includes_scope_with_null_defaulted_to_admin(tmp_path):
    h = _handler(tmp_path)
    h.create_api_token("legacy", "hash-list-legacy")
    h.create_api_token("scoped", "hash-list-scoped", scope="companion:home")

    tokens = {t["name"]: t for t in h.list_api_tokens()}
    assert tokens["legacy"]["scope"] == "admin"
    assert tokens["scoped"]["scope"] == "companion:home"


def test_row_written_directly_with_null_scope_verifies_as_admin(tmp_path):
    # Simulate a token that existed before this migration ran: a raw INSERT
    # with no scope column touched at all (column defaults to NULL).
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "INSERT INTO api_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
        ("pre-migration token", "hash-premigration", time.time()),
    )
    conn.commit()
    conn.close()

    verified = h.verify_api_token("hash-premigration")
    assert verified is not None
    assert verified["scope"] == "admin"


# --- Push registration (phase 4, migration 17) ---------------------------

_PUSH_MIGRATION = "add_companion_device_push_detail"


def _device_with_token(h, device_id="push-dev", token_hash="hash-push"):
    token_id = h.create_api_token("t", token_hash)
    h.companion_device_create(_HASH, device_id, "Phone", token_id, platform="ios")
    return device_id


def test_push_detail_migration_applies_once(tmp_path):
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    applied = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE migration_name = ?", (_PUSH_MIGRATION,)
    ).fetchone()[0]
    assert applied == 1
    cols = {row[1] for row in conn.execute("PRAGMA table_info(companion_devices)")}
    assert "push_detail" in cols


def test_push_detail_defaults_to_none(tmp_path):
    h = _handler(tmp_path)
    device_id = _device_with_token(h)
    assert h.companion_device_get(device_id)["push_detail"] == "none"


def test_set_push_registers_token_relay_and_detail(tmp_path):
    h = _handler(tmp_path)
    device_id = _device_with_token(h)
    ok = h.companion_device_set_push(
        device_id, "apns-token-abc", push_relay_url="https://relay.example/notify",
        push_detail="preview",
    )
    assert ok is True
    device = h.companion_device_get(device_id)
    assert device["push_token"] == "apns-token-abc"
    assert device["push_relay_url"] == "https://relay.example/notify"
    assert device["push_detail"] == "preview"


def test_set_push_token_refresh_leaves_relay_and_detail(tmp_path):
    h = _handler(tmp_path)
    device_id = _device_with_token(h)
    h.companion_device_set_push(
        device_id, "tok-1", push_relay_url="https://relay.example/notify",
        push_detail="count",
    )
    # Refresh only the token; omit relay/detail.
    h.companion_device_set_push(device_id, "tok-2")
    device = h.companion_device_get(device_id)
    assert device["push_token"] == "tok-2"
    assert device["push_relay_url"] == "https://relay.example/notify"
    assert device["push_detail"] == "count"


def test_clear_push_nulls_token_but_keeps_relay(tmp_path):
    h = _handler(tmp_path)
    device_id = _device_with_token(h)
    h.companion_device_set_push(
        device_id, "tok", push_relay_url="https://relay.example/notify",
        push_detail="count",
    )
    assert h.companion_device_clear_push(device_id) is True
    device = h.companion_device_get(device_id)
    assert device["push_token"] is None
    assert device["push_relay_url"] == "https://relay.example/notify"
    assert device["push_detail"] == "count"


def test_devices_with_push_only_returns_registered(tmp_path):
    h = _handler(tmp_path)
    with_push = _device_with_token(h, "dev-push", "hash-a")
    _device_with_token(h, "dev-nopush", "hash-b")  # never registers a token
    h.companion_device_set_push(with_push, "tok", push_relay_url="https://r.example")

    rows = h.companion_devices_with_push(_HASH)
    assert [d["device_id"] for d in rows] == [with_push]


def test_devices_with_push_scoped_to_companion(tmp_path):
    h = _handler(tmp_path)
    device_id = _device_with_token(h, "dev-other-companion", "hash-c")
    h.companion_device_set_push(device_id, "tok", push_relay_url="https://r.example")
    # A different companion hash sees nothing.
    assert h.companion_devices_with_push("0x99") == []


def test_set_push_missing_device_returns_false(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_device_set_push("no-such-device", "tok") is False
