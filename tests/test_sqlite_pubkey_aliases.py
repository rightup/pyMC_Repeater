import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler


# ---------------------------------------------------------------------------
# Fixture — SQLiteHandler backed by a real in-memory SQLite database
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """Return a SQLiteHandler whose connection is an in-memory SQLite DB."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Patch sqlite3.connect so that *all* SQLiteHandler._connect calls reuse
    # this single in-memory connection regardless of the path argument.
    with patch("repeater.data_acquisition.sqlite_handler.sqlite3.connect", return_value=conn):
        handler = SQLiteHandler(Path(":memory:"))

    # Force the thread-local to always return our connection so that
    # subsequent method calls (after __init__) keep using it.
    handler._local.conn = conn
    with patch.object(handler, "_connect", return_value=conn):
        yield handler

    conn.close()


# ===========================================================================
# set_pubkey_alias
# ===========================================================================

class TestSetPubkeyAlias:

    def test_insert_new_alias_returns_true(self, db):
        assert db.set_pubkey_alias("aabbcc", "Alice") is True

    def test_inserted_alias_is_retrievable(self, db):
        db.set_pubkey_alias("aabbcc", "Alice")
        assert db.get_pubkey_alias("aabbcc") == "Alice"

    def test_upsert_updates_existing_alias(self, db):
        db.set_pubkey_alias("aabbcc", "Alice")
        db.set_pubkey_alias("aabbcc", "Bob")
        assert db.get_pubkey_alias("aabbcc") == "Bob"

    def test_pubkey_stored_lowercase(self, db):
        db.set_pubkey_alias("AABBCC", "Alice")
        assert db.get_pubkey_alias("aabbcc") == "Alice"

    def test_alias_stripped_of_whitespace(self, db):
        db.set_pubkey_alias("aabbcc", "  Alice  ")
        assert db.get_pubkey_alias("aabbcc") == "Alice"

    def test_multiple_distinct_pubkeys_stored_independently(self, db):
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("bb", "Bob")
        assert db.get_pubkey_alias("aa") == "Alice"
        assert db.get_pubkey_alias("bb") == "Bob"


# ===========================================================================
# get_pubkey_alias
# ===========================================================================

class TestGetPubkeyAlias:

    def test_returns_alias_when_found(self, db):
        db.set_pubkey_alias("deadbeef", "Charlie")
        assert db.get_pubkey_alias("deadbeef") == "Charlie"

    def test_returns_none_when_not_found(self, db):
        assert db.get_pubkey_alias("nonexistent") is None

    def test_lookup_case_insensitive_on_pubkey(self, db):
        db.set_pubkey_alias("deadbeef", "Dave")
        assert db.get_pubkey_alias("DEADBEEF") == "Dave"

    def test_empty_table_returns_none(self, db):
        assert db.get_pubkey_alias("anything") is None


# ===========================================================================
# delete_pubkey_alias
# ===========================================================================

class TestDeletePubkeyAlias:

    def test_delete_existing_returns_true(self, db):
        db.set_pubkey_alias("aabbcc", "Alice")
        assert db.delete_pubkey_alias("aabbcc") is True

    def test_deleted_alias_no_longer_retrievable(self, db):
        db.set_pubkey_alias("aabbcc", "Alice")
        db.delete_pubkey_alias("aabbcc")
        assert db.get_pubkey_alias("aabbcc") is None

    def test_delete_nonexistent_returns_false(self, db):
        assert db.delete_pubkey_alias("doesnotexist") is False

    def test_delete_case_insensitive_on_pubkey(self, db):
        db.set_pubkey_alias("aabbcc", "Alice")
        assert db.delete_pubkey_alias("AABBCC") is True
        assert db.get_pubkey_alias("aabbcc") is None

    def test_delete_one_does_not_affect_others(self, db):
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("bb", "Bob")
        db.delete_pubkey_alias("aa")
        assert db.get_pubkey_alias("bb") == "Bob"


# ===========================================================================
# get_all_pubkey_aliases
# ===========================================================================

class TestGetAllPubkeyAliases:

    def test_empty_table_returns_empty_list(self, db):
        assert db.get_all_pubkey_aliases() == []

    def test_single_entry_returned(self, db):
        db.set_pubkey_alias("aabb", "Alice")
        result = db.get_all_pubkey_aliases()
        assert len(result) == 1
        assert result[0]["pubkey"] == "aabb"
        assert result[0]["alias"] == "Alice"

    def test_multiple_entries_all_returned(self, db):
        db.set_pubkey_alias("cc", "Charlie")
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("bb", "Bob")
        assert len(db.get_all_pubkey_aliases()) == 3

    def test_ordered_by_alias_ascending(self, db):
        db.set_pubkey_alias("cc", "Zara")
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("bb", "Mike")
        result = db.get_all_pubkey_aliases()
        aliases = [r["alias"] for r in result]
        assert aliases == sorted(aliases)

    def test_each_entry_has_required_keys(self, db):
        db.set_pubkey_alias("aa", "Alice")
        entry = db.get_all_pubkey_aliases()[0]
        for key in ("pubkey", "alias", "created_at", "updated_at"):
            assert key in entry

    def test_after_delete_entry_absent(self, db):
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("bb", "Bob")
        db.delete_pubkey_alias("aa")
        result = db.get_all_pubkey_aliases()
        assert len(result) == 1
        assert result[0]["alias"] == "Bob"

    def test_upsert_does_not_create_duplicate_entries(self, db):
        db.set_pubkey_alias("aa", "Alice")
        db.set_pubkey_alias("aa", "Alicia")
        assert len(db.get_all_pubkey_aliases()) == 1
