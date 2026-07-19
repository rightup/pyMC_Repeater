"""Tests for RepeaterCompanionBridge prefs JSON round-trip (bytes fields)."""

import pytest

from openhop_core import LocalIdentity

from repeater.companion.bridge import RepeaterCompanionBridge, _prefs_bytes_from_json


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
