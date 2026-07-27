"""Every companion bridge must inject with its own hash as origin_hash.

The origin exclusion in PacketRouter (the 0x88 raw-RX echo and the companion
fan-out) is only as good as this binding: an injector built without
``origin_hash``, or with the wrong companion's hash, silently sends a
companion its own transmission back. Nothing else asserts the binding, so it
is pinned here for both the config-load path and the hot-add path.
"""

from unittest.mock import AsyncMock, patch

import pytest

from openhop_core import LocalIdentity
from repeater.companion.frame_server import CompanionFrameServer
from repeater.identity_manager import IdentityManager
from repeater.main import RepeaterDaemon
from repeater.packet_router import PacketRouter

# Distinct seeds whose derived public keys do not collide on the first byte,
# so the two companions occupy different companion_bridges slots.
LOADED_KEY = "11" * 32  # public key starts 0xd0
HOT_ADDED_KEY = "22" * 32  # public key starts 0xa0


def _config(companions=()):
    return {
        "repeater": {"node_name": "n", "identity_key": b"\x10" * 32},
        "logging": {},
        "radio": {},
        "identities": {"companions": list(companions), "room_servers": []},
    }


def _daemon(companions=()):
    daemon = RepeaterDaemon(_config(companions), radio=object())
    daemon.identity_manager = IdentityManager({})
    daemon.router = PacketRouter(daemon)
    return daemon


def _expected_hash(identity_key_hex: str) -> str:
    return f"0x{LocalIdentity(seed=bytes.fromhex(identity_key_hex)).get_public_key()[0]:02x}"


def _assert_injector_bound(daemon, expected_hash: str) -> None:
    """The bridge injects as expected_hash, and its frame server agrees."""
    bridge = daemon.companion_bridges[int(expected_hash, 16)]
    injector = bridge._packet_injector

    assert injector.func == daemon.router.inject_packet
    assert injector.keywords["origin_hash"] == expected_hash
    # The frame server compares its own companion_hash against the exclude_hash
    # inject_packet passes down, so the two must be the same string.
    frame_server = next(fs for fs in daemon.companion_frame_servers if fs.bridge is bridge)
    assert frame_server.companion_hash == expected_hash
    # Same string again as the bridge's SQLite namespace key.
    assert bridge._companion_hash == expected_hash


@pytest.mark.asyncio
async def test_companion_bridge_injector_is_bound_to_its_own_origin_hash():
    daemon = _daemon(companions=({"name": "loaded", "identity_key": LOADED_KEY},))

    with patch.object(CompanionFrameServer, "start", AsyncMock()):
        await daemon._load_companion_identities()

        assert len(daemon.companion_bridges) == 1
        _assert_injector_bound(daemon, _expected_hash(LOADED_KEY))

        # Hot-add path: a second companion added at runtime must be bound to its
        # own hash, not to the one already loaded.
        await daemon.add_companion_from_config(
            {"name": "hot", "identity_key": HOT_ADDED_KEY, "settings": {"tcp_port": 5001}}
        )

    assert len(daemon.companion_bridges) == 2
    _assert_injector_bound(daemon, _expected_hash(LOADED_KEY))
    _assert_injector_bound(daemon, _expected_hash(HOT_ADDED_KEY))
