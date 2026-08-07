"""Served-region map: build correctness + dispatcher/bridge wiring.

Core re-scopes a flood reply to the region its request arrived under
(``region_map.apply_reply_scope``), but only when a ``RegionMap`` is wired onto
the dispatcher and companion bridges. This repeater builds that map from the
``transport_keys`` table (``build_region_map``), assigns one shared instance to
the dispatcher and every bridge, and rebuilds it whenever a region is added,
removed, or re-flooded via the storage change hook.

These tests cover the repeater's contribution: the record -> RegionEntry mapping
and flood matching, the storage change hook, and the daemon wiring that keeps a
non-None map on the dispatcher and all live bridges after a runtime change.
"""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

from openhop_core.protocol.packet import Packet
from openhop_core.protocol.region_map import REGION_DENY_FLOOD
from openhop_core.protocol.transport_keys import calc_transport_code, get_auto_key_for

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.main import RepeaterDaemon
from repeater.region_map_builder import build_region_map


class _FakeHandler:
    def __init__(self, records):
        self._records = records

    def get_transport_keys(self):
        return self._records


def _b64key(name):
    return base64.b64encode(get_auto_key_for(name)).decode("ascii")


def _scoped_flood(key, payload=b"reply-body"):
    """A TRANSPORT_FLOOD packet whose transport code was hashed with ``key``."""
    pkt = Packet()
    pkt.payload = bytearray(payload)
    pkt.transport_codes[0] = calc_transport_code(key, pkt)
    return pkt


def _plain_flood(payload=b"reply-body"):
    pkt = Packet()
    pkt.payload = bytearray(payload)
    pkt.header = 0x01  # ROUTE_TYPE_FLOOD, no transport codes
    return pkt


# ---------------------------------------------------------------------------
# build_region_map: record -> RegionEntry mapping and flood matching
# ---------------------------------------------------------------------------
def test_allow_region_matches_scoped_flood():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 1,
                    "name": "#usa",
                    "flood_policy": "allow",
                    "transport_key": _b64key("#usa"),
                    "parent_id": None,
                }
            ]
        ),
    )
    match = rm.find_match(_scoped_flood(get_auto_key_for("#usa")), mask=REGION_DENY_FLOOD)
    assert match is not None
    assert match.name == "#usa"
    assert match.flags == 0


def test_deny_region_is_skipped_under_flood_mask():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 2,
                    "name": "#secret",
                    "flood_policy": "deny",
                    "transport_key": _b64key("#secret"),
                    "parent_id": None,
                }
            ]
        ),
    )
    # The entry exists and carries the deny flag ...
    assert [r.flags for r in rm.regions] == [REGION_DENY_FLOOD]
    # ... so a flood scoped to it still replies plain (find_match returns None).
    assert rm.find_match(_scoped_flood(get_auto_key_for("#secret")), mask=REGION_DENY_FLOOD) is None


def test_wildcard_and_empty_names_are_not_entries():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 3,
                    "name": "*",
                    "flood_policy": "allow",
                    "transport_key": None,
                    "parent_id": None,
                },
                {
                    "id": 4,
                    "name": "",
                    "flood_policy": "allow",
                    "transport_key": None,
                    "parent_id": None,
                },
                {
                    "id": 5,
                    "name": "  ",
                    "flood_policy": "allow",
                    "transport_key": None,
                    "parent_id": None,
                },
            ]
        ),
    )
    assert rm.regions == []


def test_plain_flood_never_matches():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 1,
                    "name": "#usa",
                    "flood_policy": "allow",
                    "transport_key": _b64key("#usa"),
                    "parent_id": None,
                }
            ]
        ),
    )
    assert rm.find_match(_plain_flood(), mask=REGION_DENY_FLOOD) is None


def test_private_region_uses_stored_key():
    custom = b"\x11" * 16
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 7,
                    "name": "$vip",
                    "flood_policy": "allow",
                    "transport_key": base64.b64encode(custom).decode("ascii"),
                    "parent_id": None,
                }
            ]
        ),
    )
    entry = rm.regions[0]
    assert entry.private_keys == [custom]
    assert rm.find_match(_scoped_flood(custom), mask=REGION_DENY_FLOOD) is not None


def test_private_region_without_key_matches_nothing():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 8,
                    "name": "$vip",
                    "flood_policy": "allow",
                    "transport_key": None,
                    "parent_id": None,
                }
            ]
        ),
    )
    # No usable key for a "$" region -> never matches (core never name-hashes it).
    assert rm.find_match(_scoped_flood(get_auto_key_for("$vip")), mask=REGION_DENY_FLOOD) is None


def test_public_region_with_custom_key_carries_private_key():
    custom = b"\x22" * 16
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 9,
                    "name": "#usa",
                    "flood_policy": "allow",
                    "transport_key": base64.b64encode(custom).decode("ascii"),
                    "parent_id": None,
                }
            ]
        ),
    )
    entry = rm.regions[0]
    assert entry.private_keys == [custom]  # differs from name hash -> carried through
    assert rm.find_match(_scoped_flood(custom), mask=REGION_DENY_FLOOD) is not None


def test_public_region_with_auto_key_relies_on_name_hash():
    rm = build_region_map(
        {},
        _FakeHandler(
            [
                {
                    "id": 1,
                    "name": "#usa",
                    "flood_policy": "allow",
                    "transport_key": _b64key("#usa"),
                    "parent_id": None,
                }
            ]
        ),
    )
    # Stored key equals the name hash -> not carried as an explicit private key.
    assert rm.regions[0].private_keys is None


def test_missing_storage_yields_empty_map():
    assert build_region_map({}, None).regions == []


# ---------------------------------------------------------------------------
# Storage change hook fires on transport_keys writes
# ---------------------------------------------------------------------------
def test_transport_keys_change_hook_fires_on_writes(tmp_path):
    handler = SQLiteHandler(tmp_path)
    cb = MagicMock()
    handler.set_transport_keys_changed_callback(cb)

    key_id = handler.create_transport_key("#usa", "allow")
    assert key_id is not None
    assert cb.call_count == 1

    assert handler.update_transport_key(key_id, flood_policy="deny")
    assert cb.call_count == 2

    # A no-op update (unknown id) must not fire the hook.
    assert not handler.update_transport_key(999999, flood_policy="allow")
    assert cb.call_count == 2

    assert handler.delete_transport_key(key_id)
    assert cb.call_count == 3

    handler.sync_transport_keys(
        [
            {"node_id": "n1", "name": "#eu", "flood_policy": "allow"},
        ]
    )
    assert cb.call_count == 4


# ---------------------------------------------------------------------------
# Daemon wiring: dispatcher + bridges get a shared, non-None map that a runtime
# region change refreshes for every holder.
# ---------------------------------------------------------------------------
def test_daemon_wires_and_refreshes_region_map(tmp_path):
    handler = SQLiteHandler(tmp_path)
    handler.create_transport_key("#usa", "allow")

    daemon = RepeaterDaemon({"logging": {}, "mesh": {}})
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler))
    daemon.dispatcher = SimpleNamespace(region_map=None)
    daemon.companion_bridges = {1: SimpleNamespace(region_map=None)}

    daemon._init_region_map()

    # Dispatcher has a non-None map with the served region.
    assert daemon.dispatcher.region_map is not None
    assert [r.name for r in daemon.dispatcher.region_map.regions] == ["#usa"]

    # A runtime add fires the storage hook -> refresh reaches dispatcher + bridges.
    handler.create_transport_key("#eu", "allow")
    assert sorted(r.name for r in daemon.dispatcher.region_map.regions) == ["#eu", "#usa"]
    assert daemon.companion_bridges[1].region_map is daemon.dispatcher.region_map

    # A runtime delete also reaches every holder.
    eu = next(r for r in handler.get_transport_keys() if r["name"] == "#eu")
    handler.delete_transport_key(eu["id"])
    assert [r.name for r in daemon.dispatcher.region_map.regions] == ["#usa"]
    assert daemon.companion_bridges[1].region_map is daemon.dispatcher.region_map
