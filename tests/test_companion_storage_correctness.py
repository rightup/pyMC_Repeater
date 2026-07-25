"""Correctness tests for the shared frame/REST companion persistence model."""

from __future__ import annotations

import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import (
    CompanionNamespaceCollisionError,
    CompanionStorageError,
    SQLiteHandler,
)

_HASH = "0x01"
_OTHER_HASH = "0x02"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


def _inbound(packet_hash: str, *, direct: bool = False) -> dict:
    return {
        "sender_key": b"\x11" * 32,
        "text": packet_hash,
        "timestamp": 1000,
        "txt_type": 0,
        "is_channel": not direct,
        "channel_idx": 0,
        "path_len": 0,
        "packet_hash": packet_hash,
    }


def test_correctness_migration_is_idempotent(tmp_path):
    _handler(tmp_path)
    _handler(tmp_path)

    conn = sqlite3.connect(tmp_path / "repeater.db")
    applied = conn.execute(
        """
        SELECT COUNT(*) FROM migrations
        WHERE migration_name = 'companion_api_correctness_primitives'
        """
    ).fetchone()[0]
    message_columns = {row[1] for row in conn.execute("PRAGMA table_info(companion_messages)")}
    idempotency_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(companion_idempotency)")
    }
    dedup_index_sql = conn.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'index'
          AND name = 'idx_companion_messages_dedup'
        """
    ).fetchone()[0]
    scoped_dedup_applied = conn.execute(
        """
        SELECT COUNT(*) FROM migrations
        WHERE migration_name = 'scope_companion_message_dedup_to_inbound'
        """
    ).fetchone()[0]
    conn.close()

    assert applied == 1
    assert {
        "direction",
        "state",
        "recipient_key",
        "expected_ack",
        "source",
        "pending_for_frame",
    } <= message_columns
    assert {
        "principal_type",
        "principal_id",
        "state",
        "updated_at",
        "message_id",
        "packet_hash",
        "expected_ack",
    } <= idempotency_columns
    assert "direction = 'in'" in dedup_index_sql
    assert scoped_dedup_applied == 1


def test_concurrent_handler_startup_serializes_migrations(tmp_path):
    start = threading.Barrier(9)

    def open_handler(_unused):
        start.wait()
        return _handler(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(open_handler, number) for number in range(8)]
        start.wait()
        handlers = [future.result() for future in futures]

    assert len(handlers) == 8
    conn = sqlite3.connect(tmp_path / "repeater.db")
    duplicate_markers = conn.execute(
        """
        SELECT migration_name, COUNT(*)
        FROM migrations
        GROUP BY migration_name
        HAVING COUNT(*) != 1
        """
    ).fetchall()
    conn.close()
    assert duplicate_markers == []


def test_database_and_wal_sidecars_remain_owner_only_across_reopen(tmp_path):
    handler = _handler(tmp_path)
    assert handler.companion_save_prefs(_HASH, {"node_name": "first"})

    def assert_private_files():
        for name in (
            "repeater.db",
            "repeater.db-wal",
            "repeater.db-shm",
            ".companion-journal-lineage",
        ):
            path = tmp_path / name
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert_private_files()
    conn = handler._connect()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert_private_files()
    conn.close()
    del handler._local.conn
    assert_private_files()

    reopened = _handler(tmp_path)
    assert reopened.companion_save_prefs(_HASH, {"node_name": "second"})
    assert_private_files()


def test_contact_unique_migration_keeps_newest_legacy_duplicate(tmp_path):
    handler = _handler(tmp_path)
    contact_key = b"\x44" * 32
    assert handler.companion_upsert_contact(
        _HASH,
        {"pubkey": contact_key, "name": "older"},
    )

    with handler._connect() as conn:
        conn.execute("DROP INDEX idx_companion_contacts_hash_pubkey")
        conn.execute(
            """
            DELETE FROM migrations
            WHERE migration_name = 'unique_companion_contacts_pubkey'
            """
        )
        conn.execute(
            """
            INSERT INTO companion_contacts
                (companion_hash, pubkey, name, updated_at)
            VALUES (?, ?, 'newer', ?)
            """,
            (_HASH, contact_key, time.time()),
        )
        conn.commit()

    upgraded = _handler(tmp_path)
    contacts = upgraded.companion_load_contacts_strict(_HASH)
    assert [(row["pubkey"], row["name"]) for row in contacts] == [(contact_key, "newer")]


def test_missing_legacy_marker_never_restores_global_outbound_dedup(tmp_path):
    handler = _handler(tmp_path)
    with handler._connect() as conn:
        conn.execute("DROP INDEX idx_companion_messages_dedup")
        conn.execute(
            """
            DELETE FROM migrations
            WHERE migration_name IN (
                'companion_messages_packet_hash_unique',
                'scope_companion_message_dedup_to_inbound'
            )
            """
        )
        conn.commit()

    for source in ("frame", "rest"):
        handler.companion_store_outbound_message(
            _HASH,
            {"packet_hash": "shared-hash", "text": source},
            source=source,
            state="transmitted",
        )

    upgraded = _handler(tmp_path)
    rows = upgraded.companion_get_messages(_HASH)
    assert [(row["source"], row["text"]) for row in rows] == [
        ("rest", "rest"),
        ("frame", "frame"),
    ]
    with upgraded._connect() as conn:
        index_sql = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_companion_messages_dedup'
            """
        ).fetchone()[0]
    assert "direction = 'in'" in index_sql


def test_namespace_binding_is_durable_idempotent_and_never_purges_history(tmp_path):
    handler = _handler(tmp_path)
    identity_a = "01" + ("11" * 31)
    identity_b = "01" + ("22" * 31)
    contact = {"pubkey": b"\x99" * 32, "name": "A's contact"}

    assert handler.companion_bind_namespace(_HASH, identity_a) == identity_a
    assert handler.companion_save_contacts(_HASH, [contact]) is True
    assert handler.companion_bind_namespace(_HASH, identity_a.upper()) == identity_a

    restarted = _handler(tmp_path)
    assert restarted.companion_namespace_binding(_HASH) == identity_a
    with pytest.raises(
        CompanionNamespaceCollisionError,
        match=r"already bound.*refusing activation",
    ):
        restarted.companion_bind_namespace(_HASH, identity_b)
    with pytest.raises(CompanionNamespaceCollisionError):
        restarted.companion_bind_namespace(
            _HASH,
            identity_b,
            adopt_legacy_namespace=True,
        )

    # Refusal is read-only: the original owner and its data remain intact.
    assert restarted.companion_namespace_binding(_HASH) == identity_a
    rows = restarted.companion_load_contacts_strict(_HASH)
    assert [(row["pubkey"], row["name"]) for row in rows] == [(contact["pubkey"], contact["name"])]


def test_namespace_migration_is_idempotent(tmp_path):
    _handler(tmp_path)
    _handler(tmp_path)

    conn = sqlite3.connect(tmp_path / "repeater.db")
    applied = conn.execute(
        """
        SELECT COUNT(*) FROM migrations
        WHERE migration_name =
            'bind_companion_namespaces_to_public_identities'
        """
    ).fetchone()[0]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(companion_namespace_bindings)")}
    conn.close()

    assert applied == 1
    assert columns == {"companion_hash", "companion_identity", "bound_at"}


def test_namespace_migration_never_guesses_from_a_paired_device(tmp_path):
    handler = _handler(tmp_path)
    identity_a = "01" + ("33" * 31)
    identity_b = "01" + ("44" * 31)
    token_id = handler.create_api_token(
        "upgrade-device",
        "upgrade-token",
        scope="companion:old",
    )
    handler.companion_device_create(
        _HASH,
        "upgrade-phone",
        "Phone",
        token_id,
        companion_identity=identity_a.upper(),
    )

    # Model an upgrade from a database that has paired-device identity
    # bindings but predates migration 20.
    with handler._connect() as conn:
        conn.execute(
            """
            DELETE FROM migrations
            WHERE migration_name =
                'bind_companion_namespaces_to_public_identities'
            """
        )
        conn.execute("DROP TABLE companion_namespace_bindings")
        conn.commit()

    upgraded = _handler(tmp_path)
    assert upgraded.companion_namespace_binding(_HASH) is None
    with pytest.raises(
        CompanionNamespaceCollisionError,
        match="legacy persisted state",
    ):
        upgraded.companion_bind_namespace(_HASH, identity_a)

    assert (
        upgraded.companion_bind_namespace(
            _HASH,
            identity_a,
            adopt_legacy_namespace=True,
        )
        == identity_a
    )
    with pytest.raises(CompanionNamespaceCollisionError):
        upgraded.companion_bind_namespace(
            _HASH,
            identity_b,
            adopt_legacy_namespace=True,
        )


@pytest.mark.parametrize(
    "table",
    [
        "companion_contacts",
        "companion_channels",
        "companion_messages",
        "companion_prefs",
        "companion_events",
        "companion_devices",
        "companion_journal_floors",
    ],
)
def test_every_legacy_namespace_table_requires_explicit_adoption(tmp_path, table):
    handler = _handler(tmp_path)
    identity = "01" + ("55" * 31)
    inserts = {
        "companion_contacts": (
            """
            INSERT INTO companion_contacts
                (companion_hash, pubkey, name, updated_at)
            VALUES (?, X'01', 'legacy', ?)
            """,
            (_HASH, time.time()),
        ),
        "companion_channels": (
            """
            INSERT INTO companion_channels
                (companion_hash, channel_idx, name, secret, updated_at)
            VALUES (?, 0, 'legacy', X'01', ?)
            """,
            (_HASH, time.time()),
        ),
        "companion_messages": (
            """
            INSERT INTO companion_messages
                (companion_hash, sender_key, text, created_at)
            VALUES (?, X'01', 'legacy', ?)
            """,
            (_HASH, time.time()),
        ),
        "companion_prefs": (
            """
            INSERT INTO companion_prefs (companion_hash, prefs_json)
            VALUES (?, '{}')
            """,
            (_HASH,),
        ),
        "companion_events": (
            """
            INSERT INTO companion_events
                (companion_hash, event_type, created_at, payload)
            VALUES (?, 'legacy', ?, '{}')
            """,
            (_HASH, time.time()),
        ),
        "companion_devices": (
            """
            INSERT INTO companion_devices
                (companion_hash, device_id, name, token_id, created_at)
            VALUES (?, 'legacy-device', 'Legacy', 1, ?)
            """,
            (_HASH, time.time()),
        ),
        "companion_journal_floors": (
            """
            INSERT INTO companion_journal_floors
                (companion_hash, prune_floor)
            VALUES (?, 0)
            """,
            (_HASH,),
        ),
    }
    sql, params = inserts[table]
    with handler._connect() as conn:
        conn.execute(sql, params)
        conn.commit()

    with pytest.raises(
        CompanionNamespaceCollisionError,
        match="adopt_legacy_namespace",
    ):
        handler.companion_bind_namespace(_HASH, identity)
    assert handler.companion_namespace_binding(_HASH) is None
    assert (
        handler.companion_bind_namespace(
            _HASH,
            identity,
            adopt_legacy_namespace=True,
        )
        == identity
    )


def test_migration_failures_surface_as_storage_errors():
    handler = object.__new__(SQLiteHandler)

    def unavailable():
        raise sqlite3.OperationalError("database unavailable")

    handler._connect = unavailable
    with pytest.raises(CompanionStorageError, match="migrations"):
        handler._run_migrations()


def test_journal_epoch_is_stable_on_restart_and_rotates_for_restored_db(tmp_path):
    first = _handler(tmp_path)
    stable_epoch = first.companion_journal_epoch()
    lineage_path = tmp_path / ".companion-journal-lineage"
    assert lineage_path.exists()
    assert lineage_path.stat().st_mode & 0o777 == 0o600

    restarted = _handler(tmp_path)
    assert restarted.companion_journal_epoch() == stable_epoch

    with restarted._connect() as conn:
        conn.execute(
            """
            UPDATE companion_journal_meta SET value = 'restored-lineage'
            WHERE key = 'database_lineage'
            """
        )
        conn.execute(
            """
            UPDATE companion_journal_meta SET value = 'restored-epoch'
            WHERE key = 'journal_epoch'
            """
        )
        conn.commit()

    restored = _handler(tmp_path)
    assert restored.companion_journal_epoch() not in {
        stable_epoch,
        "restored-epoch",
    }
    with restored._connect() as conn:
        database_lineage = conn.execute(
            """
            SELECT value FROM companion_journal_meta
            WHERE key = 'database_lineage'
            """
        ).fetchone()[0]
    assert lineage_path.read_text(encoding="ascii") == database_lineage


def test_startup_repairs_live_message_schema_before_send_recovery(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {"recipient_key": b"\x22" * 32, "text": "interrupted"},
        source="rest",
        state="pending",
    )

    # Simulate an older restored table paired with a newer migration ledger.
    # The correctness marker intentionally remains present.
    conn = sqlite3.connect(handler.sqlite_path)
    conn.execute("ALTER TABLE companion_messages DROP COLUMN expected_ack")
    conn.commit()
    conn.close()

    recovered = _handler(tmp_path)
    conn = sqlite3.connect(recovered.sqlite_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(companion_messages)")}
    applied = conn.execute(
        """
        SELECT COUNT(*) FROM migrations
        WHERE migration_name = 'companion_api_correctness_primitives'
        """
    ).fetchone()[0]
    conn.close()

    assert "expected_ack" in columns
    assert applied == 1
    message = recovered.companion_message_get_by_id(
        _HASH,
        stored["message_id"],
    )
    assert message["state"] == "indeterminate"


def test_strict_mobile_storage_distinguishes_failure_from_empty(tmp_path, monkeypatch):
    handler = _handler(tmp_path)

    def unavailable():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(handler, "_connect", unavailable)

    with pytest.raises(CompanionStorageError):
        handler.companion_get_messages_strict(_HASH)
    with pytest.raises(CompanionStorageError):
        handler.companion_device_get_by_token_strict(7)
    with pytest.raises(CompanionStorageError):
        handler.companion_device_get_strict("phone")
    with pytest.raises(CompanionStorageError):
        handler.companion_device_list_strict()
    with pytest.raises(CompanionStorageError):
        handler.companion_bind_namespace(_HASH, "01" + ("11" * 31))
    with pytest.raises(CompanionStorageError):
        handler.companion_namespace_binding(_HASH)
    with pytest.raises(CompanionStorageError):
        handler.companion_load_contacts_strict(_HASH)
    with pytest.raises(CompanionStorageError):
        handler.companion_message_get_by_id_strict(_HASH, 1)
    with pytest.raises(CompanionStorageError):
        handler.companion_messages_by_sender_strict(_HASH, b"\x11" * 32, 0.0, 1.0)
    with pytest.raises(CompanionStorageError):
        handler.packets_receptions_strict("0123456789ABCDEF", 0.0, 1.0)
    with pytest.raises(CompanionStorageError):
        handler.packets_transmissions_strict("0123456789ABCDEF", 0.0, 1.0)
    with pytest.raises(CompanionStorageError):
        handler.packets_heard_repeats_strict("0123456789ABCDEF", 0.0, 1.0)
    with pytest.raises(CompanionStorageError):
        handler.companion_device_set_push_strict("phone", "token")
    with pytest.raises(CompanionStorageError):
        handler.companion_device_clear_push_strict("phone")
    with pytest.raises(CompanionStorageError):
        handler.list_api_tokens_strict()

    # Existing non-v1 callers retain their historical cache-miss contracts.
    assert handler.companion_get_messages(_HASH) == []
    assert handler.companion_device_get_by_token(7) is None
    assert handler.companion_device_get("phone") is None
    assert handler.companion_device_list() == []
    assert handler.companion_load_contacts(_HASH) is None
    assert handler.companion_message_get_by_id(_HASH, 1) is None
    assert handler.companion_messages_by_sender(_HASH, b"\x11" * 32, 0.0, 1.0) == []
    assert handler.packets_receptions("0123456789ABCDEF", 0.0, 1.0) == []
    assert handler.packets_transmissions("0123456789ABCDEF", 0.0, 1.0) == []
    assert handler.packets_heard_repeats("0123456789ABCDEF", 0.0, 1.0) == []
    assert handler.companion_device_set_push("phone", "token") is False
    assert handler.companion_device_clear_push("phone") is False
    assert handler.list_api_tokens() == []


def test_contact_diff_and_events_rollback_as_one_transaction(tmp_path):
    handler = _handler(tmp_path)
    old_key = b"\x31" * 32
    new_key = b"\x32" * 32
    assert handler.companion_upsert_contact(
        _HASH,
        {"pubkey": old_key, "name": "old"},
    )

    with handler._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_contact_event
            BEFORE INSERT ON companion_events
            WHEN NEW.event_type = 'contact'
            BEGIN
                SELECT RAISE(ABORT, 'forced contact event failure');
            END
            """
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_apply_contact_changes(
            _HASH,
            [
                {
                    "change": "remove",
                    "contact": {"pubkey": old_key, "name": "old"},
                },
                {
                    "change": "new",
                    "contact": {"pubkey": new_key, "name": "new"},
                },
            ],
        )

    contacts = handler.companion_load_contacts_strict(_HASH)
    assert [contact["pubkey"] for contact in contacts] == [old_key]
    assert handler.companion_get_events(_HASH, 0) == []


def test_prune_floor_is_scoped_to_companion(tmp_path):
    handler = _handler(tmp_path)
    old = time.time() - 40 * 86400
    busy_old = handler.companion_append_event(_HASH, "message", {"old": True}, created_at=old)
    quiet_head = handler.companion_append_event(_OTHER_HASH, "message", {"fresh": True})

    assert handler.companion_prune_events(31) == 1
    assert handler.companion_journal_floor(_HASH) == busy_old
    assert handler.companion_journal_floor(_OTHER_HASH) == 0

    quiet_state = handler.companion_sync_state(_OTHER_HASH)
    status = handler.companion_cursor_status(_OTHER_HASH, quiet_state["epoch"], quiet_head)
    assert status["valid"] is True


def test_fully_pruned_quiet_companion_has_a_valid_empty_head_cursor(tmp_path):
    handler = _handler(tmp_path)
    old = time.time() - 40 * 86400
    deleted_seq = handler.companion_append_event(
        _HASH,
        "message",
        {"old": True},
        created_at=old,
    )

    assert handler.companion_prune_events(31) == 1
    state = handler.companion_sync_state(_HASH)
    page = handler.companion_sync_page(
        _HASH,
        state["epoch"],
        state["head"],
    )

    assert state["floor"] == deleted_seq
    assert state["head"] == deleted_seq
    assert page["valid"] is True
    assert page["events"] == []
    assert page["next_cursor"] == state["cursor"]


def test_companion_state_purge_and_epoch_rotation_are_atomic(tmp_path):
    handler = _handler(tmp_path)
    assert handler.companion_push_message(
        _HASH,
        _inbound("purge-me"),
    )
    old_epoch = handler.companion_journal_epoch()

    with handler._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_purge_epoch_rotation
            BEFORE UPDATE ON companion_journal_meta
            WHEN OLD.key = 'journal_epoch'
            BEGIN
                SELECT RAISE(ABORT, 'forced epoch rotation failure');
            END
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="epoch rotation failure"):
        handler.purge_table("companion_messages")

    assert len(handler.companion_get_messages(_HASH)) == 1
    assert handler.companion_journal_epoch() == old_epoch

    with handler._connect() as conn:
        conn.execute("DROP TRIGGER reject_purge_epoch_rotation")
        conn.commit()

    assert handler.purge_table("companion_messages") == 1
    assert handler.companion_get_messages(_HASH) == []
    assert handler.companion_journal_epoch() != old_epoch


def test_cursor_rejects_epoch_mismatch_and_future_sequence(tmp_path):
    handler = _handler(tmp_path)
    handler.companion_append_event(_HASH, "message", {})
    state = handler.companion_sync_state(_HASH)
    assert handler.companion_cursor_decode(state["cursor"]) == (
        state["epoch"],
        state["head"],
    )

    assert (
        handler.companion_cursor_status(_HASH, "wrong", state["head"])["reason"] == "epoch_mismatch"
    )
    assert (
        handler.companion_cursor_status(_HASH, state["epoch"], state["head"] + 1)["reason"]
        == "future_cursor"
    )


def test_sync_page_validates_and_pages_in_one_storage_snapshot(tmp_path):
    handler = _handler(tmp_path)
    for number in range(3):
        handler.companion_append_event(_HASH, "message", {"number": number})
    state = handler.companion_sync_state(_HASH)

    first = handler.companion_sync_page(_HASH, state["epoch"], 0, limit=2)
    assert first["valid"] is True
    assert first["has_more"] is True
    assert [event["payload"]["number"] for event in first["events"]] == [0, 1]
    assert handler.companion_cursor_decode(first["next_cursor"])[1] == first["next_seq"]

    second = handler.companion_sync_page(_HASH, state["epoch"], first["next_seq"], limit=2)
    assert second["valid"] is True
    assert second["has_more"] is False
    assert [event["payload"]["number"] for event in second["events"]] == [2]


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        "[]",
        '{"value":NaN}',
        '{"value":1e400}',
        '{"value":1,"value":2}',
    ],
)
def test_sync_page_fails_closed_on_corrupt_event_payload(tmp_path, payload):
    handler = _handler(tmp_path)
    with handler._connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO companion_events
                (companion_hash, event_type, created_at, payload)
            VALUES (?, 'message', ?, ?)
            """,
            (_HASH, time.time(), payload),
        )
        corrupt_seq = int(cursor.lastrowid)
        conn.commit()
    state = handler.companion_sync_state(_HASH)

    with pytest.raises(CompanionStorageError):
        handler.companion_sync_page(_HASH, state["epoch"], 0)

    # The row remains at the same sequence for repair/retry; no read path
    # silently substitutes an empty event and advances past it.
    assert state["head"] == corrupt_seq


def test_sync_page_fails_closed_on_non_finite_event_timestamp(tmp_path):
    handler = _handler(tmp_path)
    with handler._connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO companion_events
                (companion_hash, event_type, created_at, payload)
            VALUES (?, 'message', ?, '{}')
            """,
            (_HASH, float("inf")),
        )
        corrupt_seq = int(cursor.lastrowid)
        conn.commit()
    state = handler.companion_sync_state(_HASH)

    with pytest.raises(CompanionStorageError):
        handler.companion_sync_page(_HASH, state["epoch"], 0)

    assert state["head"] == corrupt_seq


def test_event_append_rejects_non_finite_timestamp_without_writing(tmp_path):
    handler = _handler(tmp_path)

    assert (
        handler.companion_append_event(
            _HASH,
            "message",
            {"text": "must not persist"},
            created_at=float("inf"),
        )
        is None
    )
    assert handler.companion_get_events(_HASH, 0) == []


def test_companion_json_writes_reject_non_finite_numbers_atomically(tmp_path):
    handler = _handler(tmp_path)

    assert handler.companion_save_prefs(_HASH, {"latitude": float("nan")}) is False
    assert handler.companion_load_prefs(_HASH) is None
    with pytest.raises(CompanionStorageError):
        handler.companion_save_prefs_with_event(
            _HASH,
            {"node_name": "safe"},
            {"latitude": float("inf")},
        )

    assert handler.companion_load_prefs(_HASH) is None
    assert handler.companion_get_events(_HASH, 0) == []


def test_idempotency_response_json_rejects_ambiguous_or_nonstandard_objects(
    tmp_path,
):
    handler = _handler(tmp_path)
    handler.companion_idempotency_reserve(
        "device",
        "phone",
        "key",
        "request",
    )

    for response_json in (
        "[]",
        '{"value":NaN}',
        '{"value":1e400}',
        '{"value":1,"value":2}',
        "{}",
        '{"success":true}',
        '{"success":true,"data":{}}',
        (
            '{"success":true,"data":'
            '{"message_id":9223372036854775808,'
            '"sent":true,"state":"transmitted"}}'
        ),
        ('{"success":true,"data":{"message_id":1,"sent":true,"state":"failed"}}'),
        ('{"success":true,"data":{"message_id":1,"sent":false,"state":"failed"}}'),
        ('{"success":true,"data":{"message_id":1,"sent":false,"state":"failed","reason":""}}'),
        (
            '{"success":true,"data":'
            '{"message_id":1,"sent":true,"state":"transmitted",'
            '"packet_hash":"aabbccddeeff0011"}}'
        ),
    ):
        with pytest.raises(ValueError):
            handler.companion_idempotency_complete(
                "device",
                "phone",
                "key",
                "request",
                response_json,
            )

    pending = handler.companion_idempotency_lookup(
        "device",
        "phone",
        "key",
        "request",
    )
    assert pending["result"] == "in_progress"


def test_non_finite_contact_state_is_rejected_on_write_and_load(tmp_path):
    handler = _handler(tmp_path)
    pubkey = b"\x41" * 32

    with pytest.raises(CompanionStorageError):
        handler.companion_upsert_contact_with_event(
            _HASH,
            {
                "pubkey": pubkey,
                "name": "invalid",
                "gps_lat": float("inf"),
                "gps_lon": 0.0,
            },
        )
    assert handler.companion_load_contacts_strict(_HASH) == []
    assert handler.companion_get_events(_HASH, 0) == []

    assert handler.companion_upsert_contact(
        _HASH,
        {
            "pubkey": pubkey,
            "name": "valid",
            "gps_lat": 0.0,
            "gps_lon": 0.0,
        },
    )
    with handler._connect() as conn:
        conn.execute(
            """
            UPDATE companion_contacts
            SET gps_lon = ?
            WHERE companion_hash = ? AND pubkey = ?
            """,
            (float("-inf"), _HASH, pubkey),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_load_contacts_strict(_HASH)


def test_non_finite_message_state_never_reaches_history_or_journal(tmp_path):
    handler = _handler(tmp_path)

    with pytest.raises(CompanionStorageError):
        handler.companion_store_inbound_message(
            _HASH,
            {
                "sender_key": b"\x42" * 32,
                "text": "invalid RF observation",
                "timestamp": 1000,
                "snr": float("inf"),
                "packet_hash": "invalid-snr",
            },
        )

    assert handler.companion_get_messages_strict(_HASH) == []
    assert handler.companion_get_events(_HASH, 0) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("id", 0, id="non-positive-id"),
        pytest.param("sender_key", sqlite3.Binary(b"\x01"), id="short-sender-key"),
        pytest.param(
            "recipient_key",
            sqlite3.Binary(b"\x01"),
            id="short-recipient-key",
        ),
        pytest.param("txt_type", "zero", id="text-txt-type"),
        pytest.param("txt_type", 64, id="out-of-range-txt-type"),
        pytest.param("timestamp", "now", id="text-timestamp"),
        pytest.param("timestamp", 1 << 32, id="out-of-range-timestamp"),
        pytest.param("text", sqlite3.Binary(b"text"), id="blob-text"),
        pytest.param("text", "x" * 161, id="oversized-text"),
        pytest.param("is_channel", 2, id="non-boolean-is-channel"),
        pytest.param("channel_idx", "zero", id="text-channel-index"),
        pytest.param("channel_idx", 256, id="out-of-range-channel-index"),
        pytest.param("path_len", 1.5, id="real-path-length"),
        pytest.param("path_len", -1, id="out-of-range-path-length"),
        pytest.param("sender_prefix", "not-hex", id="malformed-sender-prefix"),
        pytest.param("sender_prefix", "aa", id="short-sender-prefix"),
        pytest.param("snr", float("inf"), id="non-finite-snr"),
        pytest.param("rssi", "strong", id="text-rssi"),
        pytest.param(
            "channel_data_type",
            "binary",
            id="text-channel-data-type",
        ),
        pytest.param(
            "channel_data_type",
            1 << 16,
            id="out-of-range-channel-data-type",
        ),
        pytest.param(
            "channel_data_payload",
            "not-a-blob",
            id="text-channel-data-payload",
        ),
        pytest.param(
            "channel_data_payload",
            sqlite3.Binary(b"x" * 166),
            id="oversized-channel-data-payload",
        ),
        pytest.param("packet_hash", "not-a-hash", id="malformed-packet-hash"),
        pytest.param("packet_hash", "A" * 17, id="odd-length-packet-hash"),
        pytest.param("packet_hash", "A" * 66, id="oversized-packet-hash"),
        pytest.param("created_at", float("inf"), id="non-finite-created-at"),
        pytest.param("consumed_at", float("inf"), id="non-finite-consumed-at"),
        pytest.param("observation_count", -1, id="negative-observation-count"),
        pytest.param("unique_path_count", 2, id="too-many-unique-paths"),
        pytest.param("direction", "sideways", id="unknown-direction"),
        pytest.param("state", "unknown", id="unknown-state"),
        pytest.param("expected_ack", "ack", id="text-expected-ack"),
        pytest.param("source", "unknown", id="unknown-source"),
        pytest.param("pending_for_frame", 2, id="non-boolean-frame-state"),
    ],
)
def test_strict_message_history_fails_closed_on_corrupt_row(tmp_path, column, value):
    handler = _handler(tmp_path)
    stored = handler.companion_store_inbound_message(
        _HASH,
        _inbound("ABCDEF0123456789"),
    )
    with handler._connect() as conn:
        conn.execute(
            f"UPDATE companion_messages SET {column} = ? WHERE id = ?",
            (value, stored["message_id"]),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_get_messages_strict(_HASH)


def test_legacy_message_history_keeps_permissive_frame_contract(tmp_path):
    handler = _handler(tmp_path)
    assert handler.companion_push_message(
        _HASH,
        {
            "text": "legacy Frame row",
            "timestamp": 1,
            "packet_hash": "legacy-placeholder",
        },
    )

    assert handler.companion_get_messages(_HASH)[0]["packet_hash"] == ("legacy-placeholder")
    with pytest.raises(CompanionStorageError):
        handler.companion_get_messages_strict(_HASH)


def test_strict_message_ownership_reads_validate_complete_rows(tmp_path):
    handler = _handler(tmp_path)
    inbound = handler.companion_store_inbound_message(
        _HASH,
        _inbound("ABCDEF0123456789"),
    )
    outbound = handler.companion_store_outbound_message(
        _HASH,
        {
            "sender_key": b"\x33" * 32,
            "recipient_key": b"\x44" * 32,
            "text": "outbound",
            "timestamp": 1001,
            "packet_hash": "1122334455667788" + ("00" * 24),
        },
        source="rest",
        state="transmitted",
    )
    with handler._connect() as conn:
        conn.execute(
            "UPDATE companion_messages SET is_channel = 2 WHERE id = ?",
            (inbound["message_id"],),
        )
        conn.execute(
            "UPDATE companion_messages SET state = 'unknown' WHERE id = ?",
            (outbound["message_id"],),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_message_get_by_id_strict(
            _HASH,
            inbound["message_id"],
        )
    with pytest.raises(CompanionStorageError):
        handler.companion_outbound_message_get_by_hash(
            _HASH,
            "1122334455667788",
        )

    # The old wrappers keep their non-raising compatibility contract.
    assert (
        handler.companion_message_get_by_id(
            _HASH,
            inbound["message_id"],
        )["is_channel"]
        is True
    )


def test_non_finite_auth_timestamps_fail_strict_device_reads(tmp_path):
    handler = _handler(tmp_path)
    token_id = handler.create_api_token(
        "phone",
        "token-hash",
        scope="companion:test",
    )
    assert handler.companion_device_create(
        _HASH,
        "phone",
        "Phone",
        token_id,
    )
    with handler._connect() as conn:
        conn.execute(
            "UPDATE companion_devices SET created_at = ? WHERE device_id = 'phone'",
            (float("inf"),),
        )
        conn.execute(
            "UPDATE api_tokens SET last_used = ? WHERE id = ?",
            (float("-inf"), token_id),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_device_get_strict("phone")
    with pytest.raises(CompanionStorageError):
        handler.list_api_tokens_strict()


def test_concurrent_idempotency_reservation_has_one_winner(tmp_path):
    handler = _handler(tmp_path)

    def reserve():
        return handler.companion_idempotency_reserve("device", "phone", "same-key", "same-request")[
            "result"
        ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(), range(16)))

    assert results.count("reserved") == 1
    assert set(results) == {"reserved", "in_progress"}


def test_idempotency_replays_metadata_and_detects_conflict(tmp_path):
    handler = _handler(tmp_path)
    response_json = '{"success":true,"data":{"message_id":7,"sent":true,"state":"transmitted"}}'
    assert (
        handler.companion_idempotency_reserve("device", "phone", "key", "request-a")["result"]
        == "reserved"
    )
    handler.companion_idempotency_complete(
        "device",
        "phone",
        "key",
        "request-a",
        response_json,
        message_id=7,
        packet_hash="aabb",
        expected_ack=123,
    )

    replay = handler.companion_idempotency_reserve("device", "phone", "key", "request-a")
    assert replay["result"] == "replay"
    assert replay["response_json"] == response_json
    assert replay["message_id"] == 7
    assert replay["packet_hash"] == "aabb"
    assert replay["expected_ack"] == 123

    conflict = handler.companion_idempotency_reserve("device", "phone", "key", "request-b")
    assert conflict["result"] == "conflict"


def test_indeterminate_idempotency_state_cannot_regress(tmp_path):
    handler = _handler(tmp_path)
    handler.companion_idempotency_reserve(
        "device",
        "phone",
        "key",
        "request",
    )
    marked = handler.companion_idempotency_mark_indeterminate(
        "device",
        "phone",
        "key",
        "request",
        message_id=7,
        packet_hash="aabb",
        expected_ack=123,
    )
    assert marked["state"] == "indeterminate"

    late_transmit = handler.companion_idempotency_mark_transmitted(
        "device",
        "phone",
        "key",
        "request",
        message_id=7,
        packet_hash="ccdd",
        expected_ack=456,
    )
    late_complete = handler.companion_idempotency_complete(
        "device",
        "phone",
        "key",
        "request",
        ('{"success":true,"data":{"message_id":7,"sent":true,"state":"transmitted"}}'),
        message_id=7,
        packet_hash="ccdd",
        expected_ack=456,
    )

    assert late_transmit["result"] == "indeterminate"
    assert late_complete["result"] == "indeterminate"
    stored = handler.companion_idempotency_lookup(
        "device",
        "phone",
        "key",
        "request",
    )
    assert stored["state"] == "indeterminate"
    assert stored["response_json"] == ""
    assert stored["packet_hash"] == "aabb"
    assert stored["expected_ack"] == 123


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("state", "unknown"),
        ("created_at", float("inf")),
        ("updated_at", float("-inf")),
    ],
)
def test_idempotency_lookup_fails_closed_on_corrupt_row(tmp_path, column, value):
    handler = _handler(tmp_path)
    handler.companion_idempotency_reserve(
        "device",
        "phone",
        "key",
        "request",
    )
    with handler._connect() as conn:
        conn.execute(
            f"UPDATE companion_idempotency SET {column} = ? "
            "WHERE device_id = 'device:phone' AND idempotency_key = 'key'",
            (value,),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_idempotency_lookup(
            "device",
            "phone",
            "key",
            "request",
        )


def test_stale_pending_idempotency_becomes_indeterminate_not_deleted(tmp_path):
    handler = _handler(tmp_path)
    handler.companion_idempotency_reserve("device", "phone", "key", "request")
    conn = sqlite3.connect(handler.sqlite_path)
    conn.execute(
        "UPDATE companion_idempotency SET created_at = ?",
        (time.time() - 49 * 3600,),
    )
    conn.commit()
    conn.close()

    assert handler.companion_idempotency_prune() == 0
    retry = handler.companion_idempotency_reserve("device", "phone", "key", "request")
    assert retry["result"] == "indeterminate"


@pytest.mark.parametrize("initial_state", ["pending", "transmitted"])
def test_stale_send_prune_aligns_key_message_and_journal(tmp_path, initial_state):
    handler = _handler(tmp_path)
    outbound = {
        "sender_key": b"\x01" * 32,
        "recipient_key": b"\x02" * 32,
        "text": "stale send",
        "timestamp": 1,
        "is_channel": False,
        "channel_idx": 0,
        "txt_type": 0,
    }
    reserved = handler.companion_reserve_outbound_send(
        _HASH,
        "device",
        "phone",
        "key",
        "request",
        outbound,
    )
    message_id = reserved["message_id"]
    packet_hash = None
    expected_ack = None
    if initial_state == "transmitted":
        packet_hash = "AB" * 8
        expected_ack = 123
        handler.companion_update_outbound_state(
            _HASH,
            message_id,
            "transmitted",
            packet_hash,
            expected_ack,
        )
        handler.companion_idempotency_mark_transmitted(
            "device",
            "phone",
            "key",
            "request",
            message_id,
            packet_hash,
            expected_ack,
        )
    with handler._connect() as conn:
        conn.execute(
            """
            UPDATE companion_idempotency
            SET created_at = ?
            WHERE device_id = 'device:phone' AND idempotency_key = 'key'
            """,
            (time.time() - 49 * 3600,),
        )
        conn.commit()

    event_count = len(handler.companion_get_events(_HASH, 0))
    assert handler.companion_idempotency_prune() == 0

    key = handler.companion_idempotency_get("device:phone", "key")
    message = handler.companion_message_get_by_id(_HASH, message_id)
    events = handler.companion_get_events(_HASH, 0)
    assert key["state"] == "indeterminate"
    assert message["state"] == "indeterminate"
    assert message["packet_hash"] == packet_hash
    assert message["expected_ack"] == expected_ack
    assert len(events) == event_count + 1
    assert events[-1]["event_type"] == "message_send_state"
    assert events[-1]["payload"] == {
        "message_id": message_id,
        "state": "indeterminate",
        "packet_hash": packet_hash,
        "expected_ack": expected_ack,
    }


def test_stale_send_prune_rolls_back_key_and_message_if_journal_fails(tmp_path):
    handler = _handler(tmp_path)
    reserved = handler.companion_reserve_outbound_send(
        _HASH,
        "device",
        "phone",
        "key",
        "request",
        {
            "sender_key": b"\x01" * 32,
            "recipient_key": b"\x02" * 32,
            "text": "stale send",
            "timestamp": 1,
            "is_channel": False,
            "channel_idx": 0,
            "txt_type": 0,
        },
    )
    with handler._connect() as conn:
        conn.execute(
            """
            UPDATE companion_idempotency
            SET created_at = ?
            WHERE device_id = 'device:phone' AND idempotency_key = 'key'
            """,
            (time.time() - 49 * 3600,),
        )
        conn.execute(
            """
            CREATE TRIGGER reject_pruned_send_state_event
            BEFORE INSERT ON companion_events
            WHEN NEW.event_type = 'message_send_state'
            BEGIN
                SELECT RAISE(ABORT, 'test rejection');
            END
            """
        )
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        handler.companion_idempotency_prune()

    key = handler.companion_idempotency_get("device:phone", "key")
    message = handler.companion_message_get_by_id(_HASH, reserved["message_id"])
    assert key["state"] == "pending"
    assert message["state"] == "pending"
    assert [event["event_type"] for event in handler.companion_get_events(_HASH, 0)] == ["message"]


def test_inbound_history_survives_disabled_frame_queue(tmp_path):
    handler = _handler(tmp_path)
    result = handler.companion_store_inbound_message(_HASH, _inbound("packet-1"), max_pending=0)

    assert result["inserted"] is True
    assert result["queued"] is False
    assert result["message"]["id"] == result["message_id"]
    assert result["event"]["payload"]["id"] == result["message_id"]
    assert handler.companion_load_messages(_HASH) == []
    assert [row["text"] for row in handler.companion_get_messages(_HASH)] == ["packet-1"]


def test_direct_pending_message_is_not_displaced_but_new_history_is_kept(tmp_path):
    handler = _handler(tmp_path)
    first = handler.companion_store_inbound_message(
        _HASH, _inbound("direct", direct=True), max_pending=1
    )
    second = handler.companion_store_inbound_message(_HASH, _inbound("channel"), max_pending=1)

    assert first["queued"] is True
    assert second["queued"] is False
    assert {row["text"] for row in handler.companion_get_messages(_HASH)} == {
        "direct",
        "channel",
    }
    assert handler.companion_pop_message(_HASH)["text"] == "direct"


def test_message_and_event_rollback_together(tmp_path):
    handler = _handler(tmp_path)
    conn = sqlite3.connect(handler.sqlite_path)
    conn.execute(
        """
        CREATE TRIGGER reject_companion_event
        BEFORE INSERT ON companion_events
        BEGIN
            SELECT RAISE(ABORT, 'test rejection');
        END
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(CompanionStorageError):
        handler.companion_store_inbound_message(_HASH, _inbound("rolled-back"), max_pending=1)
    assert handler.companion_get_messages(_HASH) == []


def test_outbound_lifecycle_is_durable_and_journaled(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {
            "recipient_key": b"\x22" * 32,
            "text": "hello",
            "expected_ack": 42,
        },
        source="rest",
    )
    updated = handler.companion_update_outbound_state(
        _HASH,
        stored["message_id"],
        "transmitted",
        packet_hash="beef",
        expected_ack=42,
    )

    assert stored["message"]["direction"] == "out"
    assert stored["message"]["source"] == "rest"
    assert updated["message"]["state"] == "transmitted"
    assert updated["message"]["packet_hash"] == "beef"
    assert [event["event_type"] for event in handler.companion_get_events(_HASH, 0)] == [
        "message",
        "message_send_state",
    ]


def test_packet_hash_dedup_is_inbound_only(tmp_path):
    handler = _handler(tmp_path)
    packet_hash = "ABCDEF0123456789"

    inbound = handler.companion_store_inbound_message(
        _HASH,
        _inbound(packet_hash),
    )
    outbound_a = handler.companion_store_outbound_message(
        _HASH,
        {"packet_hash": packet_hash, "text": "frame"},
        source="frame",
        state="transmitted",
    )
    outbound_b = handler.companion_store_outbound_message(
        _HASH,
        {"packet_hash": packet_hash, "text": "operator"},
        source="operator",
        state="transmitted",
    )
    replay = handler.companion_store_inbound_message(
        _HASH,
        _inbound(packet_hash),
    )

    assert replay["inserted"] is False
    assert replay["message_id"] == inbound["message_id"]
    assert handler.companion_get_message_id(_HASH, packet_hash) == inbound["message_id"]
    assert {
        outbound_a["message_id"],
        outbound_b["message_id"],
    }.isdisjoint({inbound["message_id"]})
    assert [
        (row["direction"], row["source"], row["text"])
        for row in handler.companion_get_messages(_HASH)
    ] == [
        ("out", "operator", "operator"),
        ("out", "frame", "frame"),
        ("in", "radio", packet_hash),
    ]


def test_heard_repeat_event_failure_rolls_back_message_state(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {
            "packet_hash": "1234567890ABCDEF",
            "recipient_key": b"\x22" * 32,
            "text": "hello",
        },
        source="frame",
        state="transmitted",
    )
    with handler._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_heard_repeat_event
            BEFORE INSERT ON companion_events
            WHEN NEW.event_type = 'message_send_state'
            BEGIN
                SELECT RAISE(ABORT, 'test rejection');
            END
            """
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_record_outbound_heard_repeat(
            _HASH,
            stored["message_id"],
            {
                "packet_hash": "1234567890ABCDEF",
                "path": ["11"],
                "terminal_hash": "11",
                "heard_repeat_count": 1,
                "unique_repeater_count": 1,
            },
        )

    message = handler.companion_message_get_by_id(
        _HASH,
        stored["message_id"],
    )
    assert message["state"] == "transmitted"
    assert [event["event_type"] for event in handler.companion_get_events(_HASH, 0)] == ["message"]


def test_reception_event_failure_rolls_back_inbound_counters(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_inbound_message(
        _HASH,
        _inbound("1234567890ABCDEF"),
    )
    with handler._connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_reception_event
            BEFORE INSERT ON companion_events
            WHEN NEW.event_type = 'message_reception'
            BEGIN
                SELECT RAISE(ABORT, 'test rejection');
            END
            """
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_record_inbound_reception(
            _HASH,
            stored["message_id"],
            {
                "packet_hash": "1234567890ABCDEF",
                "path": ["11"],
                "observation_count": 2,
                "unique_path_count": 2,
            },
        )

    message = handler.companion_message_get_by_id(
        _HASH,
        stored["message_id"],
    )
    assert message["observation_count"] == 1
    assert message["unique_path_count"] == 1
    assert [event["event_type"] for event in handler.companion_get_events(_HASH, 0)] == ["message"]


def test_operator_outbound_source_is_preserved(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {
            "recipient_key": b"\x23" * 32,
            "text": "sent by operator API",
        },
        source="operator",
        state="transmitted",
    )

    assert stored["message"]["source"] == "operator"


def test_explicit_direct_outbound_is_not_misclassified_as_channel(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {
            "recipient_key": b"\x22" * 32,
            "channel_idx": 0,
            "is_channel": False,
            "text": "direct",
        },
        source="rest",
    )

    assert stored["message"]["is_channel"] is False


def test_outbound_reservation_rolls_back_key_message_and_event_together(tmp_path):
    handler = _handler(tmp_path)
    conn = sqlite3.connect(handler.sqlite_path)
    conn.execute(
        """
        CREATE TRIGGER reject_outbound_reservation_event
        BEFORE INSERT ON companion_events
        BEGIN
            SELECT RAISE(ABORT, 'test rejection');
        END
        """
    )
    conn.commit()
    conn.close()

    trace = []
    cached_conn = handler._connect()
    cached_conn.set_trace_callback(trace.append)
    try:
        with pytest.raises(CompanionStorageError):
            handler.companion_reserve_outbound_send(
                _HASH,
                "device",
                "phone",
                "key",
                "request",
                {"recipient_key": b"\x22" * 32, "text": "hello"},
            )
    finally:
        cached_conn.set_trace_callback(None)

    assert handler.companion_idempotency_lookup("device", "phone", "key", "request") is None
    assert handler.companion_get_messages(_HASH) == []
    statements = [statement.strip().upper() for statement in trace]
    full = statements.index("PRAGMA SYNCHRONOUS=FULL")
    begin = statements.index("BEGIN IMMEDIATE")
    rollback = statements.index("ROLLBACK")
    normal = statements.index("PRAGMA SYNCHRONOUS=NORMAL")
    assert full < begin < rollback < normal
    assert cached_conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_outbound_reservation_is_durable_before_return_and_restores_normal(tmp_path):
    handler = _handler(tmp_path)
    trace = []
    conn = handler._connect()
    conn.set_trace_callback(trace.append)
    try:
        result = handler.companion_reserve_outbound_send(
            _HASH,
            "device",
            "phone",
            "key",
            "request",
            {"recipient_key": b"\x22" * 32, "text": "hello"},
        )
    finally:
        conn.set_trace_callback(None)

    assert result["result"] == "reserved"
    statements = [statement.strip().upper() for statement in trace]
    full = statements.index("PRAGMA SYNCHRONOUS=FULL")
    begin = statements.index("BEGIN IMMEDIATE")
    commit = statements.index("COMMIT")
    normal = statements.index("PRAGMA SYNCHRONOUS=NORMAL")
    assert full < begin < commit < normal
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_concurrent_outbound_reservation_creates_exactly_one_message(tmp_path):
    handler = _handler(tmp_path)

    def reserve():
        return handler.companion_reserve_outbound_send(
            _HASH,
            "device",
            "phone",
            "same-key",
            "same-request",
            {"recipient_key": b"\x22" * 32, "text": "hello"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _unused: reserve(), range(2)))

    assert sorted(result["result"] for result in results) == [
        "in_progress",
        "reserved",
    ]
    assert len(handler.companion_get_messages(_HASH)) == 1


def test_outbound_completion_rolls_back_message_and_replay_state_together(tmp_path):
    handler = _handler(tmp_path)
    reserved = handler.companion_reserve_outbound_send(
        _HASH,
        "device",
        "phone",
        "key",
        "request",
        {"recipient_key": b"\x22" * 32, "text": "hello"},
    )
    conn = sqlite3.connect(handler.sqlite_path)
    conn.execute(
        """
        CREATE TRIGGER reject_outbound_state_event
        BEFORE INSERT ON companion_events
        WHEN NEW.event_type = 'message_send_state'
        BEGIN
            SELECT RAISE(ABORT, 'test rejection');
        END
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(CompanionStorageError):
        response_json = (
            '{"success":true,"data":'
            f'{{"message_id":{reserved["message_id"]},'
            '"sent":true,"state":"transmitted"}}'
        )
        handler.companion_complete_outbound_send(
            _HASH,
            "device",
            "phone",
            "key",
            "request",
            reserved["message_id"],
            "transmitted",
            response_json,
            packet_hash="beef",
            expected_ack=42,
        )

    key = handler.companion_idempotency_lookup("device", "phone", "key", "request")
    message = handler.companion_message_get_by_id(_HASH, reserved["message_id"])
    assert key["state"] == "pending"
    assert message["state"] == "pending"
    assert message["packet_hash"] is None


def test_fast_ack_cannot_be_regressed_by_late_transmitted_write(tmp_path):
    handler = _handler(tmp_path)
    stored = handler.companion_store_outbound_message(
        _HASH,
        {"recipient_key": b"\x22" * 32, "text": "hello"},
        source="rest",
    )
    message_id = stored["message_id"]
    confirmed = handler.companion_update_outbound_state(
        _HASH,
        message_id,
        "confirmed",
        packet_hash="beef",
        expected_ack=42,
    )
    late = handler.companion_update_outbound_state(
        _HASH,
        message_id,
        "transmitted",
        packet_hash="beef",
        expected_ack=42,
    )

    assert confirmed["message"]["state"] == "confirmed"
    assert late["message"]["state"] == "confirmed"
    assert late["transition_applied"] is False
    assert late["event"] is None
    assert [
        event["payload"]["state"]
        for event in handler.companion_get_events(_HASH, 0)
        if event["event_type"] == "message_send_state"
    ] == ["confirmed"]


def test_concurrent_ack_and_transmit_completion_never_regress(tmp_path):
    handler = _handler(tmp_path)

    for number in range(20):
        stored = handler.companion_store_outbound_message(
            _HASH,
            {"recipient_key": b"\x22" * 32, "text": str(number)},
            source="rest",
        )
        message_id = stored["message_id"]
        start = threading.Barrier(3)

        def advance(state):
            start.wait()
            return handler.companion_update_outbound_state(
                _HASH,
                message_id,
                state,
                packet_hash=f"packet-{number}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmed = pool.submit(advance, "confirmed")
            transmitted = pool.submit(advance, "transmitted")
            start.wait()
            confirmed.result()
            transmitted.result()

        message = handler.companion_message_get_by_id(_HASH, message_id)
        assert message["state"] == "confirmed"


def test_journal_listener_notifications_are_monotonic_under_concurrency(tmp_path):
    journal = CompanionEventJournal(_handler(tmp_path), _HASH)
    received = []
    journal.register_listener(received.append)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda number: journal.record_prefs({"number": number}), range(40)))

    sequences = [event["seq"] for event in received]
    assert sequences == sorted(sequences)
    assert len(sequences) == 40


def test_pair_and_revoke_are_transactional_and_identity_bound(tmp_path):
    handler = _handler(tmp_path)
    paired = handler.companion_pair_device(
        _HASH,
        companion_identity="11" * 32,
        device_id="phone",
        name="Phone",
        token_name="Phone",
        token_hash="token-hash",
        scope="companion-id:" + "11" * 32,
    )
    assert paired["device"]["companion_identity"] == "11" * 32

    revoked = handler.companion_revoke_device(device_id="phone")
    assert revoked == {"devices_deleted": 1, "tokens_deleted": 1}
    assert handler.companion_device_get("phone") is None
    assert handler.list_api_tokens() == []


def test_restart_migrates_numeric_device_principal_to_stable_identity(tmp_path):
    handler = _handler(tmp_path)
    identity = "12" * 32
    paired = handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="stable-phone",
        name="Phone",
        token_name="Phone",
        token_hash="stable-token",
        scope="companion:test",
    )
    legacy_id = str(paired["device"]["id"])
    handler.companion_idempotency_reserve(
        "device",
        legacy_id,
        "retry-key",
        "request-hash",
    )
    conn = handler._connect()
    conn.execute(
        """
        UPDATE companion_idempotency
        SET state = 'complete',
            response_json = '{"success":true,"data":{"message_id":1,"sent":true,"state":"transmitted"}}'
        WHERE principal_type = 'device' AND principal_id = ?
        """,
        (legacy_id,),
    )
    conn.commit()

    # The migration marker is intentionally still present. Reconciliation
    # must run on every startup so restored/downgraded writers cannot add a
    # fresh numeric principal behind an old marker.
    reopened = _handler(tmp_path)
    stable_id = f"{identity}:stable-phone"
    replay = reopened.companion_idempotency_lookup(
        "device",
        stable_id,
        "retry-key",
        "request-hash",
    )

    assert replay["result"] == "replay"
    assert replay["response_json"] == (
        '{"success":true,"data":{"message_id":1,"sent":true,"state":"transmitted"}}'
    )
    assert (
        reopened.companion_idempotency_lookup(
            "device",
            legacy_id,
            "retry-key",
            "request-hash",
        )
        is None
    )


def test_device_principal_migration_retains_incompatible_collision_fail_closed(
    tmp_path,
):
    handler = _handler(tmp_path)
    identity = "13" * 32
    paired = handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="collision-phone",
        name="Phone",
        token_name="Phone",
        token_hash="collision-token",
        scope="companion:test",
    )
    legacy_id = str(paired["device"]["id"])
    stable_id = f"{identity}:collision-phone"
    handler.companion_idempotency_reserve(
        "device",
        legacy_id,
        "same-key",
        "legacy-request",
    )
    handler.companion_idempotency_reserve(
        "device",
        stable_id,
        "same-key",
        "new-request",
    )
    conn = handler._connect()
    conn.execute(
        """
        DELETE FROM migrations
        WHERE migration_name = 'stabilize_companion_device_principals'
        """
    )
    conn.commit()

    reopened = _handler(tmp_path)
    stable = reopened.companion_idempotency_lookup(
        "device",
        stable_id,
        "same-key",
        "new-request",
    )
    legacy = reopened.companion_idempotency_lookup(
        "device",
        legacy_id,
        "same-key",
        "legacy-request",
    )
    conn = reopened._connect()
    retained_rows = conn.execute(
        """
        SELECT COUNT(*) FROM companion_idempotency
        WHERE idempotency_key = 'same-key'
        """
    ).fetchone()[0]

    assert stable["result"] == "indeterminate"
    assert legacy["result"] == "indeterminate"
    assert retained_rows == 2


def test_revoke_rekeys_indeterminate_legacy_principal_before_mapping_is_deleted(
    tmp_path,
):
    handler = _handler(tmp_path)
    identity = "14" * 32
    paired = handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="repaired-phone",
        name="Phone",
        token_name="Phone",
        token_hash="first-token",
        scope="companion:test",
    )
    legacy_id = str(paired["device"]["id"])
    handler.companion_idempotency_reserve(
        "device",
        legacy_id,
        "lost-key",
        "lost-request",
    )
    conn = handler._connect()
    conn.execute(
        """
        UPDATE companion_idempotency
        SET state = 'indeterminate'
        WHERE principal_type = 'device' AND principal_id = ?
        """,
        (legacy_id,),
    )
    conn.commit()

    assert handler.companion_revoke_device(device_id="repaired-phone") == {
        "devices_deleted": 1,
        "tokens_deleted": 1,
    }
    handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="repaired-phone",
        name="Phone",
        token_name="Phone",
        token_hash="second-token",
        scope="companion:test",
    )
    stable = handler.companion_idempotency_lookup(
        "device",
        f"{identity}:repaired-phone",
        "lost-key",
        "lost-request",
    )

    assert stable["result"] == "indeterminate"


def test_generic_token_revoke_rekeys_legacy_device_principal_before_delete(
    tmp_path,
):
    handler = _handler(tmp_path)
    identity = "15" * 32
    paired = handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="token-revoked-phone",
        name="Phone",
        token_name="Phone",
        token_hash="first-generic-token",
        scope="companion:test",
    )
    token_id = paired["token_id"]
    legacy_id = str(paired["device"]["id"])
    handler.companion_idempotency_reserve(
        "device",
        legacy_id,
        "lost-generic-key",
        "lost-generic-request",
    )
    conn = handler._connect()
    conn.execute(
        """
        UPDATE companion_idempotency
        SET state = 'indeterminate'
        WHERE principal_type = 'device' AND principal_id = ?
        """,
        (legacy_id,),
    )
    conn.commit()

    assert handler.revoke_api_token_strict(token_id) is True
    handler.companion_pair_device(
        _HASH,
        companion_identity=identity,
        device_id="token-revoked-phone",
        name="Phone",
        token_name="Phone",
        token_hash="second-generic-token",
        scope="companion:test",
    )
    stable = handler.companion_idempotency_lookup(
        "device",
        f"{identity}:token-revoked-phone",
        "lost-generic-key",
        "lost-generic-request",
    )

    assert stable["result"] == "indeterminate"
