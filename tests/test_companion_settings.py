"""Tests for per-companion bridge settings parsing and startup guard."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from repeater.companion.utils import (
    COMPANION_SETTINGS_ALLOWLIST,
    CompanionContactCapacityError,
    check_companion_contact_capacity,
    effective_max_contacts,
    enforce_companion_contact_capacity,
    merge_companion_settings_update,
    parse_companion_bridge_kwargs,
    parse_positive_int,
    select_companion_contacts_to_trim,
    trim_companion_contacts_to_fit,
    validate_companion_config_capacity,
)

# openhop_core defaults (CompanionBridge / ContactStore)
_DEFAULT_MAX_CONTACTS = 1000


class TestParsePositiveInt:
    def test_valid(self):
        assert parse_positive_int("100", "max_contacts") == 100

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int("abc", "max_contacts")

    def test_below_minimum(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int(0, "max_contacts")


class TestParseCompanionBridgeKwargs:
    def test_empty_settings(self):
        assert parse_companion_bridge_kwargs({}) == {}

    def test_max_contacts_and_offline_queue(self):
        assert parse_companion_bridge_kwargs(
            {"max_contacts": 2000, "offline_queue_size": 1024}
        ) == {"max_contacts": 2000, "offline_queue_size": 1024}

    def test_ignored_keys_warn(self, caplog):
        caplog.set_level(logging.WARNING)
        result = parse_companion_bridge_kwargs(
            {"max_contacts": 500, "max_channels": 64, "adv_type": 2}
        )
        assert result == {"max_contacts": 500}
        assert any("max_channels" in r.message for r in caplog.records)
        assert any("adv_type" in r.message for r in caplog.records)

    def test_invalid_max_contacts(self):
        with pytest.raises(ValueError):
            parse_companion_bridge_kwargs({"max_contacts": -1})


class TestEffectiveMaxContacts:
    def test_default(self):
        assert effective_max_contacts({}) == _DEFAULT_MAX_CONTACTS

    def test_override(self):
        assert effective_max_contacts({"max_contacts": 500}) == 500


class TestMergeCompanionSettingsUpdate:
    def test_merges_bridge_settings(self):
        merged = merge_companion_settings_update(
            {"node_name": "a"},
            {"max_contacts": 500},
        )
        assert merged == {"node_name": "a", "max_contacts": 500}

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="Unknown companion setting"):
            merge_companion_settings_update({}, {"max_channels": 64})


class TestValidateCompanionConfigCapacity:
    def test_uses_merged_settings_not_stale_identity(self):
        identity = {
            "identity_key": "aa" * 32,
            "settings": {"max_contacts": 1000},
        }
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 600
        with pytest.raises(CompanionContactCapacityError):
            validate_companion_config_capacity(
                identity,
                sqlite,
                settings={"max_contacts": 500},
            )
        sqlite.companion_count_contacts.assert_called_once()


class TestCheckCompanionContactCapacity:
    def test_skips_without_sqlite(self):
        check_companion_contact_capacity("0x01", 100, None)

    def test_passes_when_under_limit(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 100
        check_companion_contact_capacity("0x01", 500, sqlite)

    def test_raises_when_over_limit(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 812
        with pytest.raises(CompanionContactCapacityError) as exc:
            check_companion_contact_capacity("0xab", 500, sqlite, companion_name="BotCompanion")
        assert exc.value.stored_count == 812
        assert exc.value.max_contacts == 500
        assert "BotCompanion" in str(exc.value)


class TestOfflineQueueOff:
    def test_zero_allowed(self):
        assert parse_companion_bridge_kwargs({"offline_queue_size": 0}) == {"offline_queue_size": 0}

    def test_max_contacts_zero_still_rejected(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_companion_bridge_kwargs({"max_contacts": 0})


class TestSelectCompanionContactsToTrim:
    @staticmethod
    def _c(pk, flags=0, lastmod=0):
        return {"pubkey": pk, "flags": flags, "lastmod": lastmod}

    def test_under_limit_keeps_all(self):
        contacts = [self._c(b"\x01"), self._c(b"\x02")]
        keep, removed = select_companion_contacts_to_trim(contacts, 5)
        assert removed == []
        assert keep == contacts

    def test_evicts_oldest_non_favourite_and_protects_favourites(self):
        contacts = [
            self._c(b"\x01", lastmod=10),
            self._c(b"\x02", lastmod=30),
            self._c(b"\x03", flags=1, lastmod=5),  # favourite + oldest -> protected
            self._c(b"\x04", lastmod=20),
        ]
        keep, removed = select_companion_contacts_to_trim(contacts, 2)
        assert {c["pubkey"] for c in keep} == {b"\x03", b"\x02"}
        assert {c["pubkey"] for c in removed} == {b"\x01", b"\x04"}

    def test_refuses_when_favourites_exceed_limit(self):
        contacts = [
            self._c(b"\x01", flags=1, lastmod=1),
            self._c(b"\x02", flags=1, lastmod=2),
        ]
        with pytest.raises(ValueError, match="favourite"):
            select_companion_contacts_to_trim(contacts, 1)


class TestSqliteRetentionTrim:
    @staticmethod
    def _handler(tmp_path):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        return SQLiteHandler(tmp_path)

    @staticmethod
    def _push(h, companion_hash, i, max_messages=None):
        return h.companion_push_message(
            companion_hash,
            {"text": f"m{i}", "timestamp": i, "packet_hash": f"{companion_hash}-{i}"},
            max_messages=max_messages,
        )

    def test_trims_to_max_messages(self, tmp_path):
        h = self._handler(tmp_path)
        for i in range(5):
            self._push(h, "0x01", i, max_messages=3)
        assert len(h.companion_load_messages("0x01")) == 3

    def test_none_keeps_all(self, tmp_path):
        h = self._handler(tmp_path)
        for i in range(5):
            self._push(h, "0x01", i, max_messages=None)
        assert len(h.companion_load_messages("0x01")) == 5

    def test_trim_isolated_per_companion(self, tmp_path):
        h = self._handler(tmp_path)
        for i in range(4):
            self._push(h, "0x01", i, max_messages=2)
        for i in range(3):
            self._push(h, "0x02", i, max_messages=None)
        assert len(h.companion_load_messages("0x01")) == 2
        assert len(h.companion_load_messages("0x02")) == 3


class TestSenderPrefixPersistence:
    """sender_prefix (signed room-post author prefix) survives the SQLite round-trip."""

    PREFIX = b"\xaa\xbb\xcc\xdd"

    @staticmethod
    def _handler(tmp_path):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        return SQLiteHandler(tmp_path)

    def _push(self, h, sender_prefix=PREFIX):
        return h.companion_push_message(
            "0x01",
            {
                "sender_key": b"\x01" * 32,
                "txt_type": 2,
                "timestamp": 42,
                "text": "signed post",
                "sender_prefix": sender_prefix,
                "packet_hash": "ph-1",
            },
        )

    def test_push_pop_round_trip(self, tmp_path):
        h = self._handler(tmp_path)
        assert self._push(h)
        msg = h.companion_pop_message("0x01")
        assert msg["sender_prefix"] == self.PREFIX
        assert msg["text"] == "signed post"

    def test_load_messages_returns_prefix_bytes(self, tmp_path):
        h = self._handler(tmp_path)
        assert self._push(h)
        msgs = h.companion_load_messages("0x01")
        assert len(msgs) == 1
        assert msgs[0]["sender_prefix"] == self.PREFIX

    def test_missing_prefix_defaults_empty(self, tmp_path):
        h = self._handler(tmp_path)
        assert h.companion_push_message(
            "0x01", {"text": "plain", "timestamp": 1, "packet_hash": "ph-2"}
        )
        msg = h.companion_pop_message("0x01")
        assert msg["sender_prefix"] == b""

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        import sqlite3

        # Build a current DB, then rewind companion_messages to the
        # pre-sender_prefix schema and drop the migration marker.
        h = self._handler(tmp_path)
        conn = sqlite3.connect(str(h.sqlite_path))
        conn.execute(
            "DELETE FROM migrations "
            "WHERE migration_name = 'add_sender_prefix_to_companion_messages'"
        )
        conn.execute("ALTER TABLE companion_messages RENAME TO companion_messages_old")
        conn.execute(
            """
            CREATE TABLE companion_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                companion_hash TEXT NOT NULL,
                sender_key BLOB NOT NULL,
                txt_type INTEGER NOT NULL DEFAULT 0,
                timestamp INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                is_channel INTEGER NOT NULL DEFAULT 0,
                channel_idx INTEGER NOT NULL DEFAULT 0,
                path_len INTEGER NOT NULL DEFAULT 0,
                packet_hash TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute("DROP TABLE companion_messages_old")
        conn.execute(
            "INSERT INTO companion_messages "
            "(companion_hash, sender_key, text, created_at) VALUES ('0x01', X'01', 'old', 1.0)"
        )
        conn.commit()
        conn.close()

        h2 = self._handler(tmp_path)  # re-runs migrations
        # Pre-migration row decodes with an empty prefix.
        old = h2.companion_pop_message("0x01")
        assert old["sender_prefix"] == b""
        # New rows round-trip through the migrated column.
        assert self._push(h2)
        assert h2.companion_pop_message("0x01")["sender_prefix"] == self.PREFIX

    def test_sync_next_from_persistence_rebuilds_prefix(self):
        from repeater.companion.frame_server import CompanionFrameServer

        fs = CompanionFrameServer.__new__(CompanionFrameServer)
        fs.sqlite_handler = MagicMock()
        fs.companion_hash = "0x01"
        fs.sqlite_handler.companion_pop_message.return_value = {
            "sender_key": b"\x01" * 32,
            "txt_type": 2,
            "timestamp": 42,
            "text": "signed post",
            "is_channel": 0,
            "channel_idx": 0,
            "path_len": 0,
            "sender_prefix": self.PREFIX,
        }
        msg = fs._sync_next_from_persistence()
        assert msg.sender_prefix == self.PREFIX


class TestTrimContactsOnOverflowPolicy:
    @staticmethod
    def _contacts(n, favourites=0):
        out = []
        for i in range(n):
            flags = 1 if i < favourites else 0
            out.append({"pubkey": i.to_bytes(2, "big"), "flags": flags, "lastmod": i})
        return out

    def test_allowlist_includes_policy_key(self):
        assert "trim_contacts_on_overflow" in COMPANION_SETTINGS_ALLOWLIST
        # And it is accepted by the settings merge.
        merged = merge_companion_settings_update({}, {"trim_contacts_on_overflow": True})
        assert merged == {"trim_contacts_on_overflow": True}

    def test_trim_helper_persists_kept_set(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(5)
        sqlite.companion_save_contacts.return_value = True
        removed = trim_companion_contacts_to_fit(sqlite, "0x01", 3)
        assert removed == 2
        saved_hash, saved_contacts = sqlite.companion_save_contacts.call_args[0]
        assert saved_hash == "0x01"
        assert len(saved_contacts) == 3

    def test_trim_helper_noop_when_under_limit(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(2)
        assert trim_companion_contacts_to_fit(sqlite, "0x01", 5) == 0
        sqlite.companion_save_contacts.assert_not_called()

    def test_enforce_guards_by_default(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 600
        with pytest.raises(CompanionContactCapacityError):
            enforce_companion_contact_capacity("0x01", 500, sqlite)
        sqlite.companion_save_contacts.assert_not_called()

    def test_enforce_trims_when_policy_enabled(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(600)
        sqlite.companion_save_contacts.return_value = True
        removed = enforce_companion_contact_capacity("0x01", 500, sqlite, trim=True)
        assert removed == 100


class TestPersistSkipWhenOff:
    @staticmethod
    def _frame_server(max_size):
        from repeater.companion.frame_server import CompanionFrameServer

        fs = CompanionFrameServer.__new__(CompanionFrameServer)
        fs.sqlite_handler = MagicMock()
        fs.companion_hash = "0x01"
        bridge = MagicMock()
        bridge.message_queue.max_size = max_size
        fs.bridge = bridge
        return fs

    def test_skips_persistence_when_retention_zero(self):
        import asyncio

        fs = self._frame_server(0)
        asyncio.run(fs._persist_companion_message({"text": "x"}))
        fs.sqlite_handler.companion_push_message.assert_not_called()
        fs.bridge.message_queue.pop_last.assert_called_once()

    def test_persists_with_retention(self):
        import asyncio

        fs = self._frame_server(7)
        asyncio.run(fs._persist_companion_message({"text": "x"}))
        fs.sqlite_handler.companion_push_message.assert_called_once_with("0x01", {"text": "x"}, 7)


class TestImportRepeaterContactsCap:
    """The import endpoint must never leave persisted contacts above max_contacts.

    The bulk import writes straight to SQLite, bypassing the ContactStore cap, so the
    endpoint trims favourite-aware to fit after the insert.
    """

    _HASH = "0x01"

    @staticmethod
    def _handler(tmp_path):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        return SQLiteHandler(tmp_path)

    @staticmethod
    def _seed_adverts(h, n, start_ts=10_000):
        """Seed ``n`` repeater adverts with increasing last_seen (newest = highest i)."""
        for i in range(n):
            h.store_advert(
                {
                    "timestamp": float(start_ts + i),
                    "pubkey": f"{i:064x}",
                    "node_name": f"adv-{i}",
                    "is_repeater": True,
                    "route_type": 1,
                    "contact_type": "repeater",
                    "latitude": 0.0,
                    "longitude": 0.0,
                }
            )

    @classmethod
    def _save_contacts(cls, h, contacts):
        assert h.companion_save_contacts(cls._HASH, contacts)

    @staticmethod
    def _contact(pk_int, *, flags=0, lastmod=0):
        # Pre-existing contacts use a pubkey range disjoint from seeded adverts.
        return {
            "pubkey": (1_000_000 + pk_int).to_bytes(8, "big"),
            "name": f"pre-{pk_int}",
            "adv_type": 2,
            "flags": flags,
            "lastmod": lastmod,
            "last_advert_timestamp": lastmod,
        }

    @classmethod
    def _endpoint(cls, handler, bridge, body):
        from repeater.web.companion_endpoints import CompanionAPIEndpoints

        ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
        ep._require_post = lambda: None
        ep._get_json_body = lambda: body
        ep._resolve_bridge_params = lambda b: {}
        ep._get_bridge = lambda **kw: bridge
        ep._get_sqlite_handler = lambda: handler
        return ep

    @staticmethod
    def _invoke(ep):
        """Call the endpoint past the @require_auth wrapper (no auth context in tests)."""
        from repeater.web.companion_endpoints import CompanionAPIEndpoints

        return CompanionAPIEndpoints.import_repeater_contacts.__wrapped__(ep)

    @classmethod
    def _bridge(cls, max_contacts):
        contacts = SimpleNamespace(max_contacts=max_contacts, loaded=None)
        contacts.load_from_dicts = lambda records: setattr(contacts, "loaded", list(records))
        return SimpleNamespace(_companion_hash=cls._HASH, contacts=contacts)

    def test_import_over_cap_trims_to_fit(self, tmp_path):
        h = self._handler(tmp_path)
        self._seed_adverts(h, 60)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 50
        assert resp["data"] == {"imported": 60, "removed": 10}
        assert len(bridge.contacts.loaded) == 50

    def test_pre_existing_plus_import_accumulation(self, tmp_path):
        h = self._handler(tmp_path)
        # 40 old pre-existing contacts (lastmod 0..39).
        self._save_contacts(h, [self._contact(i, lastmod=i) for i in range(40)])
        # 30 newer imported adverts (last_seen >= 10_000).
        self._seed_adverts(h, 30)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 50
        assert resp["data"]["imported"] == 30
        # All 30 newer imports survive; oldest pre-existing are evicted.
        kept = {row["pubkey"] for row in h.companion_load_contacts(self._HASH)}
        for i in range(30):
            assert bytes.fromhex(f"{i:064x}") in kept

    def test_favourites_protected(self, tmp_path):
        h = self._handler(tmp_path)
        # 5 favourites that are also the oldest (lastmod 0..4).
        favourites = [self._contact(i, flags=1, lastmod=i) for i in range(5)]
        self._save_contacts(h, favourites)
        self._seed_adverts(h, 60)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 50
        kept = {row["pubkey"] for row in h.companion_load_contacts(self._HASH)}
        for fav in favourites:
            assert fav["pubkey"] in kept

    def test_favourites_exceed_cap_returns_409(self, tmp_path):
        import cherrypy

        h = self._handler(tmp_path)
        self._save_contacts(h, [self._contact(i, flags=1, lastmod=i) for i in range(51)])
        self._seed_adverts(h, 1)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        with pytest.raises(cherrypy.HTTPError) as exc_info:
            self._invoke(ep)
        assert exc_info.value.code == 409

    def test_cap_source_is_contacts_not_default(self, tmp_path):
        # A companion configured above the 1000 default must not be silently clamped.
        h = self._handler(tmp_path)
        captured = {}
        real_import = h.companion_import_repeater_contacts

        def _spy(companion_hash, **kwargs):
            captured["limit"] = kwargs.get("limit")
            return real_import(companion_hash, **kwargs)

        h.companion_import_repeater_contacts = _spy
        bridge = self._bridge(max_contacts=1200)
        ep = self._endpoint(h, bridge, {"companion_name": "c", "limit": 1100})

        self._invoke(ep)

        # min(limit=1100, max_contacts=1200) -> 1100, proving the cap came from
        # bridge.contacts.max_contacts (1200), not the old 1000 fallback.
        assert captured["limit"] == 1100

    def test_under_cap_import_is_noop_trim(self, tmp_path):
        # Happy path: an import that fits leaves everything and trims nothing.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 10)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 10
        assert resp["data"] == {"imported": 10, "removed": 0}
        assert len(bridge.contacts.loaded) == 10

    def test_incident_scale_default_cap(self, tmp_path):
        # Reproduces the reported incident: an oversized import at the real 1000
        # default must end at exactly the cap, not 1062.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 1062)
        bridge = self._bridge(max_contacts=_DEFAULT_MAX_CONTACTS)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == _DEFAULT_MAX_CONTACTS
        assert resp["data"] == {"imported": 1062, "removed": 62}
        assert len(bridge.contacts.loaded) == _DEFAULT_MAX_CONTACTS

    def test_repeated_import_stays_within_cap(self, tmp_path):
        # Repeated imports (a plausible cause of the original overflow) must never
        # accumulate past the cap.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 60)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        first = self._invoke(ep)
        assert h.companion_count_contacts(self._HASH) == 50
        assert first["data"]["removed"] == 10

        # Second call re-imports the same adverts (the 10 trimmed are still in the
        # adverts table) and must trim back to the cap again, not climb to 60.
        second = self._invoke(ep)
        assert h.companion_count_contacts(self._HASH) == 50
        assert second["data"]["removed"] == 10
