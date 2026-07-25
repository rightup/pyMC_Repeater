"""Tests for per-companion bridge settings parsing and startup guard."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openhop_core.companion import CompanionBridge
from openhop_core.protocol import LocalIdentity

from repeater.companion.utils import (
    COMPANION_SETTINGS_ALLOWLIST,
    CompanionContactCapacityError,
    DEFAULT_COMPANION_TCP_PORT,
    DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
    check_companion_contact_capacity,
    effective_max_contacts,
    enforce_companion_contact_capacity,
    merge_companion_settings_update,
    parse_companion_bridge_kwargs,
    parse_positive_int,
    select_companion_contacts_to_trim,
    trim_companion_contacts_to_fit,
    validate_companion_bind_address,
    validate_companion_boolean_setting,
    validate_companion_config_capacity,
    validate_companion_legacy_adoption,
    validate_companion_listener_config,
    validate_companion_node_name,
    validate_companion_seconds_setting,
    validate_companion_tcp_port,
    validate_companion_tcp_timeout,
)
from repeater.main import RepeaterDaemon
from repeater.data_acquisition.sqlite_handler import CompanionStorageError, SQLiteHandler
from repeater.web.mobile_endpoints import CompanionsV1

# openhop_core defaults (CompanionBridge / ContactStore)
_DEFAULT_MAX_CONTACTS = 1000


def test_companion_seconds_setting_rejects_huge_integer_cleanly():
    with pytest.raises(ValueError, match="between 0.1 and 300 seconds"):
        validate_companion_seconds_setting(
            10**1000,
            "request_timeout_sec",
            minimum=0.1,
            maximum=300.0,
        )


class TestParsePositiveInt:
    def test_valid(self):
        assert parse_positive_int("100", "max_contacts") == 100

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int("abc", "max_contacts")

    @pytest.mark.parametrize("value", [True, False, 1.0, 1.5])
    def test_rejects_json_boolean_and_float_coercion(self, value):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int(value, "max_contacts")

    def test_below_minimum(self):
        with pytest.raises(ValueError, match="max_contacts"):
            parse_positive_int(0, "max_contacts")


class TestCompanionTcpSettings:
    def test_valid_bounds_and_defaults(self):
        assert DEFAULT_COMPANION_TCP_PORT == 5000
        assert DEFAULT_COMPANION_TCP_TIMEOUT_SEC == 8 * 60 * 60
        assert validate_companion_tcp_port(1) == 1
        assert validate_companion_tcp_port(65_535) == 65_535
        assert validate_companion_tcp_timeout(0) == 0
        assert validate_companion_tcp_timeout(DEFAULT_COMPANION_TCP_TIMEOUT_SEC) == (
            DEFAULT_COMPANION_TCP_TIMEOUT_SEC
        )

    @pytest.mark.parametrize("value", [True, "5000", 0, 65_536, None])
    def test_invalid_port_type_or_range(self, value):
        with pytest.raises(ValueError, match="tcp_port"):
            validate_companion_tcp_port(value)

    @pytest.mark.parametrize("value", [True, "120", -1, 2_147_483_648, None])
    def test_invalid_timeout_type_or_range(self, value):
        with pytest.raises(ValueError, match="tcp_timeout"):
            validate_companion_tcp_timeout(value)

    @pytest.mark.parametrize("value", [1, 0, "true", "false", None])
    def test_policy_boolean_rejects_coercion(self, value):
        with pytest.raises(ValueError, match="rf_reception_events"):
            validate_companion_boolean_setting(value, "rf_reception_events")

    def test_policy_boolean_accepts_only_real_booleans(self):
        assert validate_companion_boolean_setting(
            True, "trim_contacts_on_overflow"
        ) is True
        assert validate_companion_boolean_setting(
            False, "trim_contacts_on_overflow"
        ) is False

    @pytest.mark.parametrize(
        "value",
        ["127.0.0.1", "0.0.0.0", "::1", "::", "localhost", "radio.local"],
    )
    def test_valid_bind_addresses(self, value):
        assert validate_companion_bind_address(value) == value

    @pytest.mark.parametrize(
        "value",
        [None, 123, "", "   ", "bad host", "bad\nhost", "\ud800"],
    )
    def test_invalid_bind_addresses(self, value):
        with pytest.raises(ValueError, match="bind_address"):
            validate_companion_bind_address(value)

    def test_node_name_rejects_lone_unicode_surrogate(self):
        with pytest.raises(ValueError, match="valid UTF-8"):
            validate_companion_node_name("\ud800")

    def test_listener_ports_are_unique_across_companions_and_http(self):
        validate_companion_listener_config(
            [
                {"name": "phone", "settings": {"tcp_port": 5000}},
                {"name": "tablet", "settings": {"tcp_port": 5001}},
            ],
            {"enabled": True, "port": 8000},
        )

        with pytest.raises(ValueError, match="tablet.*companion 'phone'"):
            validate_companion_listener_config(
                [
                    {"name": "phone"},
                    {"name": "tablet"},
                ],
                {"enabled": False, "port": 5000},
            )

        with pytest.raises(ValueError, match="Repeater HTTP API"):
            validate_companion_listener_config(
                [{"name": "phone", "settings": {"tcp_port": 8000}}],
                {"enabled": True, "port": 8000},
            )

    def test_disabled_http_port_is_not_reserved(self):
        validate_companion_listener_config(
            [{"name": "phone", "settings": {"tcp_port": 8000}}],
            {"enabled": "false", "port": 8000},
        )


class TestParseCompanionBridgeKwargs:
    def test_empty_settings(self):
        assert parse_companion_bridge_kwargs({}) == {}

    def test_max_contacts_and_offline_queue(self):
        assert parse_companion_bridge_kwargs(
            {"max_contacts": 2000, "offline_queue_size": 1024}
        ) == {"max_contacts": 2000, "offline_queue_size": 1024}

    @pytest.mark.parametrize(
        ("field", "value", "maximum"),
        [
            ("max_contacts", 2001, 2000),
            ("offline_queue_size", 4097, 4096),
        ],
    )
    def test_rejects_values_above_bounded_memory_limits(
        self,
        field,
        value,
        maximum,
    ):
        with pytest.raises(ValueError, match=rf"{field} must be <= {maximum}"):
            parse_companion_bridge_kwargs({field: value})

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


class TestCompanionRadioCapabilities:
    def test_reads_active_radio_state_and_known_sx1262_limit(self):
        # The SX1262 driver declares its 22 dBm limit as a backend attribute
        # (SX1262Radio.max_tx_power_dbm); the daemon no longer string-matches
        # radio_type to recover it.
        radio = SimpleNamespace(
            frequency=868_000_000,
            bandwidth=125_000,
            spreading_factor=7,
            coding_rate=8,
            tx_power=14,
            max_tx_power_dbm=22,
        )
        daemon = RepeaterDaemon.__new__(RepeaterDaemon)
        daemon.config = {"radio_type": "sx1262", "radio": {"frequency": 915_000_000}}
        daemon.repeater_handler = SimpleNamespace(radio_config={"frequency": 915_000_000})
        daemon.radio = radio

        assert RepeaterDaemon._get_companion_radio_settings(daemon) == {
            "frequency": 868_000_000,
            "bandwidth": 125_000,
            "spreading_factor": 7,
            "coding_rate": 8,
            "tx_power": 14,
        }
        assert RepeaterDaemon._get_companion_max_tx_power_dbm(daemon) == 22

    def test_prefers_backend_declared_maximum(self):
        daemon = RepeaterDaemon.__new__(RepeaterDaemon)
        daemon.config = {"radio_type": "sx1262"}
        daemon.repeater_handler = SimpleNamespace(radio_config={})
        daemon.radio = SimpleNamespace(max_tx_power_dbm=19)

        assert RepeaterDaemon._get_companion_max_tx_power_dbm(daemon) == 19

    def test_uses_configured_limit_when_backend_cannot_declare_one(self):
        daemon = RepeaterDaemon.__new__(RepeaterDaemon)
        daemon.config = {"radio_type": "kiss"}
        daemon.repeater_handler = SimpleNamespace(radio_config={"max_tx_power_dbm": 15})
        daemon.radio = SimpleNamespace()

        assert RepeaterDaemon._get_companion_max_tx_power_dbm(daemon) == 15

    def test_backend_class_attribute_reaches_self_info_max_tx_power(self):
        # A driver declares its limit as a class attribute (as SX1262Radio
        # does); no radio_type string match is involved.
        class _FakeRadio:
            max_tx_power_dbm = 20

        daemon = RepeaterDaemon.__new__(RepeaterDaemon)
        daemon.config = {"radio_type": "sx1262_ch341"}
        daemon.repeater_handler = SimpleNamespace(radio_config={})
        daemon.radio = _FakeRadio()

        assert RepeaterDaemon._get_companion_max_tx_power_dbm(daemon) == 20

        # The daemon getter is what a bridge is wired with at load time; the
        # value must surface through the companion SELF_INFO max-tx-power path.
        bridge = CompanionBridge(
            LocalIdentity(),
            AsyncMock(return_value=True),
            max_tx_power_getter=daemon._get_companion_max_tx_power_dbm,
        )
        assert bridge.get_max_tx_power_dbm() == 20


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

    def test_validates_tcp_settings_without_coercion(self):
        assert merge_companion_settings_update(
            {},
            {"tcp_port": 6000, "tcp_timeout": 0},
        ) == {"tcp_port": 6000, "tcp_timeout": 0}
        with pytest.raises(ValueError, match="tcp_port"):
            merge_companion_settings_update({}, {"tcp_port": "6000"})

    def test_validates_display_name_and_policy_booleans(self):
        assert merge_companion_settings_update(
            {},
            {
                "node_name": "Human Name",
                "adopt_legacy_namespace": False,
                "trim_contacts_on_overflow": False,
                "rf_reception_events": True,
            },
        ) == {
            "node_name": "Human Name",
            "adopt_legacy_namespace": False,
            "trim_contacts_on_overflow": False,
            "rf_reception_events": True,
        }
        with pytest.raises(ValueError, match="node_name"):
            merge_companion_settings_update({}, {"node_name": "bad\nname"})
        with pytest.raises(ValueError, match="trim_contacts_on_overflow"):
            merge_companion_settings_update(
                {},
                {"trim_contacts_on_overflow": "false"},
            )
        with pytest.raises(ValueError, match="rf_reception_events"):
            merge_companion_settings_update({}, {"rf_reception_events": 1})
        with pytest.raises(ValueError, match="adopt_legacy_namespace"):
            merge_companion_settings_update(
                {},
                {"adopt_legacy_namespace": "true"},
            )

    def test_legacy_adoption_requires_a_real_boolean(self):
        assert validate_companion_legacy_adoption(True) is True
        assert "adopt_legacy_namespace" in COMPANION_SETTINGS_ALLOWLIST
        for value in (1, 0, "true", None):
            with pytest.raises(ValueError, match="adopt_legacy_namespace"):
                validate_companion_legacy_adoption(value)


class TestRfReceptionEventsSetting:
    """Design doc §9 write gate: default off, per-companion opt-in."""

    def test_allowlist_includes_the_key(self):
        assert "rf_reception_events" in COMPANION_SETTINGS_ALLOWLIST

    def test_accepted_by_settings_merge(self):
        merged = merge_companion_settings_update({}, {"rf_reception_events": True})
        assert merged == {"rf_reception_events": True}

    def test_default_is_false_when_absent(self):
        assert validate_companion_boolean_setting(
            False, "rf_reception_events"
        ) is False


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
        results = [self._push(h, "0x01", i, max_messages=3) for i in range(5)]
        assert results == [True, True, True, False, False]
        assert [m["text"] for m in h.companion_load_messages("0x01")] == ["m0", "m1", "m2"]

    def test_evicts_oldest_channel_message_before_direct_message(self, tmp_path):
        h = self._handler(tmp_path)
        direct_one = {"text": "direct one", "packet_hash": "d1", "is_channel": False}
        channel_one = {"text": "channel one", "packet_hash": "c1", "is_channel": True}
        direct_two = {"text": "direct two", "packet_hash": "d2", "is_channel": False}

        assert h.companion_push_message("0x01", direct_one, max_messages=2)
        assert h.companion_push_message("0x01", channel_one, max_messages=2)
        assert h.companion_push_message("0x01", direct_two, max_messages=2)

        messages = h.companion_load_messages("0x01")
        assert [m["text"] for m in messages] == ["direct one", "direct two"]
        assert [m["is_channel"] for m in messages] == [0, 0]

    def test_rejects_channel_when_queue_contains_only_direct_messages(self, tmp_path):
        h = self._handler(tmp_path)
        for packet_hash in ("d1", "d2"):
            assert h.companion_push_message(
                "0x01", {"text": packet_hash, "packet_hash": packet_hash}, max_messages=2
            )

        assert not h.companion_push_message(
            "0x01", {"text": "channel", "packet_hash": "c1", "is_channel": True}, max_messages=2
        )
        assert [m["text"] for m in h.companion_load_messages("0x01")] == ["d1", "d2"]

    def test_rejected_insert_keeps_existing_channels_when_limit_is_lowered(self, tmp_path):
        h = self._handler(tmp_path)
        existing = [
            {"text": "direct one", "packet_hash": "d1", "is_channel": False},
            {"text": "channel one", "packet_hash": "c1", "is_channel": True},
            {"text": "direct two", "packet_hash": "d2", "is_channel": False},
        ]
        for message in existing:
            assert h.companion_push_message("0x01", message)

        assert not h.companion_push_message(
            "0x01", {"text": "incoming", "packet_hash": "d3"}, max_messages=2
        )
        assert [m["text"] for m in h.companion_load_messages("0x01")] == [
            "direct one",
            "channel one",
            "direct two",
        ]

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

    def test_evicts_insertion_oldest_when_clock_steps_backwards(self, tmp_path, monkeypatch):
        from repeater.data_acquisition import sqlite_handler

        h = self._handler(tmp_path)
        for i in range(3):
            assert h.companion_push_message(
                "0x01",
                {"text": f"c{i}", "packet_hash": f"c{i}", "is_channel": True},
                max_messages=3,
            )

        # The incoming row records a created_at older than every existing row.
        # Insertion-order (id) eviction must drop the oldest existing row and
        # keep the new push, rather than treating the incoming row as oldest.
        monkeypatch.setattr(sqlite_handler.time, "time", lambda: 1.0)
        assert h.companion_push_message(
            "0x01",
            {"text": "c3", "packet_hash": "c3", "is_channel": True},
            max_messages=3,
        )

        assert [m["text"] for m in h.companion_load_messages("0x01")] == ["c1", "c2", "c3"]

    def test_lowered_limit_evicts_multiple_channels_in_one_push(self, tmp_path):
        h = self._handler(tmp_path)
        seed = [
            {"text": "d1", "packet_hash": "d1", "is_channel": False},
            {"text": "d2", "packet_hash": "d2", "is_channel": False},
            {"text": "c1", "packet_hash": "c1", "is_channel": True},
            {"text": "c2", "packet_hash": "c2", "is_channel": True},
            {"text": "c3", "packet_hash": "c3", "is_channel": True},
        ]
        for message in seed:
            assert h.companion_push_message("0x01", message)

        assert h.companion_push_message(
            "0x01",
            {"text": "c4", "packet_hash": "c4", "is_channel": True},
            max_messages=4,
        )

        messages = h.companion_load_messages("0x01")
        assert [m["text"] for m in messages] == ["d1", "d2", "c3", "c4"]
        assert [m["is_channel"] for m in messages] == [0, 0, 1, 1]


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
            "WHERE migration_name IN ("
            "'add_sender_prefix_to_companion_messages', "
            "'add_signal_and_channel_data_to_companion_messages', "
            "'add_companion_event_journal')"
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
        fs.sqlite_handler.companion_store_inbound_message.return_value = {
            "inserted": True,
            "message_id": 1,
        }
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

    def test_trim_helper_persists_removals_with_events(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(5)
        removed = trim_companion_contacts_to_fit(sqlite, "0x01", 3)
        assert removed == 2
        saved_hash, changes = sqlite.companion_apply_contact_changes.call_args.args
        assert saved_hash == "0x01"
        assert [change["change"] for change in changes] == ["remove", "remove"]

    def test_trim_helper_noop_when_under_limit(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(2)
        assert trim_companion_contacts_to_fit(sqlite, "0x01", 5) == 0
        sqlite.companion_apply_contact_changes.assert_not_called()

    def test_enforce_guards_by_default(self):
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 600
        with pytest.raises(CompanionContactCapacityError):
            enforce_companion_contact_capacity("0x01", 500, sqlite)
        sqlite.companion_apply_contact_changes.assert_not_called()

    def test_enforce_trims_when_policy_enabled(self):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = self._contacts(600)
        removed = enforce_companion_contact_capacity("0x01", 500, sqlite, trim=True)
        assert removed == 100

    def test_restart_trim_advances_cursor_with_normalized_remove_events(self, tmp_path):
        handler = SQLiteHandler(tmp_path)
        companion_hash = "0x41"
        contacts = [
            {
                "pubkey": bytes([i]) * 32,
                "name": f"c{i}",
                "flags": 0,
                "lastmod": i,
            }
            for i in range(1, 5)
        ]
        assert handler.companion_save_contacts(companion_hash, contacts)
        before = handler.companion_sync_state(companion_hash)

        assert trim_companion_contacts_to_fit(handler, companion_hash, 2) == 2

        page = handler.companion_sync_page(
            companion_hash,
            before["epoch"],
            before["head"],
            10,
        )
        events = [CompanionsV1._event_to_wire(row) for row in page["events"]]
        assert [event["data"]["change"] for event in events] == [
            "remove",
            "remove",
        ]
        assert all("public_key" in event["data"] for event in events)
        assert all("pubkey" not in event["data"] for event in events)
        assert len(handler.companion_load_contacts_strict(companion_hash)) == 2

    def test_trim_failure_rolls_back_rows_and_events(self, tmp_path, monkeypatch):
        handler = SQLiteHandler(tmp_path)
        companion_hash = "0x42"
        contacts = [
            {
                "pubkey": bytes([i]) * 32,
                "name": f"c{i}",
                "flags": 0,
                "lastmod": i,
            }
            for i in range(1, 4)
        ]
        assert handler.companion_save_contacts(companion_hash, contacts)
        before = handler.companion_sync_state(companion_hash)
        monkeypatch.setattr(
            handler,
            "_companion_append_event_row",
            MagicMock(side_effect=RuntimeError("journal unavailable")),
        )

        with pytest.raises(CompanionStorageError):
            trim_companion_contacts_to_fit(handler, companion_hash, 1)

        assert len(handler.companion_load_contacts_strict(companion_hash)) == 3
        assert handler.companion_sync_state(companion_hash)["head"] == before["head"]


class TestFrameMessagePersistence:
    @staticmethod
    def _frame_server(max_size):
        from repeater.companion.frame_server import CompanionFrameServer

        fs = CompanionFrameServer.__new__(CompanionFrameServer)
        fs.sqlite_handler = MagicMock()
        fs.companion_hash = "0x01"
        fs.journal = None
        bridge = MagicMock()
        bridge.message_queue.max_size = max_size
        fs.bridge = bridge
        return fs

    def test_retention_zero_keeps_history_but_not_frame_pending(self):
        import asyncio

        entry = object()
        fs = self._frame_server(0)
        queue_entry = object()
        asyncio.run(fs._persist_companion_message({"text": "x"}, queue_entry))
        fs.sqlite_handler.companion_store_inbound_message.assert_called_once_with(
            "0x01", {"text": "x"}, 0
        )
        fs.bridge.message_queue.remove.assert_called_once_with(queue_entry)

    def test_persists_with_retention(self):
        import asyncio

        fs = self._frame_server(7)
        queue_entry = object()
        asyncio.run(fs._persist_companion_message({"text": "x"}, queue_entry))
        fs.sqlite_handler.companion_store_inbound_message.assert_called_once_with(
            "0x01", {"text": "x"}, 7
        )
        fs.bridge.message_queue.remove.assert_called_once_with(queue_entry)

    def test_deduplicated_message_is_removed_from_memory(self):
        import asyncio

        fs = self._frame_server(7)
        fs.sqlite_handler.companion_store_inbound_message.return_value = {
            "inserted": False,
            "message_id": 1,
        }
        queue_entry = object()
        asyncio.run(fs._persist_companion_message({"text": "x"}, queue_entry))
        fs.bridge.message_queue.remove.assert_called_once_with(queue_entry)

    def test_storage_failure_keeps_memory_message(self):
        import asyncio

        fs = self._frame_server(7)
        fs.sqlite_handler.companion_store_inbound_message.side_effect = RuntimeError(
            "disk failed"
        )
        with pytest.raises(RuntimeError, match="disk failed"):
            asyncio.run(fs._persist_companion_message({"text": "x"}, object()))
        fs.bridge.message_queue.remove.assert_not_called()


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
            "pubkey": (1_000_000 + pk_int).to_bytes(32, "big"),
            "name": f"pre-{pk_int}",
            "adv_type": 2,
            "flags": flags,
            "lastmod": lastmod,
            "last_advert_timestamp": lastmod,
        }

    @classmethod
    def _endpoint(cls, handler, bridge, body):
        from repeater.web.companion_endpoints import CompanionAPIEndpoints

        rows = handler.companion_load_contacts(cls._HASH) or []
        if rows and bridge.contacts.get_count() == 0:
            records = []
            for row in rows:
                record = dict(row)
                record["public_key"] = record.pop("pubkey")
                records.append(record)
            bridge.contacts.load_from_dicts(records)

        async def _persist(changes):
            handler.companion_apply_contact_changes(cls._HASH, changes)

        async def _notify(*_args):
            return None

        bridge._persist_contact_changes = _persist
        bridge._notify_observers = _notify
        ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
        ep._require_post = lambda: None
        ep._get_json_body = lambda: body
        ep._resolve_bridge_params = lambda b: {}
        ep._get_bridge = lambda **kw: bridge
        ep._get_sqlite_handler = lambda: handler
        ep._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
        return ep

    @staticmethod
    def _invoke(ep):
        """Call the endpoint past the @require_auth wrapper (no auth context in tests)."""
        from repeater.web.companion_endpoints import CompanionAPIEndpoints

        return CompanionAPIEndpoints.import_repeater_contacts.__wrapped__(ep)

    @classmethod
    def _bridge(cls, max_contacts):
        from openhop_core.companion.contact_store import ContactStore
        from repeater.companion.bridge import RepeaterCompanionBridge

        return SimpleNamespace(
            _companion_hash=cls._HASH,
            contacts=ContactStore(max_contacts=max_contacts),
            state_mutation_lock=asyncio.Lock(),
            _contact_storage_dict=RepeaterCompanionBridge._contact_storage_dict,
            _contact_changes=RepeaterCompanionBridge._contact_changes,
        )

    def test_import_over_cap_trims_to_fit(self, tmp_path):
        h = self._handler(tmp_path)
        self._seed_adverts(h, 60)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 50
        assert resp["data"] == {
            "imported": 60,
            "added": 50,
            "updated": 0,
            "retained": 0,
            "removed": 10,
        }
        assert bridge.contacts.get_count() == 50

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

    def test_full_favourite_store_rejects_import_without_eviction(self, tmp_path):
        h = self._handler(tmp_path)
        favourites = [self._contact(i, flags=1, lastmod=i) for i in range(50)]
        self._save_contacts(h, favourites)
        self._seed_adverts(h, 1)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        response = self._invoke(ep)

        assert response["data"] == {
            "imported": 1,
            "added": 0,
            "updated": 0,
            "retained": 0,
            "removed": 1,
        }
        kept = {row["pubkey"] for row in h.companion_load_contacts(self._HASH)}
        assert kept == {contact["pubkey"] for contact in favourites}

    def test_cap_source_is_contacts_not_default(self, tmp_path):
        # A companion configured above the 1000 default must not be silently clamped.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 1101)
        bridge = self._bridge(max_contacts=1200)
        ep = self._endpoint(h, bridge, {"companion_name": "c", "limit": 1100})

        response = self._invoke(ep)

        # min(limit=1100, max_contacts=1200) -> 1100, proving the cap came from
        # bridge.contacts.max_contacts (1200), not the old 1000 fallback.
        assert response["data"]["imported"] == 1100
        assert bridge.contacts.get_count() == 1100

    def test_under_cap_import_is_noop_trim(self, tmp_path):
        # Happy path: an import that fits leaves everything and trims nothing.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 10)
        bridge = self._bridge(max_contacts=50)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == 10
        assert resp["data"] == {
            "imported": 10,
            "added": 10,
            "updated": 0,
            "retained": 0,
            "removed": 0,
        }
        assert bridge.contacts.get_count() == 10

    def test_incident_scale_default_cap(self, tmp_path):
        # Reproduces the reported incident: an oversized import at the real 1000
        # default must end at exactly the cap, not 1062.
        h = self._handler(tmp_path)
        self._seed_adverts(h, 1062)
        bridge = self._bridge(max_contacts=_DEFAULT_MAX_CONTACTS)
        ep = self._endpoint(h, bridge, {"companion_name": "c"})

        resp = self._invoke(ep)

        assert h.companion_count_contacts(self._HASH) == _DEFAULT_MAX_CONTACTS
        assert resp["data"] == {
            "imported": 1062,
            "added": 1000,
            "updated": 0,
            "retained": 0,
            "removed": 62,
        }
        assert bridge.contacts.get_count() == _DEFAULT_MAX_CONTACTS

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
