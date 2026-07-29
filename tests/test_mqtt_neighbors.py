"""Tests for the MQTT neighbours feature.

Covers the three pieces that carry real risk:

* the serialized scope sweep (one query in flight, deadline armed only after the
  request transmits) — the pacing the firmware had to be fixed to get right;
* response matching, which must consume a reply only when it authenticates AND
  echoes the pending tag, so unrelated RESPONSE traffic still reaches companions;
* the publish gate, which must reach opted-in brokers only.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openhop_core.protocol import CryptoUtils, Identity, LocalIdentity
from openhop_core.protocol.constants import PAYLOAD_TYPE_RESPONSE
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.handler_helpers.neighbor_scopes import (
    DEFAULT_MAX_SWEEP_SECONDS,
    STATUS_RESPONDED,
    STATUS_SEND_FAILED,
    STATUS_TIMEOUT,
    NeighborScopeHelper,
    NeighborSnapshot,
    ScopeResult,
)
from repeater.neighbors_publisher import (
    DEFAULT_INTERVAL_HOURS,
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    STARTUP_GRACE_SECONDS,
    STATE_KEY,
    NeighborsPublisher,
    build_neighbors_payload,
    normalize_interval_hours,
)


# ====================================================================
# Scaffolding
# ====================================================================
class _FakePacket:
    """Minimal Packet stand-in for the response-matching path."""

    do_not_retransmit = False

    def mark_do_not_retransmit(self):
        self.do_not_retransmit = True

    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)

    def get_payload_type(self):
        return PAYLOAD_TYPE_RESPONSE

    def get_raw_length(self):
        return len(self.payload) + 2


def _make_response_packet(
    responder: LocalIdentity, requester: LocalIdentity, tag: int, scopes: str
) -> _FakePacket:
    """Build the reply a neighbour sends: dest_hash + src_hash + encrypted body."""
    requester_identity = Identity(requester.get_public_key())
    shared_secret = requester_identity.calc_shared_secret(responder.get_private_key())
    plaintext = tag.to_bytes(4, "little") + int(time.time()).to_bytes(4, "little") + scopes.encode()
    cipher = CryptoUtils.encrypt_then_mac(shared_secret[:16], shared_secret, plaintext)
    payload = bytes([requester.get_public_key()[0], responder.get_public_key()[0]]) + cipher
    return _FakePacket(payload)


def _helper_with_injector(local_identity, injector, config=None):
    return NeighborScopeHelper(
        local_identity=local_identity,
        packet_injector=injector,
        airtime_manager=None,
        config=config or {},
    )


# ====================================================================
# Payload
# ====================================================================
def test_payload_orders_most_useful_first():
    payload = build_neighbors_payload(
        origin="node",
        origin_id="AA" * 32,
        self_scopes="DEN,APRS",
        entries=[
            {"pubkey": "cc", "snr": 3.0, "heard_secs_ago": 900, "scopes": "", "status": "timeout"},
            {
                "pubkey": "aa",
                "snr": 1.0,
                "heard_secs_ago": 10,
                "scopes": "DEN",
                "status": "responded",
            },
            {"pubkey": "bb", "snr": 9.0, "heard_secs_ago": 10, "scopes": "", "status": "timeout"},
        ],
    )

    assert [e["pubkey"] for e in payload["neighbors"]] == ["bb", "aa", "cc"]
    assert payload["self"] == {"scopes": "DEN,APRS", "default_scope": "*"}
    assert payload["origin_id"] == "AA" * 32
    assert payload["timestamp"]


def test_payload_keeps_every_entry_regardless_of_size():
    """Unlike the firmware there is no fixed publish buffer, so nothing is dropped."""
    entries = [
        {
            "pubkey": f"{i:064x}",
            "snr": 1.0,
            "heard_secs_ago": i,
            "scopes": "SCOPE" * 20,
            "status": "responded",
        }
        for i in range(200)
    ]
    payload = build_neighbors_payload(
        origin="node", origin_id="AA" * 32, self_scopes="", entries=entries
    )

    assert len(payload["neighbors"]) == 200
    assert len(json.dumps(payload)) > 10240


@pytest.mark.parametrize(
    "value,expected",
    [
        (24, 24.0),
        (MIN_INTERVAL_HOURS, float(MIN_INTERVAL_HOURS)),
        (MAX_INTERVAL_HOURS, float(MAX_INTERVAL_HOURS)),
        (MIN_INTERVAL_HOURS - 1, 24.0),  # out of range -> default, never clamped
        (MAX_INTERVAL_HOURS + 1, 24.0),
        ("nonsense", 24.0),
        (None, 24.0),
    ],
)
def test_interval_validation(value, expected):
    assert normalize_interval_hours(value) == expected


# ====================================================================
# Scope sweep pacing
# ====================================================================
@pytest.mark.asyncio
async def test_sweep_keeps_exactly_one_query_in_flight():
    """The pacing guarantee: query N+1 is not sent until N has resolved."""
    local = LocalIdentity()
    peers = [LocalIdentity() for _ in range(3)]
    targets = [
        NeighborSnapshot(pubkey=p.get_public_key().hex(), last_seen=time.time(), snr=5.0)
        for p in peers
    ]

    peers_by_hex = {p.get_public_key().hex(): p for p in peers}
    in_flight = 0
    max_in_flight = 0
    sent_to = []
    helper = None

    async def injector(packet, wait_for_ack=False):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        pending = helper._pending
        sent_to.append(pending.pubkey)
        await asyncio.sleep(0)  # yield, so an overlapping send would be observable
        # The neighbour answers once the request has "transmitted".
        response = _make_response_packet(peers_by_hex[pending.pubkey], local, pending.tag, "DEN")
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(helper.process_response_packet(response))
        )
        in_flight -= 1
        return True

    helper = _helper_with_injector(local, injector)
    results = await helper.sweep(targets)

    assert max_in_flight == 1
    assert sent_to == [t.pubkey for t in targets]
    assert all(r.status == STATUS_RESPONDED for r in results.values())
    assert all(r.scopes == "DEN" for r in results.values())


@pytest.mark.asyncio
async def test_sweep_marks_timeout_without_a_response():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_TIMEOUT
    assert helper._pending is None


@pytest.mark.asyncio
async def test_sweep_marks_send_failed_when_transmit_fails():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        return False

    helper = _helper_with_injector(local, injector)
    started = time.monotonic()
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_SEND_FAILED
    # No response window is opened for a packet that never transmitted.
    assert time.monotonic() - started < 1.0
    assert helper._pending is None


@pytest.mark.asyncio
async def test_sweep_budget_marks_remaining_targets_timeout():
    local = LocalIdentity()
    peers = [LocalIdentity() for _ in range(3)]
    targets = [
        NeighborSnapshot(pubkey=p.get_public_key().hex(), last_seen=time.time()) for p in peers
    ]

    async def injector(packet, wait_for_ack=False):
        await asyncio.sleep(0.05)
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={
            "mqtt_brokers": {
                "neighbors": {
                    "scope_response_timeout_seconds": 0.05,
                    "max_sweep_seconds": 0.12,
                }
            }
        },
    )
    results = await helper.sweep(targets)

    assert len(results) == 3
    assert results[targets[-1].pubkey].status == STATUS_TIMEOUT


@pytest.mark.asyncio
async def test_response_timeout_scales_with_radio_settings():
    local = LocalIdentity()

    class _Airtime:
        def __init__(self, airtime_ms):
            self._airtime_ms = airtime_ms

        def calculate_airtime(self, payload_len):
            return self._airtime_ms

    fast = NeighborScopeHelper(local, airtime_manager=_Airtime(50), config={})
    slow = NeighborScopeHelper(local, airtime_manager=_Airtime(2000), config={})

    assert slow.response_timeout() > fast.response_timeout()

    override = NeighborScopeHelper(
        local,
        airtime_manager=_Airtime(2000),
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 7}}},
    )
    assert override.response_timeout() == 7


# ====================================================================
# Response matching
# ====================================================================
@pytest.mark.asyncio
async def test_response_with_matching_tag_is_consumed():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    captured = {}

    async def injector(packet, wait_for_ack=False):
        captured["tag"] = helper._pending.tag
        response = _make_response_packet(peer, local, captured["tag"], "DEN,APRS")
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(helper.process_response_packet(response))
        )
        return True

    helper = _helper_with_injector(local, injector)
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_RESPONDED
    assert results[target.pubkey].scopes == "DEN,APRS"


@pytest.mark.asyncio
async def test_response_with_wrong_tag_is_not_consumed():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        bogus = _make_response_packet(peer, local, helper._pending.tag ^ 0xFFFF, "DEN")
        assert await helper.process_response_packet(bogus) is False
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_TIMEOUT


@pytest.mark.asyncio
async def test_response_from_unrelated_node_is_not_consumed():
    local = LocalIdentity()
    peer = LocalIdentity()
    stranger = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        # Correct tag, wrong sender: must not resolve the pending query.
        foreign = _make_response_packet(stranger, local, helper._pending.tag, "DEN")
        assert await helper.process_response_packet(foreign) is False
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_TIMEOUT


@pytest.mark.asyncio
async def test_response_with_no_query_pending_is_ignored():
    local = LocalIdentity()
    peer = LocalIdentity()
    helper = _helper_with_injector(local, None)

    packet = _make_response_packet(peer, local, 1234, "DEN")
    assert await helper.process_response_packet(packet) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x01",
        b"\x01\x02",  # header only, no ciphertext
        b"\x01\x02\x03\x04\x05",  # too short to carry a MAC + block
        bytes(64),  # right shape, garbage contents
    ],
)
async def test_malformed_response_never_raises_or_matches(payload):
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        # Truncated/garbage payloads must be rejected quietly, not blow up the
        # router thread that offers every RESPONSE to this matcher.
        malformed = _FakePacket(payload)
        assert await helper.process_response_packet(malformed) is False
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])

    assert results[target.pubkey].status == STATUS_TIMEOUT


@pytest.mark.asyncio
async def test_scopes_are_truncated_at_the_first_nul():
    """The responder builds a C string; the cipher zero-pads the tail."""
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        response = _make_response_packet(peer, local, helper._pending.tag, "DEN\x00junk")
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(helper.process_response_packet(response))
        )
        return True

    helper = _helper_with_injector(local, injector)
    results = await helper.sweep([target])

    assert results[target.pubkey].scopes == "DEN"


@pytest.mark.asyncio
async def test_cancelling_a_sweep_mid_send_clears_the_pending_query():
    """A leaked pending query would keep hiding RESPONSE packets from companions.

    The injector await is where a shutdown cancel lands: it covers the engine's
    TX-delay and duty-cycle deferral, which is most of a query's wall time.
    """
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        await asyncio.sleep(30)  # still "transmitting" when the cancel arrives
        return True

    helper = _helper_with_injector(local, injector)
    task = asyncio.create_task(helper.sweep([target]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert helper._pending is None
    assert helper.active is False

    # A response arriving afterwards must not be consumed by the dead query.
    stale = _make_response_packet(peer, local, 1, "DEN")
    assert await helper.process_response_packet(stale) is False


@pytest.mark.asyncio
async def test_concurrent_sweep_is_rejected():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def injector(packet, wait_for_ack=False):
        await asyncio.sleep(0.2)
        return True

    helper = _helper_with_injector(local, injector)
    first = asyncio.create_task(helper.sweep([target]))
    await asyncio.sleep(0.05)

    with pytest.raises(RuntimeError):
        await helper.sweep([target])

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_sweep_rereads_live_config():
    """delays.direct_tx_delay_factor is live-updatable and sizes the window."""
    local = LocalIdentity()
    config = {"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 5}}}
    helper = _helper_with_injector(local, None, config=config)
    assert helper.response_timeout() == 5

    config["mqtt_brokers"]["neighbors"]["scope_response_timeout_seconds"] = 11
    await helper.sweep([NeighborSnapshot(pubkey="aa" * 32, last_seen=time.time())])

    assert helper.response_timeout() == 11


# ====================================================================
# Router integration
# ====================================================================
class _StubRouter:
    """Exercises the real PacketRouter RESPONSE branch against a stub daemon."""

    def __init__(self, scope_helper):
        self.fanned_out = []
        self.recorded = []
        self.daemon = SimpleNamespace(
            neighbor_scope_helper=scope_helper,
            local_hash=0x11,
            repeater_handler=None,
        )

    async def _fan_out_to_bridges(self, packet, bridges, context=""):
        self.fanned_out.append((packet, dict(bridges), context))
        return (bool(bridges), False)

    def _companion_bridges_for_packet(self, packet, metadata):
        return {0x11: object()}

    def _record_for_ui(self, packet, metadata):
        self.recorded.append(packet)


async def _route_response(router_stub, packet):
    from repeater.packet_router import PacketRouter

    return await PacketRouter._route_packet(router_stub, packet)


@pytest.mark.asyncio
async def test_router_consumes_only_a_matching_scope_response():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())
    routed = {}

    async def injector(packet, wait_for_ack=False):
        response = _make_response_packet(peer, local, helper._pending.tag, "DEN")
        stub = _StubRouter(helper)
        await _route_response(stub, response)
        routed["consumed"] = stub
        return True

    helper = _helper_with_injector(local, injector)
    results = await helper.sweep([target])

    stub = routed["consumed"]
    assert results[target.pubkey].status == STATUS_RESPONDED
    # Consumed: not retransmitted, recorded, and never offered to a bridge.
    assert stub.fanned_out == []
    assert stub.recorded


@pytest.mark.asyncio
async def test_router_still_delivers_unrelated_responses_to_companions():
    """A companion's login reply must not be swallowed by an active sweep.

    Same 1-byte dest hash, same instant, different sender: the matcher must
    decline it so the companion bridge still sees it.
    """
    local = LocalIdentity()
    peer = LocalIdentity()
    stranger = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())
    routed = {}

    async def injector(packet, wait_for_ack=False):
        foreign = _make_response_packet(stranger, local, helper._pending.tag, "NOPE")
        foreign.payload[0] = 0x11  # collide with the companion's dest hash
        stub = _StubRouter(helper)
        await _route_response(stub, foreign)
        routed["stub"] = stub
        return True

    helper = _helper_with_injector(
        local,
        injector,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])

    stub = routed["stub"]
    assert results[target.pubkey].status == STATUS_TIMEOUT
    assert len(stub.fanned_out) == 1
    assert stub.fanned_out[0][2] == "RESPONSE"


# ====================================================================
# Publish gating
# ====================================================================
def _pusher_with_brokers(brokers):
    config = {
        "repeater": {"node_name": "test-node"},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 62500,
            "coding_rate": 8,
            "preamble_length": 17,
            "frequency": 869618000,
        },
        "mqtt_brokers": {
            "iata_code": "LAX",
            "status_interval": 0,
            "owner": "",
            "email": "",
            "brokers": brokers,
        },
    }
    identity = SimpleNamespace(get_public_key=lambda: bytes.fromhex("AB" * 32))
    return MeshCoreToMqttPusher(local_identity=identity, config=config)


def _broker(name, *, neighbors=False, enabled=True, fmt="letsmesh"):
    return {
        "name": name,
        "enabled": enabled,
        "host": f"{name}.example",
        "port": 1883,
        "transport": "tcp",
        "format": fmt,
        "use_jwt_auth": False,
        "neighbors": neighbors,
        "tls": {"enabled": False, "insecure": False},
    }


def _capture(conn):
    captured = []

    def _fake_publish(topic, payload, retain=False, qos=0):
        captured.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos})
        return None

    conn.client = MagicMock()
    conn.client.publish = _fake_publish
    conn._running = True
    return captured


def test_publish_neighbors_reaches_only_opted_in_brokers():
    pusher = _pusher_with_brokers([_broker("opted-in", neighbors=True), _broker("plain")])
    opted_in, plain = pusher.connections
    opted_capture = _capture(opted_in)
    plain_capture = _capture(plain)

    pusher.publish_neighbors({"neighbors": [], "self": {"scopes": ""}})

    assert len(opted_capture) == 1
    assert plain_capture == []
    assert opted_capture[0]["topic"] == "meshcore/LAX/" + "AB" * 32 + "/neighbors"
    assert opted_capture[0]["qos"] == 1
    assert opted_capture[0]["retain"] is False


def test_publish_neighbors_skips_disabled_broker():
    pusher = _pusher_with_brokers([_broker("off", neighbors=True, enabled=False)])
    captured = _capture(pusher.connections[0])

    results = pusher.publish_neighbors({"neighbors": []})

    assert captured == []
    assert results == []


def test_publish_neighbors_uses_custom_base_topic_for_legacy_format():
    pusher = _pusher_with_brokers([_broker("lan", neighbors=True, fmt="mqtt")])
    captured = _capture(pusher.connections[0])

    pusher.publish_neighbors({"neighbors": []})

    assert captured[0]["topic"] == "meshcore/repeater/test-node/neighbors"


def test_broker_opt_in_flags():
    pusher = _pusher_with_brokers([_broker("plain")])
    assert pusher.has_neighbors_brokers() is False

    pusher = _pusher_with_brokers([_broker("opted-in", neighbors=True)])
    assert pusher.has_neighbors_brokers() is True
    assert pusher.has_connected_neighbors_brokers() is False

    pusher.connections[0]._running = True
    assert pusher.has_connected_neighbors_brokers() is True


# ====================================================================
# Publisher gating and snapshotting
# ====================================================================
def _publisher(config, *, handler=None, storage=None, **kwargs):
    return NeighborsPublisher(
        config=config,
        mqtt_handler_provider=lambda: handler,
        storage_provider=lambda: storage,
        **kwargs,
    )


def test_publisher_disabled_without_opted_in_broker():
    handler = SimpleNamespace(has_neighbors_brokers=lambda: False)
    publisher = _publisher({"mqtt_brokers": {}}, handler=handler)

    assert publisher.enabled() is False
    assert publisher.status()["phase"] == "disabled"


def test_master_switch_overrides_broker_opt_in():
    handler = SimpleNamespace(has_neighbors_brokers=lambda: True)
    config = {"mqtt_brokers": {"neighbors": {"enabled": False}}}
    publisher = _publisher(config, handler=handler)

    assert publisher.master_enabled is False
    assert publisher.enabled() is False

    config["mqtt_brokers"]["neighbors"]["enabled"] = True
    assert publisher.enabled() is True


def test_snapshot_filters_to_fresh_zero_hop_repeaters():
    now = time.time()
    local = LocalIdentity()
    local_key = local.get_public_key().hex()
    storage = SimpleNamespace(
        get_neighbors=lambda: {
            "aa" * 32: {"is_repeater": True, "zero_hop": True, "last_seen": now, "snr": 4.0},
            "bb" * 32: {"is_repeater": True, "zero_hop": False, "last_seen": now, "snr": 9.0},
            "cc" * 32: {"is_repeater": False, "zero_hop": True, "last_seen": now, "snr": 9.0},
            "dd" * 32: {
                "is_repeater": True,
                "zero_hop": True,
                "last_seen": now - 100000,
                "snr": 9.0,
            },
            "ee": {"is_repeater": True, "zero_hop": True, "last_seen": now, "snr": 9.0},
            local_key: {"is_repeater": True, "zero_hop": True, "last_seen": now, "snr": 9.0},
        }
    )
    publisher = _publisher({"mqtt_brokers": {}}, storage=storage, local_identity=local)

    snapshot = publisher._snapshot_neighbors()

    # Multi-hop, non-repeater, stale, short-key and self rows are all excluded.
    assert [s.pubkey for s in snapshot] == ["aa" * 32]


def test_snapshot_merges_this_cycle_discovery_over_the_cached_table():
    """get_neighbors() serves a 60s cache that store_advert() does not invalidate.

    A neighbour that answered discovery seconds ago must still be queried, with
    its live SNR, even when the cached table has not caught up.
    """
    now = time.time()
    storage = SimpleNamespace(
        get_neighbors=lambda: {
            "aa" * 32: {
                "is_repeater": True,
                "zero_hop": True,
                "last_seen": now - 500,
                "snr": -3.0,
            }
        }
    )
    publisher = _publisher({"mqtt_brokers": {}}, storage=storage)

    # Responses seen during this cycle: one refresh, one brand-new neighbour.
    publisher._discovery_seen = {
        "aa" * 32: {"last_seen": now, "snr": 8.0},
        "bb" * 32: {"last_seen": now, "snr": 6.0},
    }

    snapshot = {s.pubkey: s for s in publisher._snapshot_neighbors()}

    assert set(snapshot) == {"aa" * 32, "bb" * 32}
    assert snapshot["aa" * 32].snr == 8.0  # live value wins over the cached row
    assert snapshot["aa" * 32].last_seen == now


def test_snapshot_is_capped_and_ordered_freshest_first():
    now = time.time()
    storage = SimpleNamespace(
        get_neighbors=lambda: {
            f"{i:064x}": {
                "is_repeater": True,
                "zero_hop": True,
                "last_seen": now - i,
                "snr": 1.0,
            }
            for i in range(10)
        }
    )
    publisher = _publisher({"mqtt_brokers": {"neighbors": {"max_neighbors": 3}}}, storage=storage)

    snapshot = publisher._snapshot_neighbors()

    assert [s.pubkey for s in snapshot] == [f"{i:064x}" for i in range(3)]


@pytest.mark.asyncio
async def test_cycle_publishes_table_with_unanswered_neighbors():
    """Firmware parity: the whole zero-hop table is published, timeouts included."""
    now = time.time()
    published = []
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: published.append(payload) or [("b", None)],
        node_name="test-node",
        public_key="AB" * 32,
    )
    storage = SimpleNamespace(
        get_neighbors=lambda: {
            "aa" * 32: {"is_repeater": True, "zero_hop": True, "last_seen": now, "snr": 4.0},
            "bb" * 32: {
                "is_repeater": True,
                "zero_hop": True,
                "last_seen": now - 30,
                "snr": 2.0,
            },
        }
    )

    class _Sweeper:
        async def sweep(self, targets):
            from repeater.handler_helpers.neighbor_scopes import ScopeResult

            return {
                targets[0].pubkey: ScopeResult(STATUS_RESPONDED, "DEN"),
                targets[1].pubkey: ScopeResult(STATUS_TIMEOUT),
            }

    publisher = _publisher(
        {"mqtt_brokers": {}},
        handler=handler,
        storage=storage,
        scope_helper=_Sweeper(),
        self_scopes_fn=lambda: "DEN,APRS",
    )

    result = await publisher.run_cycle(trigger="test")

    assert result["success"] is True
    assert result["neighbors"] == 2
    assert result["responded"] == 1

    payload = published[0]
    assert payload["self"] == {"scopes": "DEN,APRS", "default_scope": "*"}
    statuses = {e["pubkey"]: e["status"] for e in payload["neighbors"]}
    assert statuses == {"aa" * 32: STATUS_RESPONDED, "bb" * 32: STATUS_TIMEOUT}
    # A cycle always arms the next one, so a failure cannot wedge the schedule.
    assert publisher.status()["phase"] == "scheduled"


@pytest.mark.asyncio
async def test_cycle_rejects_reentry_while_active():
    publisher = _publisher({"mqtt_brokers": {}})
    publisher._active = True

    result = await publisher.run_cycle()

    assert result["success"] is False


@pytest.mark.asyncio
async def test_failed_publish_retries_sooner_than_a_full_interval():
    """A rejected payload must not cost a whole 24h interval."""
    from repeater.neighbors_publisher import RETRY_DELAY_SECONDS

    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: [],  # nothing reached a broker
        node_name="n",
        public_key="AB" * 32,
    )
    publisher = _publisher({"mqtt_brokers": {}}, handler=handler, storage=None)

    result = await publisher.run_cycle(trigger="test")

    assert result["published"] is False
    assert "publish failed" in publisher._last_result
    assert publisher.status()["secs_until_next"] <= RETRY_DELAY_SECONDS


def test_enricher_records_only_full_key_repeaters_and_never_self():
    local = LocalIdentity()
    publisher = _publisher({"mqtt_brokers": {}}, local_identity=local)
    publisher._discovery_seen = {}

    publisher._enrich_discovery_result({"pub_key": "aa" * 32, "node_type": 2, "response_snr": 7.5})
    publisher._enrich_discovery_result({"pub_key": "bb" * 32, "node_type": 1})  # chat node
    publisher._enrich_discovery_result({"pub_key": "cc", "node_type": 2})  # prefix only
    publisher._enrich_discovery_result(
        {"pub_key": local.get_public_key().hex(), "node_type": 2}
    )  # self

    assert set(publisher._discovery_seen) == {"aa" * 32}
    assert publisher._discovery_seen["aa" * 32]["snr"] == 7.5


def test_enricher_persists_discovery_results_to_storage():
    storage = SimpleNamespace(record_advert=MagicMock(), get_neighbors=lambda: {})
    publisher = _publisher({"mqtt_brokers": {}}, storage=storage)
    publisher._discovery_seen = {}

    publisher._enrich_discovery_result(
        {"pub_key": "aa" * 32, "node_type": 2, "response_snr": 3.0, "rssi": -90}
    )

    record = storage.record_advert.call_args.args[0]
    assert record["pubkey"] == "aa" * 32
    assert record["is_repeater"] is True
    assert record["zero_hop"] is True
    assert record["snr"] == 3.0


# ====================================================================
# Config validation
# ====================================================================
def _validate(raw):
    from repeater.web.api_endpoints import APIEndpoints

    return APIEndpoints._validate_neighbors_settings(raw)


@pytest.mark.parametrize(
    "raw,expect_error",
    [
        ({"interval_hours": 24}, False),
        ({"interval_hours": MIN_INTERVAL_HOURS}, False),
        ({"interval_hours": MAX_INTERVAL_HOURS}, False),
        ({"interval_hours": MIN_INTERVAL_HOURS - 1}, True),
        ({"interval_hours": MAX_INTERVAL_HOURS + 1}, True),
        ({"interval_hours": 12.5}, True),  # silently truncating would be worse
        ({"interval_hours": "24"}, True),
        ({"enabled": True}, False),
        ({"enabled": "false"}, True),  # would coerce to True
        ({"discovery_timeout_seconds": 60}, False),
        ({"discovery_timeout_seconds": 1}, True),
        ({"scope_response_timeout_seconds": 0}, False),
        ({"max_neighbors": 0}, True),
        ({"max_neighbor_age_seconds": 10}, True),
        ({"max_sweep_seconds": 900}, False),
        ({"duty_cycle_abort_seconds": 30}, False),
        ({"max_sweep_secondz": 5}, True),  # typo must not report success
        ("not a dict", True),
    ],
)
def test_neighbors_settings_validation(raw, expect_error):
    settings, error = _validate(raw)
    assert (error is not None) is expect_error
    if not expect_error:
        assert settings


def _api_with_stored_brokers(monkeypatch, brokers, neighbors_block=None):
    import cherrypy

    from repeater.web.api_endpoints import APIEndpoints

    request = SimpleNamespace(method="POST", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)

    api = APIEndpoints.__new__(APIEndpoints)
    api.config = {"mqtt_brokers": {"brokers": brokers, "neighbors": neighbors_block or {}}}
    api.daemon_instance = None
    api.send_advert_func = None
    api.event_loop = None
    api.stats_getter = None
    api._config_path = "/tmp/test-config.yaml"
    api.config_manager = MagicMock()
    api.config_manager.update_and_save.return_value = {"success": True, "saved": True}
    return api, request


def test_broker_neighbors_flag_survives_a_save_that_omits_it(monkeypatch):
    """A UI that predates the feature must not silently disable it.

    The broker rebuild is a strict field whitelist, so a client that never learned
    about `neighbors` would otherwise reset every broker to False on any unrelated
    MQTT save — turning the feature off with no error.
    """
    api, request = _api_with_stored_brokers(
        monkeypatch,
        [{"name": "keeper", "neighbors": True}, {"name": "plain", "neighbors": False}],
    )

    request.json = {
        "email": "someone@example.com",
        "brokers": [
            {"name": "keeper", "host": "h", "port": 1883, "format": "letsmesh"},
            {"name": "plain", "host": "h", "port": 1883, "format": "letsmesh"},
        ],
    }
    assert api.update_mqtt_config()["success"] is True

    saved = api.config_manager.update_and_save.call_args.kwargs["updates"]["mqtt_brokers"]
    by_name = {b["name"]: b["neighbors"] for b in saved["brokers"]}
    assert by_name == {"keeper": True, "plain": False}


def test_broker_neighbors_flag_can_be_turned_off_explicitly(monkeypatch):
    api, request = _api_with_stored_brokers(monkeypatch, [{"name": "keeper", "neighbors": True}])

    request.json = {
        "brokers": [
            {"name": "keeper", "host": "h", "port": 1883, "format": "letsmesh", "neighbors": False}
        ]
    }
    assert api.update_mqtt_config()["success"] is True

    saved = api.config_manager.update_and_save.call_args.kwargs["updates"]["mqtt_brokers"]
    assert saved["brokers"][0]["neighbors"] is False


def test_partial_neighbors_post_keeps_unmentioned_settings(monkeypatch):
    api, request = _api_with_stored_brokers(
        monkeypatch, [], neighbors_block={"interval_hours": 48, "max_neighbors": 8}
    )

    request.json = {"neighbors": {"enabled": False}}
    assert api.update_mqtt_config()["success"] is True

    saved = api.config_manager.update_and_save.call_args.kwargs["updates"]["mqtt_brokers"]
    assert saved["neighbors"] == {"interval_hours": 48, "max_neighbors": 8, "enabled": False}


def test_invalid_neighbors_interval_is_rejected_by_the_endpoint(monkeypatch):
    api, request = _api_with_stored_brokers(monkeypatch, [])

    request.json = {"neighbors": {"interval_hours": 1}}
    out = api.update_mqtt_config()

    assert out["success"] is False
    assert "between 12 and 336" in out["error"]
    api.config_manager.update_and_save.assert_not_called()


# ====================================================================
# Malformed config block
# ====================================================================
# `neighbors` is a settings block under mqtt_brokers but a boolean on each broker
# entry, and config.yaml.example documents both, so `mqtt_brokers.neighbors: true`
# is an easy hand-edit to make. Every reader used to call .get() on it directly.
@pytest.mark.parametrize("bogus", [True, 24, "on", ["DEN"]])
def test_scalar_neighbors_block_is_ignored_not_fatal(bogus):
    """A truthy scalar took the daemon down: the scope helper reads it in __init__."""
    config = {"mqtt_brokers": {"neighbors": bogus}}

    helper = NeighborScopeHelper(
        local_identity=LocalIdentity(), packet_injector=None, config=config
    )
    assert helper._max_sweep_seconds == DEFAULT_MAX_SWEEP_SECONDS

    publisher = NeighborsPublisher(config=config)
    assert publisher._neighbors_config == {}
    assert publisher.master_enabled is True
    assert publisher.interval_seconds == DEFAULT_INTERVAL_HOURS * 3600.0
    assert publisher.enabled() is False  # no handler, so nothing is published


@pytest.mark.parametrize("bogus", [True, 24, "on"])
def test_scalar_neighbors_block_is_repaired_by_a_save(monkeypatch, bogus):
    """The merge onto the stored block must not fail on a non-mapping."""
    api, request = _api_with_stored_brokers(monkeypatch, [], neighbors_block=bogus)

    request.json = {"neighbors": {"enabled": True, "interval_hours": 36}}
    assert api.update_mqtt_config()["success"] is True

    saved = api.config_manager.update_and_save.call_args.kwargs["updates"]["mqtt_brokers"]
    assert saved["neighbors"] == {"enabled": True, "interval_hours": 36}


def test_scalar_delays_block_does_not_break_the_response_window():
    helper = NeighborScopeHelper(
        local_identity=LocalIdentity(),
        packet_injector=None,
        config={"delays": True},
    )
    assert helper._direct_tx_delay_factor == 0.5


# ====================================================================
# Progress metadata (firmware total_neighbors / queried_neighbors)
# ====================================================================
def test_payload_reports_progress_metadata_in_firmware_key_order():
    payload = build_neighbors_payload(
        origin="node",
        origin_id="AA" * 32,
        self_scopes="DEN",
        entries=[
            {
                "pubkey": "aa",
                "snr": 1.0,
                "heard_secs_ago": 5,
                "scopes": "DEN",
                "status": "responded",
            }
        ],
        total_neighbors=4,
        queried_neighbors=2,
    )

    assert payload["total_neighbors"] == 4
    assert payload["queried_neighbors"] == 2
    # Firmware's buildNeighborsMessageBase writes the counters between origin_id
    # and self; keeping the order lets the two payloads diff cleanly.
    assert list(payload) == [
        "timestamp",
        "origin",
        "origin_id",
        "total_neighbors",
        "queried_neighbors",
        "self",
        "neighbors",
    ]


def test_payload_never_emits_the_firmware_truncated_field():
    """It reports a fixed PSRAM buffer overflowing, which openhop cannot have."""
    payload = build_neighbors_payload(
        origin="node",
        origin_id="AA" * 32,
        self_scopes="",
        entries=[],
        total_neighbors=3,
        queried_neighbors=1,
    )

    assert "truncated" not in payload


def test_payload_omits_progress_metadata_when_counts_are_absent():
    """Mirrors the firmware's `total_neighbors >= 0` guard."""
    payload = build_neighbors_payload(
        origin="node", origin_id="AA" * 32, self_scopes="", entries=[]
    )

    assert "total_neighbors" not in payload
    assert "queried_neighbors" not in payload


def test_queried_count_excludes_neighbors_never_put_on_air():
    """`status` cannot carry this: an unreached target also reports `timeout`."""
    from repeater.handler_helpers.neighbor_scopes import ScopeResult

    targets = [
        NeighborSnapshot(pubkey=f"{i:064x}", last_seen=time.time(), snr=1.0) for i in range(4)
    ]
    publisher = _publisher({"mqtt_brokers": {}})

    payload = publisher._build_payload(
        targets,
        {
            targets[0].pubkey: ScopeResult(STATUS_RESPONDED, "DEN", transmitted=True),
            targets[1].pubkey: ScopeResult(STATUS_TIMEOUT, transmitted=True),
            targets[2].pubkey: ScopeResult(STATUS_SEND_FAILED),  # never transmitted
            targets[3].pubkey: ScopeResult(STATUS_TIMEOUT),  # sweep never reached it
        },
    )

    assert payload["queried_neighbors"] == 2


def test_total_neighbors_always_matches_the_published_row_count():
    """Without `truncated` to flag a gap, the two must not diverge."""
    publisher = _publisher({"mqtt_brokers": {}})
    targets = [
        NeighborSnapshot(pubkey=f"{i:064x}", last_seen=time.time(), snr=1.0) for i in range(3)
    ]

    payload = publisher._build_payload(targets, {})

    assert payload["total_neighbors"] == len(payload["neighbors"]) == 3


@pytest.mark.asyncio
async def test_transmitted_flag_tracks_whether_the_request_reached_the_air():
    local = LocalIdentity()
    peer = LocalIdentity()
    target = NeighborSnapshot(pubkey=peer.get_public_key().hex(), last_seen=time.time())

    async def failed(packet, wait_for_ack=False):
        return False

    results = await _helper_with_injector(local, failed).sweep([target])
    assert results[target.pubkey].transmitted is False

    async def sent(packet, wait_for_ack=False):
        return True

    helper = _helper_with_injector(
        local,
        sent,
        config={"mqtt_brokers": {"neighbors": {"scope_response_timeout_seconds": 0.05}}},
    )
    results = await helper.sweep([target])
    assert results[target.pubkey].status == STATUS_TIMEOUT
    assert results[target.pubkey].transmitted is True


# ====================================================================
# Manual trigger endpoint
# ====================================================================
def _api_with_publisher(monkeypatch, publisher, method="POST", json=None, storage=None):
    import cherrypy

    from repeater.web.api_endpoints import APIEndpoints

    request = SimpleNamespace(method=method, params={}, json=json if json is not None else {})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)

    api = APIEndpoints.__new__(APIEndpoints)
    api.config = {}
    api.daemon_instance = SimpleNamespace(
        neighbors_publisher=publisher,
        repeater_handler=SimpleNamespace(storage=storage),
    )
    api.event_loop = asyncio.new_event_loop()
    api.send_advert_func = None
    api.stats_getter = None
    api._config_path = "/tmp/test-config.yaml"
    api.config_manager = MagicMock()
    return api


class _FakePublisher:
    def __init__(self, *, is_enabled=True, starts=True):
        self._enabled = is_enabled
        self._starts = starts
        self.triggered = 0

    def enabled(self):
        return self._enabled

    def trigger_cycle(self):
        self.triggered += 1
        return self._starts


def _run_endpoint(api, call=None):
    """Drive the endpoint's run_coroutine_threadsafe against a real loop."""
    import threading

    loop = api.event_loop
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        return (call or api.publish_neighbors)()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        # Closing a loop whose thread has not finished unwinding can raise on its
        # self-pipe descriptors; leak it rather than risk a flaky teardown.
        if not thread.is_alive():
            loop.close()


def test_publish_neighbors_endpoint_starts_a_cycle(monkeypatch):
    publisher = _FakePublisher()
    api = _api_with_publisher(monkeypatch, publisher)

    out = _run_endpoint(api)

    assert out["success"] is True
    assert publisher.triggered == 1


def test_publish_neighbors_endpoint_reports_an_already_running_cycle(monkeypatch):
    publisher = _FakePublisher(starts=False)
    api = _api_with_publisher(monkeypatch, publisher)

    out = _run_endpoint(api)

    assert out["success"] is False
    assert "already running" in out["error"]


def test_publish_neighbors_endpoint_refuses_when_disabled(monkeypatch):
    """No broker opted in: refuse rather than burn airtime on an unpublishable cycle."""
    publisher = _FakePublisher(is_enabled=False)
    api = _api_with_publisher(monkeypatch, publisher)

    out = api.publish_neighbors()

    assert out["success"] is False
    assert publisher.triggered == 0


def test_publish_neighbors_endpoint_without_a_publisher(monkeypatch):
    api = _api_with_publisher(monkeypatch, None)

    out = api.publish_neighbors()

    assert out["success"] is False
    assert "not available" in out["error"]


# ====================================================================
# self.default_scope
# ====================================================================
@pytest.mark.parametrize(
    "mesh_cfg,expected",
    [
        ({"default_region": "DEN"}, "DEN"),
        ({"default_region": "#DEN"}, "DEN"),  # transport-key tables prefix with '#'
        ({"default_region": "  DEN  "}, "DEN"),
        ({"default_region": None}, "*"),  # unset -> floods unscoped
        ({"default_region": ""}, "*"),
        ({"default_region": "   "}, "*"),
        ({}, "*"),  # key absent entirely
        ({"default_region": "*"}, "*"),
        (True, "*"),  # hand-edited scalar must not raise
    ],
)
def test_self_default_scope_normalisation(mesh_cfg, expected):
    publisher = _publisher({"mqtt_brokers": {}, "mesh": mesh_cfg})
    assert publisher._self_default_scope() == expected


def test_default_scope_is_published_inside_self():
    publisher = _publisher({"mqtt_brokers": {}, "mesh": {"default_region": "#PDX"}})

    payload = publisher._build_payload([], {})

    assert payload["self"]["default_scope"] == "PDX"
    assert "scopes" in payload["self"]


def test_default_scope_tracks_a_live_config_edit():
    """`region default <name>` writes into the same dict and must not need a restart."""
    config = {"mqtt_brokers": {}, "mesh": {"default_region": None}}
    publisher = _publisher(config)

    assert publisher._build_payload([], {})["self"]["default_scope"] == "*"

    config["mesh"]["default_region"] = "DEN"
    assert publisher._build_payload([], {})["self"]["default_scope"] == "DEN"


def test_payload_defaults_default_scope_to_the_wildcard():
    """Callers that omit it still emit a valid self block."""
    payload = build_neighbors_payload(
        origin="node", origin_id="AA" * 32, self_scopes="DEN", entries=[]
    )

    assert payload["self"] == {"scopes": "DEN", "default_scope": "*"}


# ====================================================================
# Schedule persistence across restarts
# ====================================================================
class _FakeStateStore:
    """Stands in for the daemon_state table."""

    def __init__(self, initial=None):
        self.rows = dict(initial or {})
        self.writes = 0

    def get_daemon_state(self, key):
        return self.rows.get(key)

    def set_daemon_state(self, key, value):
        self.rows[key] = dict(value)
        self.writes += 1
        return True


def _enabled_handler():
    return SimpleNamespace(
        has_neighbors_brokers=lambda: True, has_connected_neighbors_brokers=lambda: True
    )


def test_restore_resumes_the_interval_from_the_last_successful_publish():
    now = time.time()
    store = _FakeStateStore({STATE_KEY: {"last_success_at": now - 3600, "last_result": "ok"}})
    publisher = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=_enabled_handler(),
        storage=store,
    )

    publisher._restore_schedule()

    # 1h since the last publish on a 24h interval -> ~23h to go, not "due now".
    secs = publisher.status()["secs_until_next"]
    assert 22.9 * 3600 < secs < 23.1 * 3600
    assert publisher.status()["phase"] == "scheduled"
    assert publisher._last_result == "ok"


def test_restore_applies_the_grace_delay_when_already_overdue():
    """A node off for a week must not transmit the instant it boots."""
    store = _FakeStateStore(
        {STATE_KEY: {"last_success_at": time.time() - 7 * 86400, "last_result": "ok"}}
    )
    publisher = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=_enabled_handler(),
        storage=store,
    )

    publisher._restore_schedule()

    assert publisher.status()["secs_until_next"] == pytest.approx(STARTUP_GRACE_SECONDS, abs=2)


@pytest.mark.parametrize(
    "state",
    [
        None,  # fresh install
        {},
        {"last_success_at": None},
        {"last_success_at": "nonsense"},
        {"last_success_at": 0},
        # Clock moved backwards, or a junk row: must not park the schedule in the
        # far future where nothing would ever publish again.
        {"last_success_at": time.time() + 5 * 86400},
    ],
)
def test_restore_falls_back_to_the_grace_delay_on_unusable_state(state):
    store = _FakeStateStore({STATE_KEY: state} if state is not None else {})
    publisher = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=_enabled_handler(),
        storage=store,
    )

    publisher._restore_schedule()

    assert publisher.status()["secs_until_next"] == pytest.approx(STARTUP_GRACE_SECONDS, abs=2)


def test_restore_clamps_a_delay_longer_than_the_interval():
    """An interval shortened since the last publish must take effect now."""
    store = _FakeStateStore({STATE_KEY: {"last_success_at": time.time()}})
    publisher = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 12}}},
        handler=_enabled_handler(),
        storage=store,
    )
    publisher._restore_schedule()
    assert publisher.status()["secs_until_next"] <= 12 * 3600

    # Same stored publish time, but the config now says 24h -> still bounded.
    publisher.config["mqtt_brokers"]["neighbors"]["interval_hours"] = 24
    publisher._restore_schedule()
    assert publisher.status()["secs_until_next"] <= 24 * 3600


def test_restore_without_a_state_capable_storage_backend():
    """An older storage backend must still start, just without resuming."""
    publisher = _publisher({"mqtt_brokers": {}}, handler=_enabled_handler(), storage=object())

    publisher._restore_schedule()

    assert publisher.status()["secs_until_next"] == pytest.approx(STARTUP_GRACE_SECONDS, abs=2)


@pytest.mark.asyncio
async def test_failed_publish_is_not_recorded_as_a_successful_one():
    """Otherwise a restart turns the 15-minute retry into a full interval."""
    store = _FakeStateStore()
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: [],  # reached no broker
        node_name="n",
        public_key="AB" * 32,
    )
    store.get_neighbors = lambda: {}
    publisher = _publisher({"mqtt_brokers": {}}, handler=handler, storage=store)

    await publisher.run_cycle(trigger="test")

    saved = store.rows[STATE_KEY]
    assert saved["last_success_at"] is None
    assert saved["last_publish_at"] is not None  # the attempt is still recorded
    assert "publish failed" in saved["last_result"]

    # A restart therefore retries promptly rather than waiting out the interval.
    restarted = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=_enabled_handler(),
        storage=store,
    )
    restarted._restore_schedule()
    assert restarted.status()["secs_until_next"] == pytest.approx(STARTUP_GRACE_SECONDS, abs=2)


@pytest.mark.asyncio
async def test_successful_publish_persists_a_resumable_schedule():
    store = _FakeStateStore()
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: [("broker", None)],
        node_name="n",
        public_key="AB" * 32,
    )
    store.get_neighbors = lambda: {}
    publisher = _publisher({"mqtt_brokers": {}}, handler=handler, storage=store)

    await publisher.run_cycle(trigger="test")

    assert store.rows[STATE_KEY]["last_success_at"] == pytest.approx(time.time(), abs=5)

    restarted = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=_enabled_handler(),
        storage=store,
    )
    restarted._restore_schedule()
    assert restarted.status()["secs_until_next"] > 23 * 3600


@pytest.mark.asyncio
async def test_boot_tick_does_not_discard_the_restored_schedule():
    """The disabled branch of _tick must not fire before we were ever enabled.

    At boot the MQTT connections may not be up, so enabled() can briefly be
    False. Clearing the schedule there would mark the node due and re-run the
    sweep on every restart -- the exact thing persistence prevents.
    """
    store = _FakeStateStore({STATE_KEY: {"last_success_at": time.time() - 3600}})
    not_ready = SimpleNamespace(has_neighbors_brokers=lambda: False)
    publisher = _publisher(
        {"mqtt_brokers": {"neighbors": {"interval_hours": 24}}},
        handler=not_ready,
        storage=store,
    )
    publisher._restore_schedule()
    restored = publisher._next_publish_at

    await publisher._tick()

    assert publisher._next_publish_at == restored


@pytest.mark.asyncio
async def test_disabling_after_being_enabled_still_clears_the_schedule():
    """Re-enabling should publish promptly; that behaviour is preserved."""
    store = _FakeStateStore()
    enabled = True
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: enabled,
        has_connected_neighbors_brokers=lambda: False,
    )
    publisher = _publisher({"mqtt_brokers": {}}, handler=handler, storage=store)
    publisher._next_publish_at = time.monotonic() + 3600

    await publisher._tick()  # enabled, but no connected broker -> schedule kept
    assert publisher._next_publish_at is not None

    enabled = False
    await publisher._tick()
    assert publisher._next_publish_at is None


def test_daemon_state_round_trips_through_real_sqlite(tmp_path):
    """The fake store above cannot prove the migration or the accessors work."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)

    assert handler.get_daemon_state(STATE_KEY) is None  # absent, not an error

    assert handler.set_daemon_state(STATE_KEY, {"last_success_at": 1785372000.0}) is True
    assert handler.get_daemon_state(STATE_KEY) == {"last_success_at": 1785372000.0}

    # Upsert, not a second row.
    assert handler.set_daemon_state(STATE_KEY, {"last_success_at": 1785458400.0}) is True
    assert handler.get_daemon_state(STATE_KEY)["last_success_at"] == 1785458400.0

    # A fresh handler on the same file sees it -- this is the restart path.
    assert SQLiteHandler(tmp_path).get_daemon_state(STATE_KEY)["last_success_at"] == 1785458400.0


def test_daemon_state_survives_a_corrupt_row(tmp_path):
    """Unparseable state must read as "no history", never break startup."""
    import sqlite3

    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    handler.set_daemon_state(STATE_KEY, {"last_success_at": 1.0})
    with sqlite3.connect(handler.sqlite_path) as conn:
        conn.execute("UPDATE daemon_state SET value_json = ?", ("{not json",))
        conn.commit()

    assert handler.get_daemon_state(STATE_KEY) is None

    publisher = _publisher({"mqtt_brokers": {}}, handler=_enabled_handler(), storage=handler)
    publisher._restore_schedule()
    assert publisher.status()["secs_until_next"] == pytest.approx(STARTUP_GRACE_SECONDS, abs=2)


def test_migration_is_idempotent_on_an_existing_database(tmp_path):
    """Migration 14 runs against nibbler's populated DB, not a fresh file."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    first = SQLiteHandler(tmp_path)
    first.set_daemon_state(STATE_KEY, {"last_success_at": 42.0})

    # Re-running migrations must not drop the table or its contents.
    for _ in range(3):
        SQLiteHandler(tmp_path)._run_migrations()

    assert SQLiteHandler(tmp_path).get_daemon_state(STATE_KEY) == {"last_success_at": 42.0}


# ====================================================================
# Scope persistence
# ====================================================================
class _FakeScopeStore:
    """Neighbour table plus the scope rows, without touching sqlite."""

    def __init__(self, neighbors=None, scopes=None):
        self._neighbors = dict(neighbors or {})
        self.scopes = dict(scopes or {})
        self.writes = []

    def get_neighbors(self):
        return self._neighbors

    def get_neighbor_scopes(self):
        return self.scopes

    def record_neighbor_scope(self, pubkey, status, scopes=None, queried_at=None):
        self.writes.append((pubkey, status, scopes))
        row = self.scopes.setdefault(pubkey, {"scopes": "", "responded_at": None})
        row["status"] = status
        row["queried_at"] = queried_at
        if scopes is not None:
            row["scopes"] = scopes
            row["responded_at"] = queried_at
        return True


class _StubSweep:
    """Scope helper stand-in returning canned results for one sweep."""

    def __init__(self, results):
        self.results = results
        self.targets = None

    async def sweep(self, targets):
        self.targets = list(targets)
        return dict(self.results)


def _repeater_row(last_seen=None, snr=6.0):
    return {
        "is_repeater": True,
        "zero_hop": True,
        "last_seen": time.time() if last_seen is None else last_seen,
        "snr": snr,
    }


@pytest.mark.asyncio
async def test_cycle_persists_what_the_sweep_learned():
    """The MQTT payload used to be the only consumer, leaving the UI nothing."""
    answered, silent = "aa" * 32, "bb" * 32
    store = _FakeScopeStore({answered: _repeater_row(), silent: _repeater_row()})
    helper = _StubSweep(
        {
            answered: ScopeResult(STATUS_RESPONDED, "DEN,BOU", transmitted=True),
            silent: ScopeResult(STATUS_TIMEOUT, transmitted=True),
        }
    )
    publisher = _publisher(
        {"mqtt_brokers": {}},
        handler=SimpleNamespace(
            has_neighbors_brokers=lambda: True,
            has_connected_neighbors_brokers=lambda: True,
            publish_neighbors=lambda payload: [("broker", None)],
            node_name="n",
            public_key="AB" * 32,
        ),
        storage=store,
        scope_helper=helper,
    )

    await publisher.run_cycle(trigger="test")

    assert store.scopes[answered]["scopes"] == "DEN,BOU"
    assert store.scopes[answered]["responded_at"] is not None
    # A silent neighbour is recorded as asked, but claims no scopes.
    assert store.scopes[silent]["status"] == STATUS_TIMEOUT
    assert store.scopes[silent]["responded_at"] is None


@pytest.mark.asyncio
async def test_neighbors_never_put_on_air_are_not_recorded_as_queried():
    """`timeout` covers both "asked, silent" and "never reached"; only the first counts."""
    unreached = "cc" * 32
    store = _FakeScopeStore({unreached: _repeater_row()})
    publisher = _publisher(
        {"mqtt_brokers": {}},
        handler=SimpleNamespace(
            has_neighbors_brokers=lambda: True,
            has_connected_neighbors_brokers=lambda: True,
            publish_neighbors=lambda payload: [("broker", None)],
            node_name="n",
            public_key="AB" * 32,
        ),
        storage=store,
        scope_helper=_StubSweep({unreached: ScopeResult(STATUS_TIMEOUT, transmitted=False)}),
    )

    await publisher.run_cycle(trigger="test")

    assert store.writes == []
    assert store.scopes == {}


@pytest.mark.asyncio
async def test_query_one_returns_and_stores_the_answer():
    target = "dd" * 32
    store = _FakeScopeStore({target: _repeater_row(snr=3.5)})
    helper = _StubSweep({target: ScopeResult(STATUS_RESPONDED, "DEN", transmitted=True)})
    publisher = _publisher({"mqtt_brokers": {}}, storage=store, scope_helper=helper)

    out = await publisher.query_one(target.upper())

    assert out["status"] == STATUS_RESPONDED
    assert out["scopes"] == "DEN"
    assert out["responded_at"] is not None
    assert store.scopes[target]["scopes"] == "DEN"
    # The snapshot borrows the stored row so a single query walks the sweep path.
    assert helper.targets[0].snr == 3.5


@pytest.mark.asyncio
async def test_query_one_does_not_publish():
    """Only the periodic cycle writes to the neighbors topic."""
    target = "ee" * 32
    published = []
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: published.append(payload) or [("broker", None)],
        node_name="n",
        public_key="AB" * 32,
    )
    publisher = _publisher(
        {"mqtt_brokers": {}},
        handler=handler,
        storage=_FakeScopeStore({target: _repeater_row()}),
        scope_helper=_StubSweep({target: ScopeResult(STATUS_RESPONDED, "DEN", transmitted=True)}),
    )

    await publisher.query_one(target)

    assert published == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abcd", "zz" * 32, "aa" * 31])
async def test_query_one_rejects_keys_it_cannot_query(bad):
    """ECDH needs the full 32-byte key; a prefix or non-hex cannot be used."""
    publisher = _publisher({"mqtt_brokers": {}}, scope_helper=_StubSweep({}))

    with pytest.raises(ValueError):
        await publisher.query_one(bad)


@pytest.mark.asyncio
async def test_query_one_refuses_our_own_key():
    local = LocalIdentity()
    publisher = _publisher({"mqtt_brokers": {}}, scope_helper=_StubSweep({}), local_identity=local)

    with pytest.raises(ValueError):
        await publisher.query_one(local.get_public_key().hex())


@pytest.mark.asyncio
async def test_query_one_reports_a_sweep_already_in_progress():
    """One request in flight at a time; the wording has to be actionable."""
    local = LocalIdentity()
    helper = _helper_with_injector(local, lambda packet, wait_for_ack=False: True)
    publisher = _publisher({"mqtt_brokers": {}}, scope_helper=helper)

    async with helper._sweep_lock:
        with pytest.raises(RuntimeError, match="already running"):
            await publisher.query_one("ff" * 32)


@pytest.mark.asyncio
async def test_query_one_without_storage_still_answers():
    """Scope queries are useful on a repeater with no storage wired up."""
    target = "ab" * 32
    helper = _StubSweep({target: ScopeResult(STATUS_RESPONDED, "", transmitted=True)})
    publisher = _publisher({"mqtt_brokers": {}}, storage=None, scope_helper=helper)

    out = await publisher.query_one(target)

    assert out["status"] == STATUS_RESPONDED
    assert out["scopes"] == ""  # a real answer: unscoped traffic only


def test_neighbor_scopes_round_trip_through_real_sqlite(tmp_path):
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    pubkey = "1f" * 32

    assert handler.get_neighbor_scopes() == {}

    assert handler.record_neighbor_scope(pubkey, STATUS_RESPONDED, "DEN,BOU", 1785372000.0) is True
    row = handler.get_neighbor_scopes()[pubkey]
    assert row == {
        "scopes": "DEN,BOU",
        "responded_at": 1785372000.0,
        "status": STATUS_RESPONDED,
        "queried_at": 1785372000.0,
    }

    # An empty answer is an answer -- the neighbour serves unscoped traffic only.
    handler.record_neighbor_scope(pubkey, STATUS_RESPONDED, "", 1785372600.0)
    assert handler.get_neighbor_scopes()[pubkey]["scopes"] == ""

    # Keys are normalised, so an upper-case caller does not create a second row.
    handler.record_neighbor_scope(pubkey.upper(), STATUS_RESPONDED, "DEN", 1785373000.0)
    assert list(handler.get_neighbor_scopes()) == [pubkey]

    # Survives a restart.
    assert SQLiteHandler(tmp_path).get_neighbor_scopes()[pubkey]["scopes"] == "DEN"


def test_failed_query_keeps_the_last_known_scopes(tmp_path):
    """The responder rate-limits anon replies, so one timeout is weak evidence."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    pubkey = "2f" * 32
    handler.record_neighbor_scope(pubkey, STATUS_RESPONDED, "DEN", 1785372000.0)

    handler.record_neighbor_scope(pubkey, STATUS_TIMEOUT, None, 1785375600.0)

    row = handler.get_neighbor_scopes()[pubkey]
    assert row["scopes"] == "DEN"
    assert row["responded_at"] == 1785372000.0  # still says how fresh the scopes are
    assert row["status"] == STATUS_TIMEOUT
    assert row["queried_at"] == 1785375600.0


def test_deleting_a_neighbor_drops_its_scope_row(tmp_path):
    """Otherwise the row outlives the neighbour with nothing to display it against."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    pubkey = "3f" * 32
    handler.store_advert(
        {
            "pubkey": pubkey,
            "node_name": "gone",
            "is_repeater": True,
            "contact_type": "Repeater",
            "timestamp": time.time(),
        }
    )
    handler.record_neighbor_scope(pubkey, STATUS_RESPONDED, "DEN", time.time())
    advert_id = handler.get_adverts_by_contact_type(contact_type="Repeater")[0]["id"]

    assert handler.delete_advert(advert_id) is True
    assert handler.get_neighbor_scopes() == {}


def test_scope_migration_is_idempotent_on_an_existing_database(tmp_path):
    """Migration 15 runs against nibbler's populated DB, not a fresh file."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    pubkey = "4f" * 32
    SQLiteHandler(tmp_path).record_neighbor_scope(pubkey, STATUS_RESPONDED, "DEN", 42.0)

    for _ in range(3):
        SQLiteHandler(tmp_path)._run_migrations()

    assert SQLiteHandler(tmp_path).get_neighbor_scopes()[pubkey]["scopes"] == "DEN"


# ====================================================================
# Scope endpoints
# ====================================================================
class _QueryPublisher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def query_one(self, pubkey):
        self.calls.append(pubkey)
        if self.error:
            raise self.error
        return self.result


def test_neighbor_scopes_endpoint_serves_the_stored_table(monkeypatch):
    store = _FakeScopeStore(
        scopes={"5f" * 32: {"scopes": "DEN", "status": STATUS_RESPONDED, "queried_at": 1.0}}
    )
    api = _api_with_publisher(monkeypatch, None, method="GET", storage=store)

    out = api.neighbor_scopes()

    assert out["success"] is True
    assert out["count"] == 1
    assert out["data"]["5f" * 32]["scopes"] == "DEN"


def test_query_neighbor_scopes_endpoint_returns_the_answer(monkeypatch):
    target = "6f" * 32
    publisher = _QueryPublisher(
        {"pubkey": target, "status": STATUS_RESPONDED, "scopes": "DEN", "transmitted": True}
    )
    api = _api_with_publisher(monkeypatch, publisher, json={"pubkey": target})

    out = _run_endpoint(api, api.query_neighbor_scopes)

    assert out["success"] is True
    assert out["data"]["scopes"] == "DEN"
    assert publisher.calls == [target]


def test_query_neighbor_scopes_endpoint_requires_a_pubkey(monkeypatch):
    publisher = _QueryPublisher()
    api = _api_with_publisher(monkeypatch, publisher, json={})

    out = api.query_neighbor_scopes()

    assert out["success"] is False
    assert publisher.calls == []


@pytest.mark.parametrize(
    "error",
    [ValueError("A full 64-character public key is required"), RuntimeError("already running")],
)
def test_query_neighbor_scopes_endpoint_surfaces_refusals(monkeypatch, error):
    """Both a bad key and a busy sweep must read as a message, not a 500."""
    publisher = _QueryPublisher(error=error)
    api = _api_with_publisher(monkeypatch, publisher, json={"pubkey": "7f" * 32})

    out = _run_endpoint(api, api.query_neighbor_scopes)

    assert out["success"] is False
    assert str(error) in out["error"]


def test_query_neighbor_scopes_endpoint_without_a_publisher(monkeypatch):
    api = _api_with_publisher(monkeypatch, None, json={"pubkey": "8f" * 32})

    out = api.query_neighbor_scopes()

    assert out["success"] is False
    assert "not available" in out["error"]


@pytest.mark.parametrize("name", ["query_neighbor_scopes"])
def test_json_body_endpoints_enable_the_json_in_tool(name):
    """cherrypy.request.json only exists when json_in is on for the handler.

    It is not enabled globally for /api, and Request has no __getattr__, so a
    handler that reads .json without the decorator raises AttributeError on every
    real request -- while a test that fabricates cherrypy.request still passes.
    That is exactly how this was missed, hence a check on the decorator itself.
    """
    from repeater.web.api_endpoints import APIEndpoints

    handler = getattr(APIEndpoints, name)
    config = getattr(handler, "_cp_config", {})
    assert config.get("tools.json_in.on") is True


# ====================================================================
# Scope persistence: the rules the review found broken
# ====================================================================
@pytest.mark.asyncio
async def test_duty_cycle_refusal_is_recorded_as_a_query():
    """Nothing reached the air, but the node did try -- and kept trying.

    Recording only `transmitted` outcomes left a repeater that refuses on duty
    cycle reading as "never queried" forever, however often it was asked.
    """
    target = "1a" * 32
    store = _FakeScopeStore({target: _repeater_row()})
    publisher = _publisher(
        {"mqtt_brokers": {}},
        storage=store,
        scope_helper=_StubSweep({target: ScopeResult(STATUS_SEND_FAILED, transmitted=False)}),
    )

    out = await publisher.query_one(target)

    assert out["status"] == STATUS_SEND_FAILED
    assert out["queried_at"] is not None
    assert store.scopes[target]["status"] == STATUS_SEND_FAILED


@pytest.mark.asyncio
async def test_failed_query_returns_the_scopes_still_on_record():
    """The row keeps the last answer, so the response must not tell the UI to drop it."""
    target = "2a" * 32
    store = _FakeScopeStore({target: _repeater_row()})
    publisher = _publisher(
        {"mqtt_brokers": {}},
        storage=store,
        scope_helper=_StubSweep(
            {target: ScopeResult(STATUS_RESPONDED, "DEN,BOU", transmitted=True)}
        ),
    )
    await publisher.query_one(target)

    publisher.scope_helper = _StubSweep({target: ScopeResult(STATUS_TIMEOUT, transmitted=True)})
    out = await publisher.query_one(target)

    assert out["status"] == STATUS_TIMEOUT
    assert out["scopes"] == "DEN,BOU"  # not blanked
    assert out["responded_at"] is not None
    assert out["responded_at"] < out["queried_at"]


@pytest.mark.asyncio
async def test_query_refuses_while_a_cycle_is_running():
    """The cycle owns the helper for its whole run, discovery window included."""
    publisher = _publisher({"mqtt_brokers": {}}, scope_helper=_StubSweep({}))
    publisher._active = True

    with pytest.raises(RuntimeError, match="cycle is running"):
        await publisher.query_one("3a" * 32)


@pytest.mark.asyncio
async def test_a_cycle_defers_rather_than_dies_when_a_query_holds_the_helper():
    """Colliding with a manual query must not cost a whole cycle's airtime."""
    target = "4a" * 32
    store = _FakeScopeStore({target: _repeater_row()})
    published = []
    handler = SimpleNamespace(
        has_neighbors_brokers=lambda: True,
        has_connected_neighbors_brokers=lambda: True,
        publish_neighbors=lambda payload: published.append(payload) or [("broker", None)],
        node_name="n",
        public_key="AB" * 32,
    )

    class _BusyHelper:
        async def sweep(self, targets):
            raise RuntimeError("neighbor scope sweep already active")

    publisher = _publisher(
        {"mqtt_brokers": {}}, handler=handler, storage=store, scope_helper=_BusyHelper()
    )

    out = await publisher.run_cycle(trigger="test")

    assert out["success"] is False
    # No scope-less table published, and the short retry delay applies.
    assert published == []
    assert "deferred" in publisher._last_result
    assert publisher.status()["secs_until_next"] <= 900


@pytest.mark.asyncio
async def test_a_query_in_flight_blocks_a_cycle_from_starting():
    publisher = _publisher(
        {"mqtt_brokers": {}}, handler=_enabled_handler(), scope_helper=_StubSweep({})
    )
    publisher._queries_in_flight = 1

    assert publisher.trigger_cycle() is False

    publisher._next_publish_at = None  # due
    await publisher._tick()
    assert publisher._last_publish_at is None  # no cycle ran


@pytest.mark.asyncio
async def test_shutdown_cancels_an_in_flight_query():
    """A query holds the radio for a response window; teardown must cut it short."""
    local = LocalIdentity()
    peer = LocalIdentity()  # a real key, so the request actually builds
    on_air = asyncio.Event()

    async def _never_returns(packet, wait_for_ack=False):
        on_air.set()
        await asyncio.sleep(3600)

    helper = _helper_with_injector(local, _never_returns)
    publisher = _publisher({"mqtt_brokers": {}}, scope_helper=helper, local_identity=local)

    task = asyncio.create_task(publisher.query_one(peer.get_public_key().hex()))
    # Waited on an event rather than polling the task set: if the query fails
    # before it gets on air this fails fast instead of spinning.
    await asyncio.wait_for(on_air.wait(), timeout=5)
    assert publisher._query_tasks

    await publisher.stop()

    assert task.done()
    assert publisher._queries_in_flight == 0


def test_retention_cleanup_drops_scope_rows_for_pruned_neighbours(tmp_path):
    """cleanup_old_data is the path that actually runs unattended on the Pi."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    gone, kept = "6a" * 32, "7a" * 32
    old = time.time() - (60 * 24 * 3600)
    for pubkey, ts in ((gone, old), (kept, time.time())):
        handler.store_advert(
            {
                "pubkey": pubkey,
                "node_name": pubkey[:4],
                "is_repeater": True,
                "contact_type": "Repeater",
                "timestamp": ts,
            }
        )
        handler.record_neighbor_scope(pubkey, STATUS_RESPONDED, "DEN", time.time())

    handler.cleanup_old_data(days=31)

    remaining = handler.get_neighbor_scopes()
    assert kept in remaining
    assert gone not in remaining


def test_purging_the_advert_table_takes_the_scopes_with_it(tmp_path):
    """Otherwise the UI shows scope counts for repeaters it no longer lists."""
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    handler = SQLiteHandler(tmp_path)
    handler.record_neighbor_scope("8a" * 32, STATUS_RESPONDED, "DEN", time.time())

    handler.purge_table("adverts")

    assert handler.get_neighbor_scopes() == {}


def test_neighbor_scopes_endpoint_reports_our_own_served_scopes(monkeypatch):
    """The comparison a client draws must be against what we tell neighbours.

    Same formatter as the anon-regions responder, so "we serve this too" cannot
    drift from the answer a neighbour would get by asking us.
    """
    store = _FakeScopeStore(scopes={})
    api = _api_with_publisher(monkeypatch, None, method="GET", storage=store)
    api.daemon_instance.login_helper = SimpleNamespace(_format_region_names=lambda: "*,DEN,BOU")

    out = api.neighbor_scopes()

    assert out["success"] is True
    assert out["served"]["scopes"] == "*,DEN,BOU"


def test_served_scopes_degrade_to_empty_without_a_login_helper(monkeypatch):
    """An older or half-started daemon must not fail the whole request."""
    api = _api_with_publisher(monkeypatch, None, method="GET", storage=_FakeScopeStore())
    api.daemon_instance.login_helper = None

    out = api.neighbor_scopes()

    assert out["success"] is True
    assert out["served"]["scopes"] == ""


def test_served_scopes_survive_a_raising_formatter(monkeypatch):
    def _boom():
        raise RuntimeError("transport keys unreadable")

    api = _api_with_publisher(monkeypatch, None, method="GET", storage=_FakeScopeStore())
    api.daemon_instance.login_helper = SimpleNamespace(_format_region_names=_boom)

    out = api.neighbor_scopes()

    assert out["success"] is True
    assert out["served"]["scopes"] == ""
