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
    STATUS_RESPONDED,
    STATUS_SEND_FAILED,
    STATUS_TIMEOUT,
    NeighborScopeHelper,
    NeighborSnapshot,
)
from repeater.neighbors_publisher import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
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
    assert payload["self"] == {"scopes": "DEN,APRS"}
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
    assert payload["self"] == {"scopes": "DEN,APRS"}
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
