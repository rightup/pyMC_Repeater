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

import pytest

from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol.constants import (
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.region_map import (
    REGION_DENY_FLOOD,
    RegionEntry,
    RegionMap,
    apply_reply_scope,
    capture_recv_region,
)
from openhop_core.protocol.transport_keys import (
    calc_transport_code,
    get_auto_key_for,
    scope_packet,
)

from repeater.config_manager import ConfigManager
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.main import RepeaterDaemon
from repeater.region_map_builder import build_region_map, resolve_default_scope_key


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
    scope_packet(pkt, key)
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


# ---------------------------------------------------------------------------
# resolve_default_scope_key: mesh.default_region -> firmware's default_scope
# ---------------------------------------------------------------------------
def _served_map(*names_and_policies):
    """A RegionMap of ``(name, policy)`` pairs, as build_region_map would yield."""
    rm = RegionMap()
    for i, (name, policy) in enumerate(names_and_policies, start=1):
        flags = 0 if policy == "allow" else REGION_DENY_FLOOD
        rm.add_region(RegionEntry(id=i, parent=0, flags=flags, name=name))
    return rm


def test_unset_default_region_resolves_to_no_key():
    """Firmware's ``default_scope.isNull()`` row: chooseReplyScope -> NONE, plain."""
    rm = _served_map(("#usa", "allow"))
    assert resolve_default_scope_key({"mesh": {}}, rm) is None
    assert resolve_default_scope_key({}, rm) is None
    assert resolve_default_scope_key({"mesh": {"default_region": ""}}, rm) is None


def test_wildcard_default_region_resolves_to_no_key():
    """``*`` is not a region entry, so it carries no key: replies flood plain."""
    rm = _served_map(("#usa", "allow"))
    assert resolve_default_scope_key({"mesh": {"default_region": "*"}}, rm) is None


def test_served_region_resolves_to_its_key():
    rm = _served_map(("#usa", "allow"))
    assert resolve_default_scope_key({"mesh": {"default_region": "usa"}}, rm) == get_auto_key_for(
        "#usa"
    )


def test_leading_hash_and_case_are_tolerated():
    """The web API matches on the display name case-insensitively; match it here.

    The key still comes from the matched entry, so it derives from the name the
    transport_keys table actually holds -- not from the spelling in config.
    """
    rm = _served_map(("#usa", "allow"))
    expected = get_auto_key_for("#usa")
    for spelling in ("#usa", "USA", " #UsA "):
        assert resolve_default_scope_key({"mesh": {"default_region": spelling}}, rm) == expected


def test_unserved_default_region_resolves_to_no_key():
    """A default naming a region we do not serve must not invent a scope.

    Firmware cannot reach this state -- ``getDefaultRegion()`` only ever returns a
    region the map holds -- so the closest parity is to resolve nothing rather
    than to scope replies with a code no local Region would match on the way back.
    """
    rm = _served_map(("#usa", "allow"))
    assert resolve_default_scope_key({"mesh": {"default_region": "eu"}}, rm) is None


def test_private_default_region_uses_its_stored_key():
    """A ``$`` region is never name-hashed, so only stored material can resolve it."""
    custom = bytes(range(16))
    rm = RegionMap()
    rm.add_region(RegionEntry(id=1, parent=0, flags=0, name="$vip", private_keys=[custom]))
    assert resolve_default_scope_key({"mesh": {"default_region": "$vip"}}, rm) == custom


def test_private_default_region_without_a_key_resolves_to_nothing():
    rm = RegionMap()
    rm.add_region(RegionEntry(id=1, parent=0, flags=0, name="$vip", private_keys=None))
    assert resolve_default_scope_key({"mesh": {"default_region": "$vip"}}, rm) is None


def test_deny_flood_default_region_still_resolves():
    """``REGION_DENY_FLOOD`` gates inbound ``find_match``, not our own replies.

    Firmware's ``default_scope`` is read straight from the region via
    ``getTransportKeysFor``, with no flags test, and its web API forces the
    default region to allow-flood anyway -- so a deny-flood default is a
    misconfiguration, not a reason to strand replies un-scoped.
    """
    rm = _served_map(("#usa", "deny"))
    assert resolve_default_scope_key({"mesh": {"default_region": "usa"}}, rm) == get_auto_key_for(
        "#usa"
    )


# ---------------------------------------------------------------------------
# The behaviour this exists for: the REPLY_SCOPE_DEFAULT row goes out scoped
# ---------------------------------------------------------------------------
def _core_defers_the_default_row() -> bool:
    """Whether the installed core leaves rows 3/4 for the send layer.

    The wiring in this module is only *observable* on a core that defers
    ``REPLY_SCOPE_DEFAULT``. An older core resolved every captured case inside
    ``apply_reply_scope`` and marked the reply final, so both send-layer resolvers
    skipped it and ``default_flood_transport_key`` was never consulted -- the
    assignment was correct but inert.

    Probed by behaviour rather than by attribute, so this reports what the reply
    path actually does. The two tests below skip on an older core; the rest of the
    module does not depend on it.
    """
    rm = _served_map(("#usa", "allow"))
    req = Packet()
    req.payload = bytearray(b"req-body")
    req.header = ROUTE_TYPE_DIRECT
    capture_recv_region(rm, req)

    reply = Packet()
    reply.payload = bytearray(b"reply-body")
    reply.header = ROUTE_TYPE_FLOOD
    try:
        apply_reply_scope(reply, req)
    except Exception:
        return False
    return not getattr(reply, "_flood_scope_applied", False)


requires_deferred_default = pytest.mark.skipif(
    not _core_defers_the_default_row(),
    reason="installed openhop_core resolves REPLY_SCOPE_DEFAULT at RX and marks the "
    "reply final, so the dispatcher default is never consulted; needs the core "
    "reply-scope deferral",
)


def _direct_request():
    """A DIRECT request: firmware leaves recv_pkt_region NULL -> scope unknowable."""
    pkt = Packet()
    pkt.payload = bytearray(b"req-body")
    pkt.header = ROUTE_TYPE_DIRECT
    return pkt


def _resolve_default_row(default_key):
    """Drive a DEFAULT-row reply through core exactly as the repeater does.

    Capture on a DIRECT request resolves nothing, ``apply_reply_scope`` defers
    (firmware rows 3/4 turn on whether a default is configured, which only the
    send layer knows), and the dispatcher's resolver answers at TX.
    """
    rm = _served_map(("#usa", "allow"))
    req = _direct_request()
    capture_recv_region(rm, req)
    assert req._recv_region_key is None and req._recv_region_unscoped is False

    reply = Packet()
    reply.payload = bytearray(b"reply-body")
    reply.header = ROUTE_TYPE_FLOOD
    apply_reply_scope(reply, req)
    assert reply._flood_scope_applied is False, "rows 3/4 must defer to the send layer"

    dispatcher = Dispatcher(MagicMock())
    dispatcher.region_map = rm
    dispatcher.default_flood_transport_key = default_key
    dispatcher._apply_flood_scope(reply)
    return reply


@requires_deferred_default
def test_default_row_reply_is_scoped_once_the_default_is_wired():
    """[the fix] Firmware answers this row with sendFloodScoped(default_scope, ...)."""
    key = get_auto_key_for("#usa")
    reply = _resolve_default_row(key)

    assert reply.get_route_type() == ROUTE_TYPE_TRANSPORT_FLOOD
    assert reply.transport_codes[0] == calc_transport_code(key, reply)


@requires_deferred_default
def test_default_row_reply_floods_plain_without_the_wiring():
    """[the bug] Un-scoped here dies at hop 0 on repeaters with flood.max.unscoped=0.

    Pins what the wiring buys: the same reply, with no default resolved, stays a
    plain flood -- which is correct for an unset default (firmware's null row) and
    a deliverability failure when a default region *is* configured but never
    reaches the dispatcher.
    """
    reply = _resolve_default_row(None)

    assert reply.get_route_type() == ROUTE_TYPE_FLOOD
    assert reply.transport_codes == [0, 0]


# ---------------------------------------------------------------------------
# Daemon + config wiring: the three places the answer can change
# ---------------------------------------------------------------------------
def test_daemon_wires_default_scope_at_boot_and_on_region_changes(tmp_path):
    handler = SQLiteHandler(tmp_path)
    handler.create_transport_key("#usa", "allow")

    daemon = RepeaterDaemon({"logging": {}, "mesh": {"default_region": "usa"}})
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler))
    daemon.dispatcher = SimpleNamespace(region_map=None, default_flood_transport_key=None)
    daemon.companion_bridges = {}

    daemon._init_region_map()
    assert daemon.dispatcher.default_flood_transport_key == get_auto_key_for("#usa")

    # Deleting the default region leaves nothing to resolve: the storage hook
    # reruns the resolve, so the stale key must not linger.
    usa = next(r for r in handler.get_transport_keys() if r["name"] == "#usa")
    handler.delete_transport_key(usa["id"])
    assert daemon.dispatcher.default_flood_transport_key is None

    # Re-creating it resolves again.
    handler.create_transport_key("#usa", "allow")
    assert daemon.dispatcher.default_flood_transport_key == get_auto_key_for("#usa")


def test_daemon_default_scope_is_none_when_no_default_region_configured(tmp_path):
    handler = SQLiteHandler(tmp_path)
    handler.create_transport_key("#usa", "allow")

    daemon = RepeaterDaemon({"logging": {}, "mesh": {}})
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler))
    daemon.dispatcher = SimpleNamespace(region_map=None, default_flood_transport_key="stale")
    daemon.companion_bridges = {}

    daemon._init_region_map()
    assert daemon.dispatcher.default_flood_transport_key is None


def test_live_config_update_reresolves_the_default_scope(tmp_path):
    """A runtime ``mesh.default_region`` change must not need a restart.

    The web API creates the region *before* writing the config, so the
    transport_keys hook has already run against the previous value; the mesh
    branch of ``live_update_daemon`` is what closes that gap.
    """
    handler = SQLiteHandler(tmp_path)
    handler.create_transport_key("#usa", "allow")

    daemon = RepeaterDaemon({"logging": {}, "mesh": {}})
    daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler))
    daemon.dispatcher = SimpleNamespace(
        region_map=None,
        default_flood_transport_key=None,
        rx_delay_base=0.0,
        set_default_path_hash_mode=lambda mode: None,
    )
    daemon.companion_bridges = {}
    daemon._init_region_map()
    assert daemon.dispatcher.default_flood_transport_key is None

    # The web API's order: region already exists, then config, then live update.
    cm = ConfigManager("unused.yaml", {"mesh": {"default_region": "usa"}}, daemon)
    cm.live_update_daemon(["mesh"])

    assert daemon.dispatcher.default_flood_transport_key == get_auto_key_for("#usa")
