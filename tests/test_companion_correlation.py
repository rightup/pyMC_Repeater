"""Tests for the Mobile Companion API live RF correlation hook (phase 3).

Covers ``repeater.companion.correlation.CompanionCorrelationTracker`` (the
in-memory TTL map), its wiring into ``RepeaterHandler``'s duplicate-record
call sites (local echo exclusion, both hook sites firing), the journal event
helpers, the two new SQLite methods (companion_get_message_id,
companion_update_message_observations), inbound registration from
frame_server, outbound registration + packet_hash surfacing from
RepeaterCompanionBridge._send_packet, and the endpoint-level
``outbound_send_capture`` contextvar plumbing. See design doc
docs/architecture/mobile-companion-api.md §9, §10.2-§10.4.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import cherrypy
import pytest
from openhop_core.protocol import LocalIdentity, Packet
from openhop_core.protocol.constants import PH_TYPE_SHIFT, ROUTE_TYPE_FLOOD

from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.correlation import CompanionCorrelationTracker, outbound_send_capture
from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from tests.test_engine import _inject_from_wire, _make_config, _make_dispatcher

_HASH = "0x01"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


def _make_pkt(payload: bytes = b"\x01\x02\x03", path: bytes = b"") -> Packet:
    pkt = Packet()
    pkt.header = ROUTE_TYPE_FLOOD | (0x01 << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    return pkt


def _record(packet_hash="ABCDEF0123456789", original_path=None, rssi=-70, snr=3.0, ts=None):
    return {
        "packet_hash": packet_hash,
        "original_path": original_path or [],
        "rssi": rssi,
        "snr": snr,
        "timestamp": ts if ts is not None else time.time(),
        "is_duplicate": True,
        "transmitted": False,
    }


# ===================================================================
# CompanionCorrelationTracker
# ===================================================================


class TestTrackerHashHandling:
    def test_truncates_64_char_registration_to_16_char_key(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        full_hash = "ABCDEF0123456789" + "00" * 24  # 64 hex chars
        tracker.register_inbound(full_hash, _HASH, message_id=5)

        hits = tracker.observe_duplicate(_record(packet_hash="ABCDEF0123456789"))
        assert len(hits) == 1
        assert hits[0]["message_id"] == 5

    def test_lowercase_and_bytes_hash_normalize_the_same(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        tracker.register_inbound(b"\xab\xcd\xef", _HASH, message_id=1)
        hits = tracker.observe_duplicate(_record(packet_hash="ABCDEF"))
        assert len(hits) == 1

    def test_none_or_empty_hash_registration_is_a_noop(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        tracker.register_inbound(None, _HASH, message_id=1)
        tracker.register_inbound("", _HASH, message_id=1)
        tracker.register_outbound(None, _HASH)
        assert len(tracker) == 0


class TestTrackerMiss:
    def test_miss_is_a_cheap_noop_returning_empty_list(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        assert tracker.observe_duplicate(_record(packet_hash="DEADBEEF00000000")) == []

    def test_missing_packet_hash_on_record_is_a_noop(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        tracker.register_inbound("ABCDEF0123456789", _HASH, message_id=1)
        record = _record()
        record["packet_hash"] = None
        assert tracker.observe_duplicate(record) == []


class TestTrackerInboundCorrelation:
    def test_running_counts_across_repeated_observations(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "ABCDEF0123456789"
        tracker.register_inbound(h, _HASH, message_id=42)

        hit1 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["11", "22"]))[0]
        assert hit1["direction"] == "in"
        assert hit1["message_id"] == 42
        assert hit1["observation_count"] == 2
        assert hit1["unique_path_count"] == 2

        # Same path again: observation_count grows, unique_path_count does not.
        hit2 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["11", "22"]))[0]
        assert hit2["observation_count"] == 3
        assert hit2["unique_path_count"] == 2

        # A genuinely different path: both grow.
        hit3 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["33"]))[0]
        assert hit3["observation_count"] == 4
        assert hit3["unique_path_count"] == 3

    def test_hit_carries_rssi_snr_and_path_through(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "ABCDEF0123456789"
        tracker.register_inbound(h, _HASH, message_id=1)
        hit = tracker.observe_duplicate(
            _record(packet_hash=h, original_path=["AA", "BB"], rssi=-91, snr=1.25)
        )[0]
        assert hit["path"] == ["AA", "BB"]
        assert hit["rssi"] == -91
        assert hit["snr"] == 1.25
        assert hit["companion_hash"] == _HASH


class TestTrackerOutboundCorrelation:
    def test_heard_repeat_count_vs_unique_repeater_count(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        tracker.register_outbound(h, _HASH)

        # Same terminal repeater heard twice: heard_repeat_count counts both,
        # unique_repeater_count counts the repeater once.
        hit1 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["11", "22"]))[0]
        assert hit1["direction"] == "out"
        assert hit1["terminal_hash"] == "22"
        assert hit1["heard_repeat_count"] == 1
        assert hit1["unique_repeater_count"] == 1

        hit2 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["33", "22"]))[0]
        assert hit2["heard_repeat_count"] == 2
        assert hit2["unique_repeater_count"] == 1

        # A different terminal repeater: both grow.
        hit3 = tracker.observe_duplicate(_record(packet_hash=h, original_path=["44"]))[0]
        assert hit3["heard_repeat_count"] == 3
        assert hit3["unique_repeater_count"] == 2

    def test_outbound_message_id_is_always_none(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        tracker.register_outbound(h, _HASH)
        hit = tracker.observe_duplicate(_record(packet_hash=h, original_path=["11"]))[0]
        assert hit["message_id"] is None

    def test_empty_path_has_no_terminal_hash(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        tracker.register_outbound(h, _HASH)
        hit = tracker.observe_duplicate(_record(packet_hash=h, original_path=[]))[0]
        assert hit["terminal_hash"] is None


class TestTrackerTTLAndEviction:
    def test_expired_entry_is_pruned_and_treated_as_a_miss(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=1)
        h = "ABCDEF0123456789"
        tracker.register_inbound(h, _HASH, message_id=1)
        with patch("repeater.companion.correlation.time.time", return_value=time.time() + 10):
            assert tracker.observe_duplicate(_record(packet_hash=h)) == []

    def test_max_size_eviction_drops_oldest_registration(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300, max_size=2)
        tracker.register_inbound("1111111111111111", _HASH, message_id=1)
        tracker.register_inbound("2222222222222222", _HASH, message_id=2)
        tracker.register_inbound("3333333333333333", _HASH, message_id=3)

        assert len(tracker) == 2
        assert tracker.observe_duplicate(_record(packet_hash="1111111111111111")) == []
        assert tracker.observe_duplicate(_record(packet_hash="3333333333333333"))[0][
            "message_id"
        ] == 3


# ===================================================================
# Journal event helpers
# ===================================================================


class TestJournalMessageReception:
    def test_record_message_reception_shapes_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)

        correlation = {
            "message_id": 7,
            "packet_hash": "ABCDEF0123456789",
            "path": ["11", "22"],
            "rssi": -80,
            "snr": 2.5,
            "observed_at": 123.0,
            "observation_count": 3,
            "unique_path_count": 2,
        }
        seq = journal.record_message_reception(correlation)
        assert seq is not None

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        assert events[0]["event_type"] == "message_reception"
        assert events[0]["packet_hash"] == "ABCDEF0123456789"
        data = events[0]["payload"]
        assert data == {
            "message_id": 7,
            "packet_hash": "ABCDEF0123456789",
            "path": ["11", "22"],
            "rssi": -80,
            "snr": 2.5,
            "observed_at": 123.0,
            "observation_count": 3,
            "unique_path_count": 2,
        }


class TestJournalSendState:
    def test_record_send_state_shapes_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)

        correlation = {
            "message_id": None,
            "packet_hash": "1234567890ABCDEF",
            "path": ["33"],
            "terminal_hash": "33",
            "rssi": -65,
            "snr": 5.0,
            "observed_at": 456.0,
            "heard_repeat_count": 2,
            "unique_repeater_count": 1,
        }
        seq = journal.record_send_state(correlation)
        assert seq is not None

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        assert events[0]["event_type"] == "message_send_state"
        data = events[0]["payload"]
        assert data["state"] == "heard_repeated"
        assert data["message_id"] is None
        assert data["terminal_repeater_hash"] == "33"
        assert data["heard_repeat_count"] == 2
        assert data["unique_repeater_count"] == 1


# ===================================================================
# SQLite counter persistence
# ===================================================================


class TestCounterPersistence:
    def test_new_push_message_rows_default_to_one_and_one(self, tmp_path):
        handler = _handler(tmp_path)
        assert handler.companion_push_message(
            _HASH, {"text": "hi", "timestamp": 1, "packet_hash": "ph-a"}
        )
        rows = handler.companion_get_messages(_HASH)
        assert rows[0]["observation_count"] == 1
        assert rows[0]["unique_path_count"] == 1

    def test_update_message_observations_round_trips(self, tmp_path):
        handler = _handler(tmp_path)
        assert handler.companion_push_message(
            _HASH, {"text": "hi", "timestamp": 1, "packet_hash": "ph-b"}
        )
        message_id = handler.companion_get_message_id(_HASH, "ph-b")
        assert message_id is not None

        assert handler.companion_update_message_observations(message_id, 4, 3)
        rows = handler.companion_get_messages(_HASH)
        assert rows[0]["observation_count"] == 4
        assert rows[0]["unique_path_count"] == 3

    def test_get_message_id_returns_none_for_unknown_hash(self, tmp_path):
        handler = _handler(tmp_path)
        assert handler.companion_get_message_id(_HASH, "does-not-exist") is None


# ===================================================================
# Engine hook wiring
# ===================================================================


def _make_handler_with_observer(observer):
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        from repeater.engine import RepeaterHandler

        return RepeaterHandler(
            _make_config(),
            _make_dispatcher(),
            0xAB,
            local_hash_bytes=bytes([0xAB]),
            duplicate_observer=observer,
        )


@pytest.mark.asyncio
class TestEngineDuplicateObserverHook:
    async def test_record_duplicate_fires_observer(self):
        calls = []
        handler = _make_handler_with_observer(calls.append)
        pkt = _make_pkt(payload=b"\x10\x20\x30", path=b"\x11")

        handler.record_duplicate(pkt, rssi=-90, snr=1.5)

        assert len(calls) == 1
        assert calls[0]["is_duplicate"] is True
        assert calls[0]["transmitted"] is False

    async def test_call_dupe_branch_fires_observer(self):
        calls = []
        handler = _make_handler_with_observer(calls.append)
        handler.airtime_mgr.calculate_airtime = MagicMock(return_value=20.0)
        handler.airtime_mgr.can_transmit = MagicMock(return_value=(True, 0.0))
        handler.airtime_mgr.record_tx = MagicMock()
        handler.airtime_mgr.record_rx = MagicMock()

        incoming = _make_pkt(payload=b"\x99\x88\x77", path=b"\x01")
        pkt1 = _inject_from_wire(incoming)
        pkt2 = _inject_from_wire(incoming)

        with (
            patch.object(handler, "_calculate_tx_delay", return_value=0.0),
            patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock),
        ):
            await handler(pkt1, {"snr": 6.0, "rssi": -75}, local_transmission=False)
            await handler(pkt2, {"snr": 5.5, "rssi": -76}, local_transmission=False)

        # First call: novel packet, not a duplicate -> observer not called.
        # Second call: genuine duplicate -> observer called exactly once.
        assert len(calls) == 1
        assert calls[0]["is_duplicate"] is True
        assert calls[0]["transmitted"] is False

    async def test_local_transmission_never_fires_observer(self):
        calls = []
        handler = _make_handler_with_observer(calls.append)
        handler.airtime_mgr.calculate_airtime = MagicMock(return_value=20.0)
        handler.airtime_mgr.can_transmit = MagicMock(return_value=(True, 0.0))
        handler.airtime_mgr.record_tx = MagicMock()
        handler.airtime_mgr.record_rx = MagicMock()

        pkt = _make_pkt(payload=b"\x01\x02\x03\x04", path=b"")
        with patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock):
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=True)

        assert calls == []

    async def test_first_reception_is_not_a_duplicate_and_does_not_fire(self):
        calls = []
        handler = _make_handler_with_observer(calls.append)
        handler.airtime_mgr.calculate_airtime = MagicMock(return_value=20.0)
        handler.airtime_mgr.can_transmit = MagicMock(return_value=(True, 0.0))
        handler.airtime_mgr.record_tx = MagicMock()
        handler.airtime_mgr.record_rx = MagicMock()

        pkt = _inject_from_wire(_make_pkt(payload=b"\xaa\xbb\xcc", path=b"\x02"))
        with patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock):
            await handler(pkt, {"snr": 1.0, "rssi": -80}, local_transmission=False)

        assert calls == []

    async def test_observer_exception_does_not_break_packet_handling(self):
        def _boom(_record):
            raise RuntimeError("boom")

        handler = _make_handler_with_observer(_boom)
        pkt = _make_pkt(payload=b"\x10\x20\x30", path=b"\x11")

        # Must not raise even though the observer blows up.
        handler.record_duplicate(pkt, rssi=-90, snr=1.5)

    async def test_no_observer_configured_is_inert(self):
        handler = _make_handler_with_observer(None)
        pkt = _make_pkt(payload=b"\x10\x20\x30", path=b"\x11")
        handler.record_duplicate(pkt, rssi=-90, snr=1.5)  # must not raise


# ===================================================================
# Frame server: inbound registration
# ===================================================================


def _frame_server_with_tracker(handler, journal, tracker):
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    fs.journal = journal
    fs.tracker = tracker

    class _FakeMessageQueue:
        def __init__(self):
            self.max_size = 10
            self.popped = 0

        def pop_last(self):
            self.popped += 1

    class _FakeBridge:
        def __init__(self):
            self.message_queue = _FakeMessageQueue()

    fs.bridge = _FakeBridge()
    return fs


class TestFrameServerInboundRegistration:
    def test_persist_registers_with_tracker_using_row_id(self, tmp_path):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        fs = _frame_server_with_tracker(handler, journal=None, tracker=tracker)

        msg = {"text": "hi", "packet_hash": "AB" * 8 + "00" * 24, "timestamp": 1}
        asyncio.run(fs._persist_companion_message(msg))

        message_id = handler.companion_get_message_id(_HASH, msg["packet_hash"])
        assert message_id is not None

        hits = tracker.observe_duplicate(_record(packet_hash="AB" * 8))
        assert len(hits) == 1
        assert hits[0]["message_id"] == message_id

    def test_missing_packet_hash_skips_registration(self, tmp_path):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        fs = _frame_server_with_tracker(handler, journal=None, tracker=tracker)

        asyncio.run(fs._persist_companion_message({"text": "hi", "timestamp": 1}))
        assert len(tracker) == 0

    def test_no_tracker_configured_does_not_error(self, tmp_path):
        handler = _handler(tmp_path)
        fs = CompanionFrameServer.__new__(CompanionFrameServer)
        fs.sqlite_handler = handler
        fs.companion_hash = _HASH
        fs.journal = None

        class _FakeMessageQueue:
            max_size = 10

            def pop_last(self):
                pass

        fs.bridge = type("B", (), {"message_queue": _FakeMessageQueue()})()

        asyncio.run(
            fs._persist_companion_message({"text": "hi", "packet_hash": "ph-x", "timestamp": 1})
        )


# ===================================================================
# Bridge: outbound registration + contextvar hash surfacing
# ===================================================================


async def _ok_injector(pkt, wait_for_ack=False, expected_crc=None):
    return True


async def _fail_injector(pkt, wait_for_ack=False, expected_crc=None):
    return False


class TestBridgeOutboundRegistration:
    @pytest.mark.asyncio
    async def test_send_packet_registers_outbound_with_tracker(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        bridge = RepeaterCompanionBridge(
            LocalIdentity(), _ok_injector, tracker=tracker, companion_hash=_HASH
        )
        pkt = _make_pkt(payload=b"\x01\x02\x03")
        expected_hash = pkt.calculate_packet_hash().hex().upper()[:16]

        sent = await bridge._send_packet(pkt)
        assert sent is True

        hits = tracker.observe_duplicate(_record(packet_hash=expected_hash))
        assert len(hits) == 1
        assert hits[0]["direction"] == "out"

    @pytest.mark.asyncio
    async def test_failed_send_does_not_register(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        bridge = RepeaterCompanionBridge(LocalIdentity(), _fail_injector, tracker=tracker)
        pkt = _make_pkt(payload=b"\x0a\x0b\x0c")

        sent = await bridge._send_packet(pkt)
        assert sent is False
        assert len(tracker) == 0

    @pytest.mark.asyncio
    async def test_no_tracker_is_inert(self):
        bridge = RepeaterCompanionBridge(LocalIdentity(), _ok_injector)
        pkt = _make_pkt(payload=b"\x0a\x0b\x0c")
        assert await bridge._send_packet(pkt) is True

    @pytest.mark.asyncio
    async def test_send_packet_publishes_hash_into_context_holder(self):
        bridge = RepeaterCompanionBridge(LocalIdentity(), _ok_injector)
        pkt = _make_pkt(payload=b"\x0d\x0e\x0f")
        expected_full_hash = pkt.calculate_packet_hash().hex().upper()

        holder = {}
        token = outbound_send_capture.set(holder)
        try:
            await bridge._send_packet(pkt)
        finally:
            outbound_send_capture.reset(token)

        assert holder["hash"] == expected_full_hash

    @pytest.mark.asyncio
    async def test_failed_send_does_not_publish_hash(self):
        bridge = RepeaterCompanionBridge(LocalIdentity(), _fail_injector)
        pkt = _make_pkt(payload=b"\x0d\x0e\x0f")

        holder = {}
        token = outbound_send_capture.set(holder)
        try:
            await bridge._send_packet(pkt)
        finally:
            outbound_send_capture.reset(token)

        assert "hash" not in holder

    @pytest.mark.asyncio
    async def test_no_holder_set_does_not_error(self):
        # No outbound_send_capture set in this context at all (default None).
        bridge = RepeaterCompanionBridge(LocalIdentity(), _ok_injector)
        pkt = _make_pkt(payload=b"\x0d\x0e\x0f")
        assert await bridge._send_packet(pkt) is True


class TestContextvarConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_sends_do_not_cross_contaminate_holders(self):
        bridge = RepeaterCompanionBridge(LocalIdentity(), _ok_injector)

        async def _send_with_own_holder(payload: bytes):
            holder: dict = {}
            token = outbound_send_capture.set(holder)
            try:
                pkt = _make_pkt(payload=payload)
                await bridge._send_packet(pkt)
                return pkt.calculate_packet_hash().hex().upper(), holder.get("hash")
            finally:
                outbound_send_capture.reset(token)

        (expected_a, got_a), (expected_b, got_b) = await asyncio.gather(
            _send_with_own_holder(b"\x01\x01\x01"),
            _send_with_own_holder(b"\x02\x02\x02"),
        )

        assert got_a == expected_a
        assert got_b == expected_b
        assert got_a != got_b


# ===================================================================
# Endpoint: successful send response carries packet_hash
# ===================================================================


class TestSendMessageSurfacesPacketHash:
    @pytest.fixture(autouse=True)
    def request_context(self):
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {"Idempotency-Key": "idem-corr-1"}
        cherrypy.serving.request.params = {}
        cherrypy.serving.request.user = {"username": "adam", "auth_type": "jwt"}
        cherrypy.serving.response.headers = {}
        cherrypy.serving.response.status = None
        yield
        cherrypy.serving.response.status = None

    def test_successful_send_response_includes_truncated_packet_hash(self, tmp_path):
        from types import SimpleNamespace

        from repeater.web.mobile_endpoints import CompanionsV1

        handler = _handler(tmp_path)
        full_hash = "AABBCCDDEE112233" + "00" * 24

        class _FakeIdentity:
            def get_public_key(self):
                return bytes([0x01]) + b"\x22" * 31

        class _FakeBridge:
            def get_public_key(self):
                return bytes([0x01]) + b"\x22" * 31

            async def send_text_message(self, pub_key, text, txt_type=0):
                from openhop_core.companion.models import SentResult

                # Simulate what RepeaterCompanionBridge._send_packet does:
                # publish the hash into whatever holder is set in context.
                holder = outbound_send_capture.get()
                if holder is not None:
                    holder["hash"] = full_hash
                return SentResult(success=True, is_flood=False, expected_ack=1)

        identity_manager = SimpleNamespace(
            get_identities_by_type=lambda t: (
                [("comp-test", _FakeIdentity(), {})] if t == "companion" else []
            )
        )
        daemon = SimpleNamespace(
            identity_manager=identity_manager,
            companion_bridges={0x01: _FakeBridge()},
            repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
        )
        endpoints = CompanionsV1(daemon_instance=daemon, config={}, event_loop=object())
        endpoints._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
        endpoints._get_json_body = lambda: {"to": "aa" * 32, "text": "hello"}

        result = endpoints.messages.__wrapped__(endpoints, companion_name="comp-test")
        assert result["success"] is True
        assert result["data"]["packet_hash"] == full_hash[:16]
