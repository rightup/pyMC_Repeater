"""Companion boot-path hardening: a failed SQLite load must not present as "no data".

Covers _load_companion_rows_verified retry/verification, _restore_companion_state,
and _load_companion_identities failing companion init loudly when persisted rows
exist but cannot be loaded (instead of booting an empty store and backfilling
the Public channel over it).
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import repeater.main as main_module
from repeater.companion.utils import (
    CompanionStateLoadError,
    DEFAULT_COMPANION_TCP_PORT,
    DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
    companion_hash_str_from_identity_key,
)
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.identity_manager import IdentityConfigurationError, IdentityManager
from repeater.main import RepeaterDaemon, _load_companion_rows_verified

_HASH = "0xab"
_NAME = "comp-test"


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(main_module, "_COMPANION_LOAD_RETRY_DELAY_SEC", 0)


class TestLoadCompanionRowsVerified:
    @pytest.mark.asyncio
    async def test_genuinely_empty_returns_without_retry(self):
        loader = MagicMock(return_value=[])
        counter = MagicMock(return_value=0)
        rows, stored = await _load_companion_rows_verified(
            loader, counter, "channels", _HASH, _NAME
        )
        assert rows == []
        assert stored == 0
        assert loader.call_count == 1

    @pytest.mark.asyncio
    async def test_transient_failure_recovers_on_retry(self, caplog):
        good_rows = [{"channel_idx": 0, "name": "Public", "secret": b"x"}]
        loader = MagicMock(side_effect=[None, good_rows])
        counter = MagicMock(return_value=1)
        with caplog.at_level(logging.WARNING):
            rows, stored = await _load_companion_rows_verified(
                loader, counter, "channels", _HASH, _NAME
            )
        assert rows == good_rows
        assert stored == 1
        assert loader.call_count == 2
        assert any("retrying once" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_persistent_failure_raises(self):
        loader = MagicMock(return_value=None)
        counter = MagicMock(return_value=3)
        with pytest.raises(CompanionStateLoadError, match="channels"):
            await _load_companion_rows_verified(loader, counter, "channels", _HASH, _NAME)
        assert loader.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_result_with_stored_rows_raises(self):
        # Load "succeeds" with [] while the table has rows for this hash:
        # treat as a failed load, not as no data.
        loader = MagicMock(return_value=[])
        counter = MagicMock(return_value=5)
        with pytest.raises(CompanionStateLoadError, match="5 row"):
            await _load_companion_rows_verified(loader, counter, "contacts", _HASH, _NAME)
        assert loader.call_count == 2


class TestRestoreCompanionState:
    @staticmethod
    def _bridge(max_size=100):
        bridge = MagicMock()
        bridge.message_queue.max_size = max_size
        bridge.channels.set.return_value = True
        return bridge

    @staticmethod
    def _sqlite(contacts=(), channels=(), messages=()):
        sqlite = MagicMock()
        sqlite.companion_load_contacts.return_value = list(contacts)
        sqlite.companion_count_contacts.return_value = len(contacts)
        sqlite.companion_load_channels.return_value = list(channels)
        sqlite.companion_count_channels.return_value = len(channels)
        sqlite.companion_load_messages.return_value = list(messages)
        sqlite.companion_count_messages.return_value = len(messages)
        return sqlite

    @staticmethod
    def _daemon():
        return RepeaterDaemon({"repeater": {"node_name": "n"}, "logging": {}}, radio=object())

    @pytest.mark.asyncio
    async def test_restores_contacts_and_channels_without_preloading_messages(self):
        daemon = self._daemon()
        bridge = self._bridge()
        sqlite = self._sqlite(
            contacts=[{"pubkey": b"\x01" * 32, "name": "c1"}],
            channels=[{"channel_idx": 1, "name": "ch1", "secret": b"\x02" * 32}],
            messages=[{"sender_key": b"", "text": "hi", "sender_prefix": b""}],
        )
        await daemon._restore_companion_state(sqlite, bridge, _HASH, _NAME)
        bridge.contacts.load_from_dicts.assert_called_once()
        bridge.channels.set.assert_called_once()
        assert bridge.channels.set.call_args[0][0] == 1
        sqlite.companion_load_messages.assert_not_called()
        sqlite.companion_count_messages.assert_not_called()
        bridge.message_queue.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_load_failure_raises_before_bridge_touch(self):
        daemon = self._daemon()
        bridge = self._bridge()
        sqlite = self._sqlite()
        sqlite.companion_load_channels.return_value = None
        sqlite.companion_count_channels.return_value = 2
        with pytest.raises(CompanionStateLoadError):
            await daemon._restore_companion_state(sqlite, bridge, _HASH, _NAME)
        bridge.channels.set.assert_not_called()
        bridge.message_queue.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejected_channel_set_logs_error(self, caplog):
        daemon = self._daemon()
        bridge = self._bridge()
        bridge.channels.set.return_value = False
        sqlite = self._sqlite(channels=[{"channel_idx": 99, "name": "bad", "secret": b""}])
        with caplog.at_level(logging.ERROR):
            await daemon._restore_companion_state(sqlite, bridge, _HASH, _NAME)
        assert any("rejected persisted channel" in r.message for r in caplog.records)


class TestDefaultPublicChannelProvision:
    class _Channels:
        def __init__(self):
            self.values = {}

        def remove(self, index):
            self.values.pop(index, None)

    class _Bridge:
        def __init__(self):
            self.channels = TestDefaultPublicChannelProvision._Channels()

        def get_channel(self, index):
            return self.channels.values.get(index)

        def set_channel(self, index, name, secret):
            self.channels.values[index] = SimpleNamespace(name=name, secret=secret)
            return True

    @pytest.mark.asyncio
    async def test_reprovision_is_durable_and_advances_old_cursor(self, tmp_path):
        handler = SQLiteHandler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        # An earlier explicit clear is already visible to synced clients.
        journal.store_channel(0, None, None)
        before = handler.companion_sync_state(_HASH)
        bridge = self._Bridge()

        await RepeaterDaemon._ensure_default_companion_channel(bridge, journal)

        page = handler.companion_sync_page(
            _HASH,
            before["epoch"],
            before["head"],
            10,
        )
        assert len(page["events"]) == 1
        assert page["events"][0]["event_type"] == "channel"
        assert page["events"][0]["payload"] == {
            "index": 0,
            "name": "Public",
            "change": "update",
        }
        assert handler.companion_sync_state(_HASH)["cursor"] != before["cursor"]
        assert handler.companion_load_channels(_HASH)[0]["name"] == "Public"

    @pytest.mark.asyncio
    async def test_storage_failure_rolls_back_in_memory_channel(self):
        bridge = self._Bridge()
        journal = SimpleNamespace(
            store_channel=MagicMock(side_effect=RuntimeError("disk unavailable"))
        )

        with pytest.raises(RuntimeError, match="disk unavailable"):
            await RepeaterDaemon._ensure_default_companion_channel(bridge, journal)

        assert bridge.get_channel(0) is None


class TestCompanionInitSurfacesLoadFailure:
    @staticmethod
    def _daemon_with_companion(sqlite):
        config = {
            "repeater": {"node_name": "n"},
            "logging": {},
            "identities": {
                "companions": [
                    {"name": _NAME, "identity_key": "11" * 32, "settings": {"tcp_port": 5001}}
                ]
            },
        }
        daemon = RepeaterDaemon(config, radio=object())
        daemon.identity_manager = IdentityManager({})
        daemon.router = SimpleNamespace(inject_packet=AsyncMock())
        daemon.repeater_handler = SimpleNamespace(
            storage=SimpleNamespace(sqlite_handler=sqlite), radio_config={}
        )
        return daemon

    @staticmethod
    def _failing_sqlite():
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 0
        sqlite.companion_load_contacts.return_value = []
        # Channels table has rows for this companion but every load fails.
        sqlite.companion_load_channels.return_value = None
        sqlite.companion_count_channels.return_value = 3
        return sqlite

    @pytest.mark.asyncio
    async def test_load_companion_identities_aborts_companion(self, caplog):
        sqlite = self._failing_sqlite()
        daemon = self._daemon_with_companion(sqlite)
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
            caplog.at_level(logging.ERROR),
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            await daemon._load_companion_identities()

        # Companion init failed loudly: nothing registered, no frame server,
        # and no Public-channel backfill over the unloaded store.
        assert daemon.companion_bridges == {}
        assert daemon.companion_frame_servers == []
        server_cls.assert_not_called()
        bridge_cls.return_value.set_channel.assert_not_called()
        assert sqlite.companion_load_channels.call_count == 2  # retried once
        assert any("Companion init aborted" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_add_companion_from_config_raises(self):
        sqlite = self._failing_sqlite()
        daemon = self._daemon_with_companion(sqlite)
        daemon.identity_manager = IdentityManager({})
        comp_config = {"name": "hot-comp", "identity_key": "22" * 32, "settings": {}}
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            with pytest.raises(CompanionStateLoadError):
                await daemon.add_companion_from_config(comp_config)
        server_cls.assert_not_called()
        assert daemon.companion_bridges == {}


class TestRfReceptionEventsSettingWiring:
    """Design doc §9 write gate: settings.rf_reception_events is read at both
    the boot path (_load_companion_identities) and hot-reload path
    (add_companion_from_config), populating daemon._rf_reception_journals
    only for opted-in companions."""

    @staticmethod
    def _empty_sqlite():
        sqlite = MagicMock()
        sqlite.companion_count_contacts.return_value = 0
        sqlite.companion_load_contacts.return_value = []
        sqlite.companion_count_channels.return_value = 0
        sqlite.companion_load_channels.return_value = []
        sqlite.companion_count_messages.return_value = 0
        sqlite.companion_load_messages.return_value = []
        return sqlite

    @staticmethod
    def _daemon_with_companion(sqlite, settings):
        config = {
            "repeater": {"node_name": "n"},
            "logging": {},
            "identities": {
                "companions": [{"name": _NAME, "identity_key": "33" * 32, "settings": settings}]
            },
        }
        daemon = RepeaterDaemon(config, radio=object())
        daemon.identity_manager = IdentityManager({})
        daemon.router = SimpleNamespace(inject_packet=AsyncMock())
        daemon.repeater_handler = SimpleNamespace(
            storage=SimpleNamespace(sqlite_handler=sqlite), radio_config={}
        )
        return daemon

    @pytest.mark.asyncio
    async def test_boot_path_default_off_not_registered(self):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={})
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            bridge_cls.return_value.start = AsyncMock()
            server_cls.return_value.start = AsyncMock()
            await daemon._load_companion_identities()

        companion_hash_str = companion_hash_str_from_identity_key("33" * 32)
        assert companion_hash_str in daemon.companion_journals
        assert companion_hash_str not in daemon._rf_reception_journals
        assert server_cls.call_args.kwargs["port"] == DEFAULT_COMPANION_TCP_PORT
        assert (
            server_cls.call_args.kwargs["client_idle_timeout_sec"]
            == DEFAULT_COMPANION_TCP_TIMEOUT_SEC
        )

    @pytest.mark.asyncio
    async def test_boot_path_explicit_true_registers(self):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={"rf_reception_events": True})
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            bridge_cls.return_value.start = AsyncMock()
            server_cls.return_value.start = AsyncMock()
            await daemon._load_companion_identities()

        companion_hash_str = companion_hash_str_from_identity_key("33" * 32)
        assert companion_hash_str in daemon.companion_journals
        assert companion_hash_str in daemon._rf_reception_journals
        assert (
            daemon._rf_reception_journals[companion_hash_str]
            is daemon.companion_journals[companion_hash_str]
        )

    @pytest.mark.asyncio
    async def test_hot_reload_path_explicit_true_registers(self):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={})  # no boot-time companions used
        daemon.identity_manager = IdentityManager({})
        comp_config = {
            "name": "hot-rf",
            "identity_key": "44" * 32,
            "settings": {"rf_reception_events": True, "tcp_port": 5001},
        }
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            bridge_cls.return_value.start = AsyncMock()
            server_cls.return_value.start = AsyncMock()
            await daemon.add_companion_from_config(comp_config)

        companion_hash_str = companion_hash_str_from_identity_key("44" * 32)
        assert companion_hash_str in daemon._rf_reception_journals

    @pytest.mark.asyncio
    async def test_hot_reload_path_default_off_not_registered(self):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={"tcp_port": 5001})
        daemon.identity_manager = IdentityManager({})
        comp_config = {"name": "hot-off", "identity_key": "55" * 32, "settings": {}}
        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge_cls.return_value.message_queue.max_size = 100
            bridge_cls.return_value.start = AsyncMock()
            server_cls.return_value.start = AsyncMock()
            await daemon.add_companion_from_config(comp_config)

        companion_hash_str = companion_hash_str_from_identity_key("55" * 32)
        assert companion_hash_str in daemon.companion_journals
        assert companion_hash_str not in daemon._rf_reception_journals
        assert server_cls.call_args.kwargs["port"] == DEFAULT_COMPANION_TCP_PORT
        assert (
            server_cls.call_args.kwargs["client_idle_timeout_sec"]
            == DEFAULT_COMPANION_TCP_TIMEOUT_SEC
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("setting_name", "setting_value"),
        [
            ("trim_contacts_on_overflow", "false"),
            ("rf_reception_events", "true"),
        ],
    )
    async def test_boot_rejects_string_policy_before_stateful_setup(
        self,
        setting_name,
        setting_value,
        caplog,
    ):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(
            sqlite,
            settings={setting_name: setting_value},
        )
        daemon._build_push_notifier = MagicMock()

        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
            caplog.at_level(logging.ERROR),
        ):
            await daemon._load_companion_identities()

        sqlite.companion_bind_namespace.assert_not_called()
        daemon._build_push_notifier.assert_not_called()
        bridge_cls.assert_not_called()
        server_cls.assert_not_called()
        assert daemon.companion_journals == {}
        assert daemon._rf_reception_journals == {}
        assert any(
            setting_name in record.message and "must be a boolean" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("setting_name", "setting_value"),
        [
            ("trim_contacts_on_overflow", "false"),
            ("rf_reception_events", "true"),
        ],
    )
    async def test_hot_reload_rejects_string_policy_before_stateful_setup(
        self,
        setting_name,
        setting_value,
    ):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={})
        daemon.identity_manager = IdentityManager({})
        daemon._build_push_notifier = MagicMock()
        comp_config = {
            "name": "hot-invalid",
            "identity_key": "77" * 32,
            "settings": {
                "tcp_port": 5001,
                setting_name: setting_value,
            },
        }

        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
            pytest.raises(ValueError, match=setting_name),
        ):
            await daemon.add_companion_from_config(comp_config)

        sqlite.companion_bind_namespace.assert_not_called()
        daemon._build_push_notifier.assert_not_called()
        bridge_cls.assert_not_called()
        server_cls.assert_not_called()
        assert daemon.companion_journals == {}
        assert daemon._rf_reception_journals == {}

    @pytest.mark.asyncio
    async def test_hot_reload_frame_start_failure_stops_partial_runtime(self):
        sqlite = self._empty_sqlite()
        daemon = self._daemon_with_companion(sqlite, settings={})
        daemon.identity_manager = IdentityManager({})
        comp_config = {
            "name": "hot-fail",
            "identity_key": "66" * 32,
            "settings": {"tcp_port": 5001},
        }

        with (
            patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
            patch("repeater.companion.CompanionFrameServer") as server_cls,
        ):
            bridge = bridge_cls.return_value
            bridge.message_queue.max_size = 100
            bridge.start = AsyncMock()
            bridge.stop = AsyncMock()
            server = server_cls.return_value
            server.start = AsyncMock(side_effect=OSError("bind failed"))
            server.stop = AsyncMock()

            with pytest.raises(
                IdentityConfigurationError,
                match=(
                    r"Companion 'hot-fail' Frame listener failed to start "
                    r"on 127\.0\.0\.1:5001: bind failed"
                ),
            ):
                await daemon.add_companion_from_config(comp_config)

        bridge.start.assert_awaited_once()
        server.stop.assert_awaited_once()
        bridge.stop.assert_awaited_once()
        assert daemon.companion_bridges == {}
        assert daemon.companion_frame_servers == []
