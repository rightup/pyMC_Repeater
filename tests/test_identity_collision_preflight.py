from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repeater.identity_manager import IdentityManager
from repeater.main import IdentityConfigurationError, RepeaterDaemon


class _SeedFirstByteIdentity:
    """Deterministically expose a configured key's first byte as its hash."""

    def __init__(self, seed: bytes):
        self._public_key = bytes([seed[0]]) + b"P" * 31

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
                {"name": "first", "identity_key": "21" * 32},
                {"name": "second", "identity_key": "21" * 32},
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
