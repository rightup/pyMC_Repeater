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
from repeater.companion.utils import CompanionStateLoadError
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
    async def test_restores_all_state(self):
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
        bridge.message_queue.push.assert_called_once()

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

    @pytest.mark.asyncio
    async def test_old_core_without_sender_prefix_drops_prefix(self, caplog):
        # openhop_core releases before the sender_prefix change reject the kwarg;
        # message restore must degrade (drop the prefix) instead of failing init.
        from dataclasses import dataclass, field

        @dataclass
        class LegacyQueuedMessage:
            sender_key: bytes = b""
            txt_type: int = 0
            timestamp: int = 0
            text: str = ""
            is_channel: bool = False
            channel_idx: int = 0
            path_len: int = 0
            snr: float = 0.0
            rssi: int = 0
            channel_data_payload: bytes = field(default=b"")

        daemon = self._daemon()
        bridge = self._bridge()
        sqlite = self._sqlite(
            messages=[{"sender_key": b"", "text": "hi", "sender_prefix": b"\xab\xcd"}]
        )
        with (
            patch("openhop_core.companion.models.QueuedMessage", LegacyQueuedMessage),
            caplog.at_level(logging.WARNING),
        ):
            await daemon._restore_companion_state(sqlite, bridge, _HASH, _NAME)
        bridge.message_queue.push.assert_called_once()
        pushed = bridge.message_queue.push.call_args[0][0]
        assert isinstance(pushed, LegacyQueuedMessage)
        assert pushed.text == "hi"
        assert any("sender prefixes will be dropped" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_zero_retention_skips_message_load(self):
        daemon = self._daemon()
        bridge = self._bridge(max_size=0)
        sqlite = self._sqlite()
        await daemon._restore_companion_state(sqlite, bridge, _HASH, _NAME)
        sqlite.companion_load_messages.assert_not_called()


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
        daemon.identity_manager = SimpleNamespace(named_identities={})
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
