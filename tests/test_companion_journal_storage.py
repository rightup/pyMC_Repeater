"""Storage-layer tests for the Mobile Companion API event journal (phase 1).

Covers the ``companion_events`` / ``companion_journal_meta`` migration, the
journal read/write/prune methods, and the soft-consume change to the
frame-protocol message queue (``companion_push_message`` /
``companion_pop_message``): popping now marks a row consumed instead of
deleting it, so ``companion_messages`` becomes durable history while still
behaving like a queue for the frame protocol. See
docs/architecture/mobile-companion-api.md §5 (journal) and §13 (performance).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from repeater.data_acquisition.sqlite_handler import (
    CompanionStorageError,
    SQLiteHandler,
)

_HASH = "0x01"
_HASH2 = "0x02"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


# --- Migration ---------------------------------------------------------


def test_migration_applies_once_and_is_idempotent(tmp_path):
    h1 = _handler(tmp_path)

    conn = sqlite3.connect(str(h1.sqlite_path))
    applied = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE migration_name = 'add_companion_event_journal'"
    ).fetchone()[0]
    assert applied == 1

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('companion_events', 'companion_journal_meta')"
        )
    }
    assert tables == {"companion_events", "companion_journal_meta"}

    columns = [row[1] for row in conn.execute("PRAGMA table_info(companion_messages)")]
    assert "consumed_at" in columns

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name = 'idx_companion_events_sync'"
        )
    }
    assert indexes == {"idx_companion_events_sync"}
    conn.close()

    # Re-opening the same DB file re-runs migrations; must not error or
    # duplicate the migration row / re-create tables.
    h2 = _handler(tmp_path)
    conn = sqlite3.connect(str(h2.sqlite_path))
    applied = conn.execute(
        "SELECT COUNT(*) FROM migrations WHERE migration_name = 'add_companion_event_journal'"
    ).fetchone()[0]
    assert applied == 1
    conn.close()

    # Functional smoke check post-reopen.
    assert h2.companion_append_event(_HASH, "message", {"id": 1}) is not None


def test_companion_events_seq_is_autoincrement(tmp_path):
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_events'"
    ).fetchone()[0]
    conn.close()
    assert "AUTOINCREMENT" in sql.upper()


# --- Append / get: ordering, filtering, limit clamp, payload round-trip ---


def test_append_and_get_events_ordering_and_after_seq_filter(tmp_path):
    h = _handler(tmp_path)

    seqs = []
    for i in range(5):
        seq = h.companion_append_event(_HASH, "message", {"n": i})
        assert seq is not None
        seqs.append(seq)

    # Strictly increasing.
    assert seqs == sorted(seqs)

    all_events = h.companion_get_events(_HASH, after_seq=0)
    assert [e["seq"] for e in all_events] == seqs
    assert [e["payload"]["n"] for e in all_events] == [0, 1, 2, 3, 4]

    tail = h.companion_get_events(_HASH, after_seq=seqs[1])
    assert [e["seq"] for e in tail] == seqs[2:]

    none_after_head = h.companion_get_events(_HASH, after_seq=seqs[-1])
    assert none_after_head == []


def test_get_events_scoped_to_companion_hash(tmp_path):
    h = _handler(tmp_path)
    h.companion_append_event(_HASH, "message", {"who": "a"})
    h.companion_append_event(_HASH2, "message", {"who": "b"})

    events = h.companion_get_events(_HASH, after_seq=0)
    assert len(events) == 1
    assert events[0]["payload"]["who"] == "a"


def test_get_events_limit_clamp(tmp_path):
    h = _handler(tmp_path)
    for i in range(10):
        h.companion_append_event(_HASH, "message", {"n": i})

    # Below-minimum clamps up to 1.
    assert len(h.companion_get_events(_HASH, after_seq=0, limit=0)) == 1
    assert len(h.companion_get_events(_HASH, after_seq=0, limit=-5)) == 1

    # Above-maximum clamps down to 500 (only 10 rows exist, so this just
    # verifies no error / truncation below what exists).
    assert len(h.companion_get_events(_HASH, after_seq=0, limit=10000)) == 10

    # A normal in-range limit is respected exactly.
    assert len(h.companion_get_events(_HASH, after_seq=0, limit=3)) == 3


def test_event_payload_round_trip(tmp_path):
    h = _handler(tmp_path)
    payload = {"message_id": 42, "text": "hi", "nested": {"a": [1, 2, 3]}}
    seq = h.companion_append_event(
        _HASH,
        "message",
        payload,
        ref_table="companion_messages",
        ref_id=7,
        packet_hash="abc123",
    )
    assert seq is not None

    events = h.companion_get_events(_HASH, after_seq=0)
    assert len(events) == 1
    event = events[0]
    assert event["seq"] == seq
    assert event["event_type"] == "message"
    assert event["packet_hash"] == "abc123"
    assert isinstance(event["created_at"], float)
    assert event["payload"] == payload


def test_event_payload_parse_failure_fails_closed_without_returning_raw(
    tmp_path, caplog
):
    h = _handler(tmp_path)
    seq = h.companion_append_event(_HASH, "message", {"ok": True})
    assert seq is not None

    # Corrupt the stored payload directly to simulate unparseable JSON.
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "UPDATE companion_events SET payload = ? WHERE seq = ?", ("{not json", seq)
    )
    conn.commit()
    conn.close()

    with caplog.at_level(logging.ERROR, logger="SQLiteHandler"):
        events = h.companion_get_events(_HASH, after_seq=0)

    # The compatibility reader cannot signal storage failure to its caller, so
    # it omits the corrupt page and logs it. Never surface malformed storage as
    # a synthetic event or expose its raw contents.
    assert events == []
    assert "{not json" not in repr(events)
    assert "invalid JSON payload" in caplog.text

    # The strict mobile sync path can signal failure and must do so.
    state = h.companion_sync_state(_HASH)
    with pytest.raises(CompanionStorageError, match="invalid JSON payload"):
        h.companion_sync_page(_HASH, state["epoch"], after_seq=0)


# --- journal_head, epoch stability ---


def test_journal_head_zero_when_empty(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_journal_head(_HASH) == 0


def test_journal_head_tracks_max_seq_per_companion(tmp_path):
    h = _handler(tmp_path)
    h.companion_append_event(_HASH, "message", {})
    seq2 = h.companion_append_event(_HASH, "message", {})
    h.companion_append_event(_HASH2, "message", {})

    assert h.companion_journal_head(_HASH) == seq2
    assert h.companion_journal_head(_HASH2) != h.companion_journal_head(_HASH)


def test_journal_epoch_stable_across_calls_and_instances(tmp_path):
    h1 = _handler(tmp_path)
    epoch1 = h1.companion_journal_epoch()
    epoch1_again = h1.companion_journal_epoch()
    assert epoch1 == epoch1_again

    h2 = _handler(tmp_path)
    epoch2 = h2.companion_journal_epoch()
    assert epoch2 == epoch1


def test_journal_meta_get_set(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_journal_meta_get("some_key") is None
    assert h.companion_journal_meta_set("some_key", "some_value") is True
    assert h.companion_journal_meta_get("some_key") == "some_value"
    assert h.companion_journal_meta_set("some_key", "updated") is True
    assert h.companion_journal_meta_get("some_key") == "updated"


# --- Prune: floor advancement, cursor-below-floor, AUTOINCREMENT reuse ---


def test_prune_events_advances_floor_to_max_deleted_seq(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)

    seqs = []
    for i in range(3):
        seq = h.companion_append_event(_HASH, "message", {"n": i})
        seqs.append(seq)

    # Backdate all three rows so they're eligible for a 31-day prune.
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_events SET created_at = ?", (old_time,))
    conn.commit()
    conn.close()

    # A fresh row that must survive the prune.
    fresh_seq = h.companion_append_event(_HASH, "message", {"n": "fresh"})

    deleted = h.companion_prune_events(max_age_days=31)
    assert deleted == 3

    floor = h.companion_journal_meta_get("prune_floor")
    assert floor is not None
    assert int(floor) == max(seqs)

    remaining = h.companion_get_events(_HASH, after_seq=0)
    assert [e["seq"] for e in remaining] == [fresh_seq]


def test_prune_events_locks_before_floor_scan_and_delete(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)
    assert h.companion_append_event(
        _HASH,
        "message",
        {"n": 1},
        created_at=old_time,
    )
    trace = []
    conn = h._connect()
    conn.set_trace_callback(trace.append)
    try:
        assert h.companion_prune_events(max_age_days=31) == 1
    finally:
        conn.set_trace_callback(None)

    statements = [statement.strip().upper() for statement in trace]
    begin = statements.index("BEGIN IMMEDIATE")
    floor_scan = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT COMPANION_HASH, MAX(SEQ)")
    )
    delete = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("DELETE FROM COMPANION_EVENTS")
    )
    commit = statements.index("COMMIT")
    assert begin < floor_scan < delete < commit


def test_prune_events_floor_never_regresses(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)

    seq1 = h.companion_append_event(_HASH, "message", {})
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_events SET created_at = ? WHERE seq = ?", (old_time, seq1))
    conn.commit()
    conn.close()

    deleted1 = h.companion_prune_events(max_age_days=31)
    assert deleted1 == 1
    floor1 = int(h.companion_journal_meta_get("prune_floor"))
    assert floor1 == seq1

    # A prune run that finds nothing old must not lower the floor.
    deleted2 = h.companion_prune_events(max_age_days=31)
    assert deleted2 == 0
    floor2 = int(h.companion_journal_meta_get("prune_floor"))
    assert floor2 == floor1


def test_cursor_below_prune_floor_is_detectable(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)

    seqs = [h.companion_append_event(_HASH, "message", {}) for _ in range(2)]
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_events SET created_at = ?", (old_time,))
    conn.commit()
    conn.close()

    h.companion_prune_events(max_age_days=31)
    floor = int(h.companion_journal_meta_get("prune_floor"))

    # A client cursor at or above the floor is still valid; anything below
    # it must be treated by callers as snapshot_required (design doc §5.3).
    stale_cursor = min(seqs) - 1 if min(seqs) > 0 else 0
    assert stale_cursor < floor
    assert floor >= max(seqs)


def test_autoincrement_prevents_seq_reuse_after_prune(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)

    seq1 = h.companion_append_event(_HASH, "message", {"n": 1})
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_events SET created_at = ? WHERE seq = ?", (old_time, seq1))
    conn.commit()
    conn.close()

    deleted = h.companion_prune_events(max_age_days=31)
    assert deleted == 1
    assert h.companion_get_events(_HASH, after_seq=0) == []

    # A brand-new event after the table is empty must get a seq strictly
    # greater than the deleted row's seq, never reusing it.
    new_seq = h.companion_append_event(_HASH, "message", {"n": 2})
    assert new_seq is not None
    assert new_seq > seq1


# --- Soft-consume: pop, push capacity/eviction, consumed-row pruning ---


def _msg(packet_hash, is_channel=False, text="hi"):
    return {
        "sender_key": b"\x11" * 32,
        "text": text,
        "timestamp": 1000,
        "txt_type": 0,
        "is_channel": is_channel,
        "channel_idx": 0,
        "path_len": 0,
        "packet_hash": packet_hash,
        "sender_prefix": b"",
    }


def test_pop_marks_consumed_and_strips_id(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1"))

    popped = h.companion_pop_message(_HASH)
    assert popped is not None
    assert "id" not in popped
    assert popped["text"] == "hi"

    conn = sqlite3.connect(str(h.sqlite_path))
    row = conn.execute(
        "SELECT consumed_at FROM companion_messages WHERE packet_hash = 'p1'"
    ).fetchone()
    conn.close()
    assert row[0] is not None


def test_second_pop_returns_next_unconsumed_message(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1", text="first"))
    assert h.companion_push_message(_HASH, _msg("p2", text="second"))

    first = h.companion_pop_message(_HASH)
    assert first["text"] == "first"

    second = h.companion_pop_message(_HASH)
    assert second["text"] == "second"

    assert h.companion_pop_message(_HASH) is None


def test_concurrent_pop_claims_each_message_once(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("only", text="only"))
    start = threading.Barrier(9)

    def pop_once(_unused):
        start.wait()
        return h.companion_pop_message(_HASH)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(pop_once, number) for number in range(8)]
        start.wait()
        results = [future.result() for future in futures]

    delivered = [result for result in results if result is not None]
    assert [message["text"] for message in delivered] == ["only"]
    assert h.companion_pop_message(_HASH) is None


def test_popped_messages_still_visible_via_get_messages(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1", text="first"))
    assert h.companion_push_message(_HASH, _msg("p2", text="second"))

    h.companion_pop_message(_HASH)

    history = h.companion_get_messages(_HASH)
    # Newest-first; both rows retained even though one is consumed.
    assert [m["text"] for m in history] == ["second", "first"]
    consumed_flags = {m["text"]: m["consumed_at"] for m in history}
    assert consumed_flags["first"] is not None
    assert consumed_flags["second"] is None
    # Bytes fields are hex-encoded strings, not raw bytes.
    assert isinstance(history[0]["sender_key"], str)
    bytes.fromhex(history[0]["sender_key"])  # must not raise


def test_get_messages_before_id_paging_and_limit_clamp(tmp_path):
    h = _handler(tmp_path)
    for i in range(5):
        assert h.companion_push_message(_HASH, _msg(f"p{i}", text=str(i)))

    first_page = h.companion_get_messages(_HASH, limit=2)
    assert [m["text"] for m in first_page] == ["4", "3"]

    next_page = h.companion_get_messages(_HASH, before_id=first_page[-1]["id"], limit=2)
    assert [m["text"] for m in next_page] == ["2", "1"]

    # Limit clamp: request absurdly high/low, still bounded to [1, 200].
    assert len(h.companion_get_messages(_HASH, limit=0)) == 1
    assert len(h.companion_get_messages(_HASH, limit=10000)) == 5


def test_push_capacity_counts_only_unconsumed_rows(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1", is_channel=True), max_messages=1)
    h.companion_pop_message(_HASH)  # consume it; queue is now "empty" for capacity purposes

    # A second push at max_messages=1 must succeed since the only prior row
    # is consumed and doesn't count toward capacity.
    assert h.companion_push_message(_HASH, _msg("p2", is_channel=True), max_messages=1)


def test_push_eviction_marks_channel_messages_consumed_not_deleted(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1", is_channel=True, text="old"), max_messages=1)
    assert h.companion_push_message(_HASH, _msg("p2", is_channel=True, text="new"), max_messages=1)

    # Eviction happened: only one unconsumed row remains and it's the new one.
    popped = h.companion_pop_message(_HASH)
    assert popped["text"] == "new"
    assert h.companion_pop_message(_HASH) is None

    # But the evicted row was marked consumed, not deleted -- still visible
    # as history.
    history = h.companion_get_messages(_HASH)
    assert {m["text"] for m in history} == {"old", "new"}
    old_row = next(m for m in history if m["text"] == "old")
    assert old_row["consumed_at"] is not None


def test_push_never_evicts_direct_messages(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(
        _HASH, _msg("p1", is_channel=False, text="direct"), max_messages=1
    )

    # No unconsumed channel rows exist to evict, and the direct message may
    # not be displaced: the push must be rejected.
    assert h.companion_push_message(
        _HASH, _msg("p2", is_channel=True, text="channel"), max_messages=1
    ) is False

    # The direct message is untouched and still poppable.
    popped = h.companion_pop_message(_HASH)
    assert popped["text"] == "direct"
    assert h.companion_pop_message(_HASH) is None


def test_push_returns_false_when_it_cannot_make_room_leaves_state_unchanged(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("p1", is_channel=False), max_messages=1)

    before_count = len(h.companion_get_messages(_HASH))
    ok = h.companion_push_message(_HASH, _msg("p2", is_channel=True), max_messages=1)
    assert ok is False
    after_count = len(h.companion_get_messages(_HASH))

    # Rejected push must not leave a partial insert behind.
    assert after_count == before_count


def test_prune_consumed_messages_deletes_only_old_consumed_rows(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, _msg("old-consumed", text="old-consumed"))
    assert h.companion_push_message(_HASH, _msg("recent-consumed", text="recent-consumed"))
    assert h.companion_push_message(_HASH, _msg("unconsumed", text="unconsumed"))

    old_popped = h.companion_pop_message(_HASH)
    assert old_popped["text"] == "old-consumed"
    recent_popped = h.companion_pop_message(_HASH)
    assert recent_popped["text"] == "recent-consumed"
    # Third message ("unconsumed") is left in the live queue.

    # Backdate only the first consumed row so it's the sole prune target.
    conn = sqlite3.connect(str(h.sqlite_path))
    old_time = time.time() - (40 * 86400)
    conn.execute(
        "UPDATE companion_messages SET consumed_at = ? WHERE packet_hash = 'old-consumed'",
        (old_time,),
    )
    conn.commit()
    conn.close()

    deleted = h.companion_prune_consumed_messages(max_age_days=31)
    assert deleted == 1

    remaining_texts = {m["text"] for m in h.companion_get_messages(_HASH)}
    assert remaining_texts == {"recent-consumed", "unconsumed"}


def test_cleanup_old_data_wires_companion_prune_methods(tmp_path):
    h = _handler(tmp_path)
    old_time = time.time() - (40 * 86400)

    seq = h.companion_append_event(_HASH, "message", {})
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute("UPDATE companion_events SET created_at = ? WHERE seq = ?", (old_time, seq))
    conn.commit()
    conn.close()

    assert h.companion_push_message(_HASH, _msg("to-prune", text="to-prune"))
    h.companion_pop_message(_HASH)
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "UPDATE companion_messages SET consumed_at = ? WHERE packet_hash = 'to-prune'",
        (old_time,),
    )
    conn.commit()
    conn.close()

    # Default retention (no companion_events_days passed) is 31 days, which
    # must catch both of the 40-day-old rows above.
    h.cleanup_old_data(days=31)

    assert h.companion_get_events(_HASH, after_seq=0) == []
    assert h.companion_get_messages(_HASH) == []
