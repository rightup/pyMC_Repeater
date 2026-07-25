"""Tests for RepeaterCompanionBridge prefs persistence and reconciliation."""

import dataclasses
import logging
from unittest.mock import patch

import pytest

from openhop_core import LocalIdentity
from openhop_core.companion.models import NodePrefs

from repeater.companion.bridge import RepeaterCompanionBridge, _prefs_bytes_from_json
from repeater.companion.journal import CompanionEventJournal
from repeater.config_manager import ConfigManager
from repeater.data_acquisition.sqlite_handler import CompanionStorageError, SQLiteHandler
from repeater.main import RepeaterDaemon


@pytest.fixture
def identity():
    return LocalIdentity()


def test_prefs_bytes_from_json_round_trip():
    assert _prefs_bytes_from_json("") == b""
    assert _prefs_bytes_from_json("00") == b"\x00"
    key = bytes(range(16))
    assert _prefs_bytes_from_json(key.hex()) == key
    assert _prefs_bytes_from_json(bytearray(key)) == key
    assert _prefs_bytes_from_json(key) == key
    assert _prefs_bytes_from_json("not-hex") == b""


def test_invalid_prefs_bytes_do_not_echo_secret_material_to_logs(caplog):
    malformed_scope_key = "scope-key-material-that-must-stay-private"

    with caplog.at_level(logging.DEBUG, logger="RepeaterCompanionBridge"):
        assert _prefs_bytes_from_json(malformed_scope_key) == b""

    assert malformed_scope_key not in caplog.text


def test_load_prefs_restores_default_scope_key_as_bytes(identity):
    """Hex strings from SQLite JSON must become bytes (not str) on NodePrefs."""

    class FakeSqlite:
        def companion_load_prefs(self, companion_hash: str):
            return {
                "default_scope_name": "region1",
                "default_scope_key": bytes(range(16)).hex(),
            }

        def companion_save_prefs(self, companion_hash: str, prefs: dict) -> bool:
            return True

    async def inject(pkt, wait_for_ack=False):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        sqlite_handler=FakeSqlite(),
        companion_hash="testhash",
        node_name="bootname",
    )
    assert bridge.prefs.default_scope_name == "region1"
    assert isinstance(bridge.prefs.default_scope_key, bytes)
    assert bridge.prefs.default_scope_key == bytes(range(16))
    scope = bridge.get_default_flood_scope()
    assert scope is not None
    assert scope[0] == "region1"
    assert scope[1] == bytes(range(16))


def test_every_current_node_pref_has_a_strict_persistence_rule(identity):
    stored = dataclasses.asdict(NodePrefs())
    stored["default_scope_key"] = stored["default_scope_key"].hex()

    class FakeSqlite:
        def companion_load_prefs(self, _companion_hash: str):
            return stored

    async def inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        sqlite_handler=FakeSqlite(),
        companion_hash="testhash",
    )
    assert dataclasses.asdict(bridge.prefs) == dataclasses.asdict(NodePrefs())


@pytest.mark.parametrize(
    "stored",
    [
        {"node_name": 7},
        {"node_name": "n" * 32},
        {"adv_type": True},
        {"adv_type": 5},
        {"tx_power_dbm": -10},
        {"frequency_hz": 99_999_999},
        {"bandwidth_hz": 6_999},
        {"spreading_factor": 13},
        {"coding_rate": 4},
        {"latitude": "1.0"},
        {"latitude": 90.000001},
        {"longitude": -180.000001},
        {"advert_loc_policy": 256},
        {"multi_acks": "1"},
        {"telemetry_mode_base": 4},
        {"manual_add_contacts": -1},
        {"autoadd_config": 256},
        {"autoadd_max_hops": 65},
        {"rx_delay_base": -0.001},
        {"airtime_factor": float("inf")},
        {"client_repeat": 256},
        {"path_hash_mode": False},
        {"path_hash_mode": 3},
        {"default_scope_name": "region", "default_scope_key": "00" * 15},
        {"default_scope_name": "", "default_scope_key": "00" * 16},
    ],
)
def test_malformed_known_prefs_refuse_companion_activation(identity, stored):
    class FakeSqlite:
        def companion_load_prefs(self, _companion_hash: str):
            return stored

    async def inject(_packet, **_kwargs):
        return True

    with pytest.raises(ValueError, match="persisted"):
        RepeaterCompanionBridge(
            identity,
            inject,
            sqlite_handler=FakeSqlite(),
            companion_hash="testhash",
        )


def test_malformed_scope_key_is_not_echoed_during_failed_activation(
    identity,
    caplog,
):
    secret = "scope-key-material-that-must-stay-private"

    class FakeSqlite:
        def companion_load_prefs(self, _companion_hash: str):
            return {
                "default_scope_name": "region",
                "default_scope_key": secret,
            }

    async def inject(_packet, **_kwargs):
        return True

    with caplog.at_level(logging.DEBUG, logger="RepeaterCompanionBridge"):
        with pytest.raises(ValueError):
            RepeaterCompanionBridge(
                identity,
                inject,
                sqlite_handler=FakeSqlite(),
                companion_hash="testhash",
            )
    assert secret not in caplog.text


def test_unknown_future_prefs_survive_a_known_pref_save(identity, tmp_path):
    handler = SQLiteHandler(tmp_path)
    companion_hash = f"0x{identity.get_public_key()[0]:02x}"
    future_value = {"mode": "future", "options": [1, 2, 3]}
    assert handler.companion_save_prefs(
        companion_hash,
        {
            "node_name": "Old",
            "future_pref": future_value,
        },
    )

    async def inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        sqlite_handler=handler,
        companion_hash=companion_hash,
    )
    bridge.set_advert_name("New")

    persisted = handler.companion_load_prefs(companion_hash)
    assert persisted["node_name"] == "New"
    assert persisted["future_pref"] == future_value


@pytest.mark.parametrize(
    "raw_prefs",
    [
        "{not-json",
        "[]",
        '{"latitude":NaN}',
        '{"latitude":1e400}',
        '{"node_name":"first","node_name":"second"}',
    ],
)
def test_invalid_persisted_prefs_refuse_companion_activation(
    identity,
    tmp_path,
    raw_prefs,
):
    handler = SQLiteHandler(tmp_path)
    companion_hash = f"0x{identity.get_public_key()[0]:02x}"
    with handler._connect() as conn:
        conn.execute(
            """
            INSERT INTO companion_prefs (companion_hash, prefs_json)
            VALUES (?, ?)
            """,
            (companion_hash, raw_prefs),
        )
        conn.commit()

    with pytest.raises(CompanionStorageError):
        handler.companion_load_prefs(companion_hash)

    async def inject(_packet, **_kwargs):
        return True

    with pytest.raises(CompanionStorageError):
        RepeaterCompanionBridge(
            identity,
            inject,
            sqlite_handler=handler,
            companion_hash=companion_hash,
        )


def test_bridge_accepts_host_radio_callbacks(identity):
    """Repeater must forward host-radio callbacks required by CompanionBridge."""

    async def inject(pkt, wait_for_ack=False):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        radio_settings_getter=lambda: {
            "frequency": 915_000_000,
            "bandwidth": 250_000,
            "spreading_factor": 10,
            "coding_rate": 5,
            "tx_power": 19,
        },
        max_tx_power_getter=lambda: 20,
    )

    radio = bridge.get_radio_params()
    assert radio == {
        "frequency_hz": 915_000_000,
        "bandwidth_hz": 250_000,
        "spreading_factor": 10,
        "coding_rate": 5,
        "tx_power_dbm": 19,
        "rx_delay_base": 0,
        "airtime_factor": 1.0,
    }
    assert bridge.get_max_tx_power_dbm() == 20


@pytest.mark.asyncio
async def test_explicit_config_name_reconciles_persisted_prefs_and_journal(
    identity,
    tmp_path,
):
    handler = SQLiteHandler(tmp_path)
    companion_hash = f"0x{identity.get_public_key()[0]:02x}"
    assert handler.companion_save_prefs(
        companion_hash,
        {"node_name": "Old persisted name"},
    )
    journal = CompanionEventJournal(handler, companion_hash)
    config = {
        "repeater": {"node_name": "repeater"},
        "logging": {},
        "identities": {
            "companions": [
                {
                    "name": "field-radio",
                    "settings": {"node_name": "Configured name"},
                }
            ]
        },
    }
    daemon = RepeaterDaemon(config, radio=object())

    async def inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        node_name="Configured name",
        sqlite_handler=handler,
        companion_hash=companion_hash,
        journal=journal,
        on_prefs_saved=lambda name: daemon._sync_companion_node_name_to_config(
            "field-radio",
            name,
        ),
    )
    assert bridge.prefs.node_name == "Old persisted name"
    before = handler.companion_sync_state(companion_hash)

    await daemon._reconcile_companion_node_name(
        bridge,
        "Configured name",
        "field-radio",
    )

    assert bridge.prefs.node_name == "Configured name"
    assert handler.companion_load_prefs(companion_hash)["node_name"] == "Configured name"
    page = handler.companion_sync_page(
        companion_hash,
        before["epoch"],
        before["head"],
        10,
    )
    assert page["events"][-1]["event_type"] == "prefs"
    assert page["events"][-1]["payload"]["node_name"] == "Configured name"


def test_config_save_failure_compensates_prefs_and_reports_error(
    identity,
    tmp_path,
):
    handler = SQLiteHandler(tmp_path)
    companion_hash = f"0x{identity.get_public_key()[0]:02x}"
    assert handler.companion_save_prefs(companion_hash, {"node_name": "Old"})
    journal = CompanionEventJournal(handler, companion_hash)
    config = {
        "repeater": {"node_name": "repeater"},
        "logging": {},
        "identities": {
            "companions": [
                {"name": "field-radio", "settings": {"node_name": "Old"}}
            ]
        },
    }
    daemon = RepeaterDaemon(config, radio=object())
    daemon.config_path = str(tmp_path / "config.yaml")
    daemon.config_manager = ConfigManager(
        daemon.config_path,
        config,
        daemon_instance=daemon,
    )

    async def inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        identity,
        inject,
        sqlite_handler=handler,
        companion_hash=companion_hash,
        journal=journal,
        on_prefs_saved=lambda name: daemon._sync_companion_node_name_to_config(
            "field-radio",
            name,
        ),
    )
    before = handler.companion_sync_state(companion_hash)

    with patch.object(daemon.config_manager, "save_to_file", return_value=False):
        bridge.set_advert_name("New")

    assert bridge.consume_prefs_save_error() is not None
    assert bridge.prefs.node_name == "Old"
    assert config["identities"]["companions"][0]["settings"]["node_name"] == "Old"
    assert handler.companion_load_prefs(companion_hash)["node_name"] == "Old"
    page = handler.companion_sync_page(
        companion_hash,
        before["epoch"],
        before["head"],
        10,
    )
    assert [event["payload"]["node_name"] for event in page["events"]] == [
        "New",
        "Old",
    ]
