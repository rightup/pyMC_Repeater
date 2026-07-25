import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.identity_manager import IdentityManager
from repeater.config_manager import ConfigManager
from repeater.main import IdentityConfigurationError, RepeaterDaemon
from repeater.data_acquisition.sqlite_handler import (
    CompanionNamespaceCollisionError,
    SQLiteHandler,
)


class _SeedFirstByteIdentity:
    """Deterministically expose a configured key's first byte as its hash."""

    def __init__(self, seed: bytes):
        self._public_key = bytes([seed[0]]) + b"P" * 31

    def get_public_key(self):
        return self._public_key

    def get_address_bytes(self):
        return self._public_key[:3]


class _PublicSeedIdentity:
    """Use the 32-byte test seed as a deterministic full public key."""

    def __init__(self, seed: bytes):
        self._public_key = bytes(seed[:32])

    def get_public_key(self):
        return self._public_key

    def get_address_bytes(self):
        return self._public_key[:3]


def _config(*, companions=()):
    return {
        "repeater": {"node_name": "n", "identity_key": b"\x10" * 32},
        "logging": {},
        "identities": {"companions": list(companions)},
    }


def test_startup_preflight_rejects_default_repeater_hash_collision():
    daemon = RepeaterDaemon(
        _config(companions=({"name": "comp", "identity_key": "10" * 32},)),
        radio=object(),
    )
    local_identity = _SeedFirstByteIdentity(b"\x10" * 32)

    with patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity):
        with pytest.raises(IdentityConfigurationError, match="one-byte public-key prefixes"):
            daemon._preflight_configured_local_identities(local_identity)


@pytest.mark.asyncio
async def test_companion_set_collision_is_rejected_before_bridge_or_server_creation():
    daemon = RepeaterDaemon(
        _config(
            companions=(
                {
                    "name": "first",
                    "identity_key": "21" * 32,
                    "settings": {"tcp_port": 5000},
                },
                {
                    "name": "second",
                    "identity_key": "21" * 32,
                    "settings": {"tcp_port": 5001},
                },
            )
        ),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=object()))

    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(IdentityConfigurationError, match="second"),
    ):
        await daemon._load_companion_identities()

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()
    assert daemon.companion_bridges == {}
    assert daemon.companion_frame_servers == []


def test_startup_preflight_rejects_duplicate_companion_listener_ports():
    daemon = RepeaterDaemon(
        _config(
            companions=(
                {"name": "first", "identity_key": "21" * 32},
                {"name": "second", "identity_key": "22" * 32},
            )
        ),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})

    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        pytest.raises(
            IdentityConfigurationError,
            match=r"tcp_port 5000.*second.*companion 'first'",
        ),
    ):
        daemon._preflight_configured_local_identities(
            _SeedFirstByteIdentity(b"\x10" * 32)
        )


def test_startup_preflight_rejects_http_listener_port_collision():
    config = _config(
        companions=(
            {
                "name": "first",
                "identity_key": "21" * 32,
                "settings": {"tcp_port": 8000},
            },
        )
    )
    config["http"] = {"enabled": True, "port": 8000}
    daemon = RepeaterDaemon(config, radio=object())
    daemon.identity_manager = IdentityManager({})

    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        pytest.raises(IdentityConfigurationError, match="Repeater HTTP API"),
    ):
        daemon._preflight_configured_local_identities(
            _SeedFirstByteIdentity(b"\x10" * 32)
        )


@pytest.mark.asyncio
async def test_invalid_config_entry_logs_once_across_preflight_and_load(caplog):
    """Preflight parses the config once and the loaders reuse the cached
    specs, so an invalid entry produces exactly one error per startup."""
    daemon = RepeaterDaemon(
        _config(companions=({"name": "bad", "identity_key": "not-hex"},)),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})
    local_identity = _SeedFirstByteIdentity(b"\x10" * 32)

    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        caplog.at_level(logging.ERROR, logger="RepeaterDaemon"),
    ):
        daemon._preflight_configured_local_identities(local_identity)
        await daemon._load_companion_identities()

    invalid_key_logs = [r for r in caplog.records if "invalid hex" in r.getMessage()]
    assert len(invalid_key_logs) == 1


@pytest.mark.asyncio
async def test_invalid_companion_registration_name_is_never_activated(caplog):
    daemon = RepeaterDaemon(
        _config(companions=({"name": "bad/name", "identity_key": "21" * 32},)),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})

    with (
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        caplog.at_level(logging.ERROR, logger="RepeaterDaemon"),
    ):
        await daemon._load_companion_identities()

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()
    assert "contain only letters" in caplog.text


@pytest.mark.asyncio
async def test_hot_added_companion_collision_is_rejected_before_stateful_setup():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    daemon.identity_manager.register_identity(
        "repeater", _SeedFirstByteIdentity(b"\x33" * 32), {}, "repeater"
    )

    comp_config = {"name": "comp", "identity_key": "33" * 32, "settings": {}}
    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(ValueError, match="Cannot add companion"),
    ):
        await daemon.add_companion_from_config(comp_config)

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()
    assert daemon.companion_bridges == {}


@pytest.mark.asyncio
async def test_deleted_companion_hash_cannot_be_rebound_or_leak_state_and_push(tmp_path):
    handler = SQLiteHandler(tmp_path)
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    daemon.router = SimpleNamespace(inject_packet=AsyncMock())
    daemon.repeater_handler = SimpleNamespace(
        storage=SimpleNamespace(sqlite_handler=handler),
        radio_config={},
    )
    daemon.push_notifier = MagicMock()
    daemon.push_notifier.make_listener.return_value = MagicMock()

    identity_a = "31" + ("11" * 31)
    identity_b = "31" + ("22" * 31)
    config_a = {
        "name": "first",
        "identity_key": identity_a,
        "settings": {"tcp_port": 5001},
    }
    config_b = {
        "name": "replacement",
        "identity_key": identity_b,
        "settings": {"tcp_port": 5002},
    }

    with (
        patch("openhop_core.LocalIdentity", _PublicSeedIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge_cls.return_value.start = AsyncMock()
        bridge_cls.return_value.stop = AsyncMock()
        server_cls.return_value.start = AsyncMock()
        server_cls.return_value.stop = AsyncMock()
        server_cls.return_value.companion_hash = "0x31"

        await daemon.add_companion_from_config(config_a)
        assert handler.companion_namespace_binding("0x31") == identity_a

        assert handler.companion_save_contacts(
            "0x31",
            [{"pubkey": b"\x77" * 32, "name": "owned by first"}],
        )
        token_id = handler.create_api_token(
            "first-device",
            "first-device-token",
            scope="companion:first",
        )
        handler.companion_device_create(
            "0x31",
            "first-phone",
            "Phone",
            token_id,
            companion_identity=identity_a,
        )
        handler.companion_device_set_push("first-phone", "push-secret")

        assert await daemon.remove_companion("first") is True
        daemon.push_notifier.deactivate.assert_called_once_with(
            "0x31",
            identity_a,
        )
        calls_before_replacement = bridge_cls.call_count
        listeners_before_replacement = (
            daemon.push_notifier.make_listener.call_count
        )

        with pytest.raises(
            CompanionNamespaceCollisionError,
            match=r"already bound.*refusing activation",
        ):
            await daemon.add_companion_from_config(config_b)

    # Refusal happened before a replacement journal, listener, state restore,
    # bridge, or server existed. The original namespace remains untouched.
    assert bridge_cls.call_count == calls_before_replacement
    assert (
        daemon.push_notifier.make_listener.call_count
        == listeners_before_replacement
    )
    assert daemon.companion_bridges == {}
    assert daemon.companion_journals == {}
    assert handler.companion_namespace_binding("0x31") == identity_a
    assert handler.companion_load_contacts_strict("0x31")[0]["name"] == "owned by first"
    assert handler.companion_devices_with_push("0x31", identity_b) == []
    assert [
        item["device_id"]
        for item in handler.companion_devices_with_push("0x31", identity_a)
    ] == ["first-phone"]


@pytest.mark.asyncio
async def test_boot_refuses_durable_namespace_collision_before_journal_or_restore(
    tmp_path,
    caplog,
):
    handler = SQLiteHandler(tmp_path)
    identity_a = "32" + ("11" * 31)
    identity_b = "32" + ("22" * 31)
    handler.companion_bind_namespace("0x32", identity_a)
    handler.companion_save_contacts(
        "0x32",
        [{"pubkey": b"\x88" * 32, "name": "private to first"}],
    )
    daemon = RepeaterDaemon(
        _config(
            companions=(
                {
                    "name": "replacement",
                    "identity_key": identity_b,
                    "settings": {"tcp_port": 5001},
                },
            )
        ),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})
    daemon.router = SimpleNamespace(inject_packet=AsyncMock())
    daemon.repeater_handler = SimpleNamespace(
        storage=SimpleNamespace(sqlite_handler=handler),
        radio_config={},
    )
    daemon.push_notifier = MagicMock()

    with (
        patch("openhop_core.LocalIdentity", _PublicSeedIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        caplog.at_level(logging.ERROR, logger="RepeaterDaemon"),
    ):
        await daemon._load_companion_identities()

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()
    daemon.push_notifier.make_listener.assert_not_called()
    assert daemon.companion_journals == {}
    assert handler.companion_load_contacts_strict("0x32")[0]["name"] == "private to first"
    assert "Companion activation refused" in caplog.text
    assert "already bound" in caplog.text


@pytest.mark.asyncio
async def test_hot_add_rejects_ambiguous_companion_registration_name():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})

    comp_config = {"name": "bad:name", "identity_key": "44" * 32, "settings": {}}
    with (
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(ValueError, match="companion name"),
    ):
        await daemon.add_companion_from_config(comp_config)

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings, error",
    [
        ({"tcp_port": 0}, "tcp_port"),
        ({"tcp_port": "5000"}, "tcp_port"),
        ({"tcp_timeout": -1}, "tcp_timeout"),
        ({"tcp_timeout": "120"}, "tcp_timeout"),
        ({"adopt_legacy_namespace": "true"}, "adopt_legacy_namespace"),
        ([], "settings"),
    ],
)
async def test_hot_add_rejects_invalid_tcp_settings_before_runtime(settings, error):
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    comp_config = {
        "name": "valid-name",
        "identity_key": "44" * 32,
        "settings": settings,
    }
    with (
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(ValueError, match=error),
    ):
        await daemon.add_companion_from_config(comp_config)

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()


@pytest.mark.asyncio
async def test_hot_add_rejects_listener_collision_before_stateful_setup():
    config = _config(
        companions=(
            {
                "name": "first",
                "identity_key": "21" * 32,
                "settings": {"tcp_port": 5000},
            },
        )
    )
    daemon = RepeaterDaemon(config, radio=object())
    daemon.identity_manager = IdentityManager({})
    daemon.identity_manager.register_identity(
        "first",
        _SeedFirstByteIdentity(b"\x21" * 32),
        {},
        "companion",
    )
    daemon.companion_frame_servers = [
        SimpleNamespace(companion_hash="0x21", port=5000)
    ]
    comp_config = {
        "name": "second",
        "identity_key": "22" * 32,
        "settings": {"tcp_port": 5000},
    }

    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(ValueError, match=r"tcp_port 5000.*second"),
    ):
        await daemon.add_companion_from_config(comp_config)

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()


@pytest.mark.asyncio
async def test_http_hot_add_aborts_if_its_committed_config_was_removed_before_publish():
    comp_config = {
        "name": "phone",
        "identity_key": "31" * 32,
        "settings": {"frame_enabled": False},
    }
    daemon = RepeaterDaemon(
        _config(companions=(comp_config,)),
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})
    daemon.router = SimpleNamespace(inject_packet=AsyncMock())
    daemon.config_manager = ConfigManager(
        "/unused/config.yaml",
        daemon.config,
        daemon,
    )

    async def remove_config_before_publish():
        daemon.config["identities"]["companions"].clear()

    with (
        patch("openhop_core.LocalIdentity", _PublicSeedIdentity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.start = AsyncMock(side_effect=remove_config_before_publish)
        bridge.stop = AsyncMock()

        with pytest.raises(
            IdentityConfigurationError,
            match="configuration changed before activation completed",
        ):
            await daemon.add_companion_from_config(
                comp_config,
                require_current_config=True,
            )

    server_cls.assert_not_called()
    bridge.stop.assert_awaited_once()
    assert daemon.companion_bridges == {}
    assert daemon.companion_frame_servers == []
    assert daemon.identity_manager.get_identity_by_name("phone") is None


@pytest.mark.asyncio
async def test_remove_companion_stops_runtime_and_clears_every_index():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    identity = _SeedFirstByteIdentity(b"\x31" * 32)
    daemon.identity_manager.register_identity("phone", identity, {}, "companion")
    bridge = SimpleNamespace(stop=AsyncMock())
    server = SimpleNamespace(companion_hash="0x31", stop=AsyncMock())
    journal = object()
    daemon.companion_bridges = {0x31: bridge}
    daemon.companion_frame_servers = [server]
    daemon.companion_journals = {"0x31": journal}
    daemon._rf_reception_journals = {"0x31": journal}

    assert await daemon.remove_companion("phone") is True

    server.stop.assert_awaited_once()
    bridge.stop.assert_awaited_once()
    assert daemon.companion_frame_servers == []
    assert daemon.companion_bridges == {}
    assert daemon.companion_journals == {}
    assert daemon._rf_reception_journals == {}
    assert daemon.identity_manager.get_identity_by_name("phone") is None
    assert daemon.identity_manager.get_identity_by_hash(0x31) is None


@pytest.mark.asyncio
async def test_remove_companion_resolves_restart_required_rename_by_full_identity_key():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    identity_key = "31" * 32
    identity = _PublicSeedIdentity(bytes.fromhex(identity_key))
    daemon.identity_manager.register_identity(
        "old-name",
        identity,
        {},
        "companion",
    )
    bridge = SimpleNamespace(stop=AsyncMock())
    daemon.companion_bridges = {0x31: bridge}

    with patch("openhop_core.LocalIdentity", _PublicSeedIdentity):
        assert (
            await daemon.remove_companion(
                "new-name",
                identity_key=identity_key,
            )
            is True
        )

    bridge.stop.assert_awaited_once()
    assert daemon.companion_bridges == {}
    assert daemon.identity_manager.get_identity_by_name("old-name") is None


@pytest.mark.asyncio
async def test_remove_companion_never_detaches_same_name_with_different_full_key():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    live_identity = _PublicSeedIdentity(bytes.fromhex("31" * 32))
    daemon.identity_manager.register_identity(
        "phone",
        live_identity,
        {},
        "companion",
    )
    bridge = SimpleNamespace(stop=AsyncMock())
    daemon.companion_bridges = {0x31: bridge}

    with patch("openhop_core.LocalIdentity", _PublicSeedIdentity):
        assert (
            await daemon.remove_companion(
                "phone",
                identity_key="32" * 32,
            )
            is False
        )

    bridge.stop.assert_not_awaited()
    assert daemon.companion_bridges == {0x31: bridge}
    assert daemon.identity_manager.get_identity_by_name("phone") is not None


@pytest.mark.asyncio
async def test_remove_companion_never_drains_same_name_retirement_with_different_key():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    retired_bridge = SimpleNamespace(stop=AsyncMock())
    daemon._retiring_companions["phone"] = {
        "companion_hash": 0x31,
        "companion_public_key": bytes.fromhex("31" * 32),
        "frame_server": None,
        "bridge": retired_bridge,
    }

    with patch("openhop_core.LocalIdentity", _PublicSeedIdentity):
        assert (
            await daemon.remove_companion(
                "phone",
                identity_key="32" * 32,
            )
            is False
        )

    retired_bridge.stop.assert_not_awaited()
    assert "phone" in daemon._retiring_companions


@pytest.mark.asyncio
async def test_remove_companion_never_drains_same_hash_retirement_with_different_key():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    retired_bridge = SimpleNamespace(stop=AsyncMock())
    daemon._retiring_companions["old-name"] = {
        "companion_hash": 0x31,
        "companion_public_key": b"\x31" + (b"\x11" * 31),
        "frame_server": None,
        "bridge": retired_bridge,
    }
    requested_key = (b"\x31" + (b"\x22" * 31)).hex()

    with patch("openhop_core.LocalIdentity", _PublicSeedIdentity):
        assert (
            await daemon.remove_companion(
                "new-name",
                identity_key=requested_key,
            )
            is False
        )

    retired_bridge.stop.assert_not_awaited()
    assert "old-name" in daemon._retiring_companions


@pytest.mark.asyncio
async def test_remove_companion_stops_components_independently_and_retries_only_failure():
    daemon = RepeaterDaemon(_config(), radio=object())
    daemon.identity_manager = IdentityManager({})
    identity = _SeedFirstByteIdentity(b"\x32" * 32)
    daemon.identity_manager.register_identity("phone", identity, {}, "companion")
    frame_stop = AsyncMock(side_effect=[RuntimeError("frame stuck"), None])
    bridge_stop = AsyncMock()
    bridge = SimpleNamespace(stop=bridge_stop)
    server = SimpleNamespace(companion_hash="0x32", stop=frame_stop)
    daemon.companion_bridges = {0x32: bridge}
    daemon.companion_frame_servers = [server]

    assert await daemon.remove_companion("phone") is False

    frame_stop.assert_awaited_once()
    bridge_stop.assert_awaited_once()
    retiring = daemon._retiring_companions["phone"]
    assert retiring["frame_server"] is server
    assert retiring["bridge"] is None

    # Neither the retired name nor its one-byte routing hash can be reused
    # while the failed component remains retryable.
    with (
        patch("openhop_core.LocalIdentity", _SeedFirstByteIdentity),
        pytest.raises(RuntimeError, match="still retiring"),
    ):
        await daemon.add_companion_from_config(
            {
                "name": "phone",
                "identity_key": "32" * 32,
                "settings": {"tcp_port": 5000},
            }
        )

    assert await daemon.remove_companion("phone") is True
    assert frame_stop.await_count == 2
    bridge_stop.assert_awaited_once()
    assert "phone" not in daemon._retiring_companions
