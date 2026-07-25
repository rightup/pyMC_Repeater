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
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cherrypy
import pytest
from openhop_core.companion.models import Contact
from openhop_core.protocol import LocalIdentity, Packet
from openhop_core.protocol.constants import PH_TYPE_SHIFT, ROUTE_TYPE_FLOOD

from repeater.companion.bridge import (
    OutboundMessageEvent,
    RepeaterCompanionBridge,
    outbound_message_id,
    outbound_message_source,
)
from repeater.companion.correlation import (
    CompanionCorrelationTracker,
    injected_tx_outcome,
    outbound_send_capture,
)
from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.main import RepeaterDaemon
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
    def test_same_packet_is_correlated_for_every_companion(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        packet_hash = "ABCDEF0123456789"
        tracker.register_inbound(packet_hash, "0x01", message_id=11)
        tracker.register_inbound(packet_hash, "0x02", message_id=22)

        hits = tracker.observe_duplicate(_record(packet_hash=packet_hash))

        assert [(hit["companion_hash"], hit["message_id"]) for hit in hits] == [
            ("0x01", 11),
            ("0x02", 22),
        ]

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
        tracker.register_outbound(h, _HASH, message_id=1)

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

    def test_transient_outbound_hit_is_buffered_until_promotion(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        tracker.register_outbound(h, _HASH)
        assert tracker.observe_duplicate(_record(packet_hash=h, original_path=["11"])) == []
        assert tracker.observe_duplicate(_record(packet_hash=h, original_path=["22"])) == []

        buffered = tracker.promote_outbound(h, _HASH, message_id=42)

        assert len(buffered) == 1
        assert buffered[0]["message_id"] == 42
        assert buffered[0]["path"] == ["22"]
        assert buffered[0]["heard_repeat_count"] == 2
        assert buffered[0]["unique_repeater_count"] == 2

    def test_promotion_preserves_counts_ttl_and_one_tracker_entry(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        clock = [100.0]

        with patch(
            "repeater.companion.correlation.time.time",
            side_effect=lambda: clock[0],
        ):
            tracker.register_outbound(h, _HASH)
            clock[0] = 101.0
            assert (
                tracker.observe_duplicate(
                    _record(
                        packet_hash=h,
                        original_path=["11"],
                        ts=clock[0],
                    )
                )
                == []
            )

            clock[0] = 102.0
            buffered = tracker.promote_outbound(h, _HASH, message_id=42)
            assert buffered[0]["heard_repeat_count"] == 1
            assert len(tracker) == 1

            clock[0] = 103.0
            promoted = tracker.observe_duplicate(
                _record(
                    packet_hash=h,
                    original_path=["22"],
                    ts=clock[0],
                )
            )
            assert len(promoted) == 1
            assert promoted[0]["message_id"] == 42
            assert promoted[0]["heard_repeat_count"] == 2
            assert promoted[0]["unique_repeater_count"] == 2

            # Promotion must not restart the original registration's TTL.
            clock[0] = 401.0
            assert tracker.observe_duplicate(_record(packet_hash=h, ts=clock[0])) == []

    def test_empty_path_has_no_terminal_hash(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "1234567890ABCDEF"
        tracker.register_outbound(h, _HASH, message_id=1)
        hit = tracker.observe_duplicate(_record(packet_hash=h, original_path=[]))[0]
        assert hit["terminal_hash"] is None

    def test_same_companion_hash_ambiguity_fails_closed(self, caplog):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "9999999999999999"
        tracker.register_outbound(h, _HASH, message_id=1)
        tracker.register_outbound(h, _HASH, message_id=2)

        assert tracker.observe_duplicate(_record(packet_hash=h)) == []
        assert "Ambiguous companion RF correlation suppressed" in caplog.text

    def test_same_hash_still_fans_out_across_companions(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "8888888888888888"
        tracker.register_outbound(h, "0x01", message_id=1)
        tracker.register_outbound(h, "0x02", message_id=2)

        hits = tracker.observe_duplicate(_record(packet_hash=h))

        assert {(hit["companion_hash"], hit["message_id"]) for hit in hits} == {
            ("0x01", 1),
            ("0x02", 2),
        }

    def test_same_companion_mixed_direction_hash_fails_closed(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        h = "7777777777777777"
        tracker.register_inbound(h, _HASH, message_id=1)
        tracker.register_outbound(h, _HASH, message_id=2)

        assert tracker.observe_duplicate(_record(packet_hash=h)) == []


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
        assert (
            tracker.observe_duplicate(_record(packet_hash="3333333333333333"))[0]["message_id"] == 3
        )


# ===================================================================
# Journal event helpers
# ===================================================================


class TestJournalMessageReception:
    def test_record_message_reception_shapes_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        packet_hash = "ABCDEF0123456789"
        assert handler.companion_push_message(
            _HASH,
            {"packet_hash": packet_hash, "text": "stored"},
        )
        message_id = handler.companion_get_message_id(_HASH, packet_hash)

        correlation = {
            "message_id": message_id,
            "packet_hash": packet_hash,
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
            "message_id": message_id,
            "packet_hash": "ABCDEF0123456789",
            "path": ["11", "22"],
            "rssi": -80,
            "snr": 2.5,
            "observed_at": 123.0,
            "observation_count": 3,
            "unique_path_count": 2,
        }


class TestJournalRfReception:
    def test_record_rf_reception_shapes_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)

        record = _record(
            packet_hash="AABBCCDD11223344",
            original_path=["11", "22"],
            rssi=-88,
            snr=1.5,
            ts=999.0,
        )
        seq = journal.record_rf_reception(record)
        assert seq is not None

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        assert events[0]["event_type"] == "rf_reception"
        assert events[0]["packet_hash"] == "AABBCCDD11223344"
        assert events[0]["payload"] == {
            "packet_hash": "AABBCCDD11223344",
            "rssi": -88,
            "snr": 1.5,
            "path": ["11", "22"],
            "observed_at": 999.0,
        }

    def test_missing_path_defaults_to_empty_list(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        record = _record(packet_hash="1122334455667788", original_path=None)
        journal.record_rf_reception(record)
        events = handler.companion_get_events(_HASH, 0)
        assert events[0]["payload"]["path"] == []


class TestJournalSendState:
    def test_null_message_id_is_rejected_without_an_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)

        with pytest.raises(ValueError, match="durable message_id"):
            journal.record_send_state({"message_id": None})

        assert handler.companion_get_events(_HASH, 0) == []

    def test_record_send_state_shapes_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        stored = journal.store_outbound_message(
            {
                "packet_hash": "1234567890ABCDEF",
                "recipient_key": b"\x22" * 32,
                "text": "stored",
            },
            "frame",
            "transmitted",
        )

        correlation = {
            "message_id": stored["message_id"],
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

        events = [
            event
            for event in handler.companion_get_events(_HASH, 0)
            if event["event_type"] == "message_send_state"
        ]
        assert len(events) == 1
        assert events[0]["event_type"] == "message_send_state"
        data = events[0]["payload"]
        assert data["state"] == "heard_repeated"
        assert data["message_id"] == stored["message_id"]
        assert data["terminal_repeater_hash"] == "33"
        assert data["heard_repeat_count"] == 2
        assert data["unique_repeater_count"] == 1

    def test_durable_heard_repeat_advances_row_and_journals_rf_detail(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        stored = journal.store_outbound_message(
            {
                "packet_hash": "1234567890ABCDEF" + "00" * 24,
                "recipient_key": b"\x22" * 32,
                "text": "hello",
            },
            "frame",
            "transmitted",
        )
        correlation = {
            "message_id": stored["message_id"],
            "packet_hash": "1234567890ABCDEF",
            "path": ["11", "22"],
            "terminal_hash": "22",
            "rssi": -71,
            "snr": 2.5,
            "observed_at": 789.0,
            "heard_repeat_count": 1,
            "unique_repeater_count": 1,
        }

        result = journal.record_outbound_heard_repeat(correlation)

        assert result["message"]["state"] == "heard_repeated"
        # The 16-char observation key must not truncate durable send history.
        assert result["message"]["packet_hash"] == "1234567890ABCDEF" + "00" * 24
        events = handler.companion_get_events(_HASH, 0)
        send_events = [event for event in events if event["event_type"] == "message_send_state"]
        assert len(send_events) == 1
        assert send_events[0]["payload"] == {
            "message_id": stored["message_id"],
            "state": "heard_repeated",
            "packet_hash": "1234567890ABCDEF",
            "path": ["11", "22"],
            "terminal_repeater_hash": "22",
            "rssi": -71,
            "snr": 2.5,
            "observed_at": 789.0,
            "heard_repeat_count": 1,
            "unique_repeater_count": 1,
        }

    def test_heard_repeat_does_not_regress_confirmed_row(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        stored = journal.store_outbound_message(
            {"packet_hash": "1234567890ABCDEF", "text": "hello"},
            "rest",
            "transmitted",
        )
        journal.update_outbound_state(stored["message_id"], "confirmed")

        result = journal.record_outbound_heard_repeat(
            {
                "message_id": stored["message_id"],
                "packet_hash": "1234567890ABCDEF",
                "path": [],
                "heard_repeat_count": 1,
                "unique_repeater_count": 0,
            }
        )

        assert result["message"]["state"] == "confirmed"
        assert result["event"]["payload"]["state"] == "confirmed"
        assert result["event"]["payload"]["heard_repeat_count"] == 1


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
# rf_reception opt-in write gate (main.py: _companion_duplicate_observer)
# ===================================================================


def _daemon_with_observer_state(tmp_path, rf_reception_hashes=()):
    """Build a RepeaterDaemon with just enough state wired for
    ``_companion_duplicate_observer`` to run standalone: a real tracker + a
    real journal per companion, matching how main.py wires them at boot/hot-
    reload. ``rf_reception_hashes`` names which companion_hash(es) have
    opted in (design doc §9 write gate, settings.rf_reception_events)."""
    daemon = RepeaterDaemon({"repeater": {"node_name": "n"}, "logging": {}}, radio=object())
    sqlite_handler = SQLiteHandler(tmp_path)
    daemon.repeater_handler = SimpleNamespace(
        storage=SimpleNamespace(sqlite_handler=sqlite_handler)
    )
    daemon.correlation_tracker = CompanionCorrelationTracker(ttl_seconds=300)
    journal = CompanionEventJournal(sqlite_handler, _HASH)
    daemon.companion_journals[_HASH] = journal
    if _HASH in rf_reception_hashes:
        daemon._rf_reception_journals[_HASH] = journal
    return daemon, sqlite_handler, journal


@pytest.mark.asyncio
async def test_frame_history_promotion_yields_one_durable_heard_repeat(tmp_path):
    daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)

    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash=_HASH,
        tracker=daemon.correlation_tracker,
    )
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)
    event = OutboundMessageEvent(
        companion_hash=_HASH,
        packet_hash="AB" * 32,
        text="from frame",
        timestamp=10,
        is_channel=False,
        recipient_key=b"\x02" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=None,
        source="frame",
        message_id=None,
        result=True,
    )

    await bridge._record_outbound_message(event)

    stored = sqlite_handler.companion_outbound_message_get_by_hash(
        _HASH,
        event.packet_hash,
    )
    assert stored is not None
    assert len(daemon.correlation_tracker) == 1

    daemon._companion_duplicate_observer(
        _record(
            packet_hash=event.packet_hash[:16],
            original_path=["AA", "BB"],
            rssi=-77,
            snr=4.0,
            ts=1234.0,
        )
    )

    durable = sqlite_handler.companion_message_get_by_id(
        _HASH,
        stored["id"],
    )
    assert durable["state"] == "heard_repeated"
    events = sqlite_handler.companion_get_events(_HASH, 0)
    send_events = [item for item in events if item["event_type"] == "message_send_state"]
    assert len(send_events) == 1
    assert send_events[0]["payload"] == {
        "message_id": stored["id"],
        "state": "heard_repeated",
        "packet_hash": event.packet_hash[:16],
        "path": ["AA", "BB"],
        "terminal_repeater_hash": "BB",
        "rssi": -77,
        "snr": 4.0,
        "observed_at": 1234.0,
        "heard_repeat_count": 1,
        "unique_repeater_count": 1,
    }


@pytest.mark.asyncio
async def test_frame_operator_and_v1_sends_share_bridge_without_history_cross_attribution(
    tmp_path,
):
    """All three clients can interleave on one bridge/radio without aliasing."""

    daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
    injected_hashes = []

    async def _inject(packet, **_kwargs):
        # Yield inside the one shared radio boundary so the three caller
        # contexts overlap instead of merely running one after another.
        injected_hashes.append(packet.calculate_packet_hash().hex().upper())
        await asyncio.sleep(0)
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash=_HASH,
        tracker=daemon.correlation_tracker,
    )
    assert bridge.set_channel(1, "shared", bytes(32))
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)

    # v1 owns its durable row before RF; Frame and the legacy operator route
    # are persisted by the bridge observer after RF acceptance.
    v1_row = journal.store_outbound_message(
        {
            "text": "v1",
            "timestamp": 303,
            "is_channel": True,
            "channel_idx": 1,
        },
        "rest",
        "pending",
    )

    async def _send(source, text, timestamp, message_id=None):
        capture = {}
        source_context = outbound_message_source.set(source)
        row_context = outbound_message_id.set(message_id)
        capture_context = outbound_send_capture.set(capture)
        try:
            assert await bridge.send_channel_message(
                1,
                text,
                timestamp=timestamp,
            )
            return capture["hash"]
        finally:
            outbound_send_capture.reset(capture_context)
            outbound_message_id.reset(row_context)
            outbound_message_source.reset(source_context)

    frame_hash, operator_hash, v1_hash = await asyncio.gather(
        _send("frame", "frame", 101),
        _send("operator", "operator", 202),
        _send("rest", "v1", 303, v1_row["message_id"]),
    )
    assert len(injected_hashes) == 3
    assert len({frame_hash, operator_hash, v1_hash}) == 3

    journal.update_outbound_state(
        v1_row["message_id"],
        "transmitted",
        v1_hash,
    )
    messages = {
        message["text"]: message for message in sqlite_handler.companion_get_messages(_HASH)
    }
    assert {
        text: (messages[text]["source"], messages[text]["packet_hash"])
        for text in ("frame", "operator", "v1")
    } == {
        "frame": ("frame", frame_hash),
        "operator": ("operator", operator_hash),
        "v1": ("rest", v1_hash),
    }

    for terminal, (text, packet_hash) in enumerate(
        (
            ("frame", frame_hash),
            ("operator", operator_hash),
            ("v1", v1_hash),
        ),
        start=1,
    ):
        daemon._companion_duplicate_observer(
            _record(
                packet_hash=packet_hash[:16],
                original_path=[f"{terminal:02X}"],
                ts=1000.0 + terminal,
            )
        )
        messages[text] = sqlite_handler.companion_message_get_by_id(
            _HASH,
            messages[text]["id"],
        )

    assert {text: messages[text]["state"] for text in ("frame", "operator", "v1")} == {
        "frame": "heard_repeated",
        "operator": "heard_repeated",
        "v1": "heard_repeated",
    }
    heard_events = [
        event
        for event in sqlite_handler.companion_get_events(_HASH, 0)
        if event["event_type"] == "message_send_state"
        and event["payload"]["state"] == "heard_repeated"
    ]
    assert {event["payload"]["message_id"] for event in heard_events} == {
        messages["frame"]["id"],
        messages["operator"]["id"],
        messages["v1"]["id"],
    }


@pytest.mark.asyncio
async def test_outbound_repeat_during_blocked_store_drains_with_durable_id(
    tmp_path,
):
    daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)

    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash=_HASH,
        tracker=daemon.correlation_tracker,
    )
    store_started = threading.Event()
    release_store = threading.Event()
    real_store = journal.store_outbound_message

    def _blocked_store(*args):
        store_started.set()
        assert release_store.wait(timeout=2)
        return real_store(*args)

    journal.store_outbound_message = _blocked_store
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)
    event = OutboundMessageEvent(
        companion_hash=_HASH,
        packet_hash="BC" * 32,
        text="racing frame send",
        timestamp=10,
        is_channel=False,
        recipient_key=b"\x03" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=None,
        source="frame",
        message_id=None,
        result=True,
    )

    send_task = asyncio.create_task(bridge._record_outbound_message(event))
    try:
        assert await asyncio.to_thread(store_started.wait, 2)
        daemon._companion_duplicate_observer(
            _record(
                packet_hash=event.packet_hash[:16],
                original_path=["AA"],
                ts=321.0,
            )
        )
        daemon._companion_duplicate_observer(
            _record(
                packet_hash=event.packet_hash[:16],
                original_path=["BB"],
                ts=322.0,
            )
        )
        assert sqlite_handler.companion_get_events(_HASH, 0) == []
    finally:
        release_store.set()
    await send_task

    stored = sqlite_handler.companion_outbound_message_get_by_hash(
        _HASH,
        event.packet_hash,
    )
    assert stored["state"] == "heard_repeated"
    events = sqlite_handler.companion_get_events(_HASH, 0)
    assert [item["event_type"] for item in events] == [
        "message",
        "message_send_state",
    ]
    assert events[1]["payload"]["message_id"] == stored["id"]
    assert events[1]["payload"]["message_id"] is not None
    assert events[1]["payload"]["heard_repeat_count"] == 2
    assert events[1]["payload"]["unique_repeater_count"] == 2


@pytest.mark.asyncio
async def test_cancelled_outbound_history_waits_for_commit_and_promotes_tracker(
    tmp_path,
):
    daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)

    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash=_HASH,
        tracker=daemon.correlation_tracker,
    )
    store_started = threading.Event()
    release_store = threading.Event()
    real_store = journal.store_outbound_message

    def _blocked_store(*args):
        store_started.set()
        assert release_store.wait(timeout=2)
        return real_store(*args)

    journal.store_outbound_message = _blocked_store
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)
    token = daemon.correlation_tracker.new_registration_token()
    packet_hash = "D1" * 32
    daemon.correlation_tracker.register_outbound(
        packet_hash,
        _HASH,
        registration_token=token,
    )
    event = OutboundMessageEvent(
        companion_hash=_HASH,
        packet_hash=packet_hash,
        text="cancel after RF",
        timestamp=10,
        is_channel=True,
        recipient_key=None,
        channel_idx=1,
        txt_type=0,
        expected_ack=None,
        source="frame",
        message_id=None,
        result=True,
        correlation_token=token,
    )

    task = asyncio.create_task(bridge._record_outbound_message(event))
    assert await asyncio.to_thread(store_started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_store.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = sqlite_handler.companion_outbound_message_get_by_hash(
        _HASH,
        packet_hash,
    )
    assert stored is not None
    hit = daemon.correlation_tracker.observe_duplicate(
        _record(packet_hash=packet_hash[:16], original_path=["AA"])
    )
    assert len(hit) == 1
    assert hit[0]["message_id"] == stored["id"]


@pytest.mark.asyncio
async def test_operator_ack_timeout_retains_transmitted_history_and_correlation(
    tmp_path,
):
    daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)

    async def _accepted_then_timed_out(_packet, **_kwargs):
        injected_tx_outcome.get()["accepted"] = True
        return False

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _accepted_then_timed_out,
        companion_hash=_HASH,
        tracker=daemon.correlation_tracker,
    )
    peer_key = LocalIdentity().get_public_key()
    assert bridge.contacts.add(Contact(public_key=peer_key, name="peer", adv_type=1))
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)

    source_context = outbound_message_source.set("operator")
    try:
        result = await bridge.send_text_message(
            peer_key,
            "ACK timed out",
            wait_for_ack=True,
        )
    finally:
        outbound_message_source.reset(source_context)

    assert result.success is False
    messages = sqlite_handler.companion_get_messages(_HASH)
    assert len(messages) == 1
    assert messages[0]["source"] == "operator"
    assert messages[0]["state"] == "transmitted"
    hit = daemon.correlation_tracker.observe_duplicate(
        _record(
            packet_hash=messages[0]["packet_hash"][:16],
            original_path=["AA"],
        )
    )
    assert len(hit) == 1
    assert hit[0]["message_id"] == messages[0]["id"]


class TestRfReceptionWriteGate:
    def test_flag_off_by_default_no_rf_reception_journaled(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
        # Uncorrelated duplicate: no tracker registration at all.
        daemon._companion_duplicate_observer(_record(packet_hash="1111111111111111"))
        events = sqlite_handler.companion_get_events(_HASH, 0)
        assert events == []

    def test_flag_off_correlation_hits_still_work(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
        h = "2222222222222222"
        assert sqlite_handler.companion_push_message(
            _HASH,
            {"packet_hash": h, "text": "stored"},
        )
        message_id = sqlite_handler.companion_get_message_id(_HASH, h)
        daemon.correlation_tracker.register_inbound(
            h,
            _HASH,
            message_id=message_id,
        )

        daemon._companion_duplicate_observer(_record(packet_hash=h, original_path=["11"]))

        events = sqlite_handler.companion_get_events(_HASH, 0)
        assert [e["event_type"] for e in events] == ["message_reception"]

    def test_failed_correlation_commit_is_retried_before_pending_is_cleared(
        self,
        tmp_path,
    ):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
        stored = journal.store_inbound_message(
            {
                "packet_hash": "2323232323232323",
                "text": "retry evidence",
            },
            10,
        )
        daemon.correlation_tracker.register_inbound(
            "2323232323232323",
            _HASH,
            message_id=stored["message_id"],
        )
        real_record = journal.record_inbound_reception
        attempts = 0

        def _fail_once(hit):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary storage failure")
            return real_record(hit)

        journal.record_inbound_reception = _fail_once
        daemon._companion_duplicate_observer(
            _record(
                packet_hash="2323232323232323",
                original_path=["11"],
            )
        )
        daemon._companion_duplicate_observer(
            _record(
                packet_hash="2323232323232323",
                original_path=["22"],
            )
        )

        message = sqlite_handler.companion_message_get_by_id(
            _HASH,
            stored["message_id"],
        )
        assert attempts == 2
        assert message["observation_count"] == 3
        reception_events = [
            event
            for event in sqlite_handler.companion_get_events(_HASH, 0)
            if event["event_type"] == "message_reception"
        ]
        assert len(reception_events) == 1
        assert reception_events[0]["payload"]["observation_count"] == 3

    def test_transient_outbound_hit_does_not_emit_null_id_event(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
        h = "2121212121212121"
        daemon.correlation_tracker.register_outbound(h, _HASH)

        daemon._companion_duplicate_observer(_record(packet_hash=h, original_path=["11"]))

        events = sqlite_handler.companion_get_events(_HASH, 0)
        assert events == []

    def test_flag_on_uncorrelated_duplicate_journals_rf_reception(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(
            tmp_path, rf_reception_hashes=(_HASH,)
        )
        record = _record(packet_hash="3333333333333333", original_path=["aa", "bb"], rssi=-77)

        daemon._companion_duplicate_observer(record)

        events = sqlite_handler.companion_get_events(_HASH, 0)
        assert [e["event_type"] for e in events] == ["rf_reception"]
        assert events[0]["payload"]["packet_hash"] == "3333333333333333"
        assert events[0]["payload"]["rssi"] == -77
        assert events[0]["payload"]["path"] == ["aa", "bb"]

    def test_flag_on_correlated_duplicate_journals_both_events(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(
            tmp_path, rf_reception_hashes=(_HASH,)
        )
        h = "4444444444444444"
        assert sqlite_handler.companion_push_message(
            _HASH,
            {"packet_hash": h, "text": "stored"},
        )
        message_id = sqlite_handler.companion_get_message_id(_HASH, h)
        daemon.correlation_tracker.register_inbound(
            h,
            _HASH,
            message_id=message_id,
        )

        daemon._companion_duplicate_observer(_record(packet_hash=h, original_path=["cc"]))

        events = sqlite_handler.companion_get_events(_HASH, 0)
        event_types = {e["event_type"] for e in events}
        assert event_types == {"message_reception", "rf_reception"}

    def test_no_companions_opted_in_is_a_cheap_noop(self, tmp_path):
        daemon, sqlite_handler, journal = _daemon_with_observer_state(tmp_path)
        assert daemon._rf_reception_journals == {}
        # Should not raise and should not journal anything for an
        # uncorrelated duplicate.
        daemon._companion_duplicate_observer(_record(packet_hash="5555555555555555"))
        assert sqlite_handler.companion_get_events(_HASH, 0) == []


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
            self.removed = []

        def remove(self, entry):
            self.removed.append(entry)
            return True

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
        asyncio.run(fs._persist_companion_message(msg, object()))

        message_id = handler.companion_get_message_id(_HASH, msg["packet_hash"])
        assert message_id is not None

        hits = tracker.observe_duplicate(_record(packet_hash="AB" * 8))
        assert len(hits) == 1
        assert hits[0]["message_id"] == message_id

    @pytest.mark.asyncio
    async def test_duplicate_during_blocked_store_drains_with_durable_id(
        self,
        tmp_path,
    ):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server_with_tracker(handler, journal, tracker)
        daemon = RepeaterDaemon(
            {"repeater": {"node_name": "n"}, "logging": {}},
            radio=object(),
        )
        daemon.repeater_handler = SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler))
        daemon.correlation_tracker = tracker
        daemon.companion_journals[_HASH] = journal

        store_started = threading.Event()
        release_store = threading.Event()
        real_store = journal.store_inbound_message

        def _blocked_store(*args):
            store_started.set()
            assert release_store.wait(timeout=2)
            return real_store(*args)

        journal.store_inbound_message = _blocked_store
        msg = {
            "text": "racing receive",
            "packet_hash": "CD" * 8 + "00" * 24,
            "timestamp": 1,
        }
        persist_task = asyncio.create_task(fs._persist_companion_message(msg, object()))
        try:
            assert await asyncio.to_thread(store_started.wait, 2)
            daemon._companion_duplicate_observer(
                _record(
                    packet_hash=msg["packet_hash"][:16],
                    original_path=["11", "22"],
                    ts=654.0,
                )
            )
            daemon._companion_duplicate_observer(
                _record(
                    packet_hash=msg["packet_hash"][:16],
                    original_path=["33"],
                    ts=655.0,
                )
            )
            assert handler.companion_get_events(_HASH, 0) == []
        finally:
            release_store.set()
        await persist_task

        message_id = handler.companion_get_message_id(
            _HASH,
            msg["packet_hash"],
        )
        message = handler.companion_message_get_by_id(_HASH, message_id)
        assert message["observation_count"] == 3
        assert message["unique_path_count"] == 3
        events = handler.companion_get_events(_HASH, 0)
        assert [event["event_type"] for event in events] == [
            "message",
            "message_reception",
        ]
        assert events[1]["payload"]["message_id"] == message_id
        assert events[1]["payload"]["message_id"] is not None
        assert events[1]["payload"]["observation_count"] == 3
        assert events[1]["payload"]["unique_path_count"] == 3

    @pytest.mark.asyncio
    async def test_cancelled_inbound_store_finishes_commit_and_reconciliation(
        self,
        tmp_path,
    ):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server_with_tracker(handler, journal, tracker)
        store_started = threading.Event()
        release_store = threading.Event()
        real_store = journal.store_inbound_message

        def _blocked_store(*args):
            store_started.set()
            assert release_store.wait(timeout=2)
            return real_store(*args)

        journal.store_inbound_message = _blocked_store
        msg = {
            "text": "cancelled caller",
            "packet_hash": "CE" * 8,
            "timestamp": 1,
        }
        queue_entry = object()
        task = asyncio.create_task(fs._persist_companion_message(msg, queue_entry))
        assert await asyncio.to_thread(store_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_store.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        message_id = handler.companion_get_message_id(
            _HASH,
            msg["packet_hash"],
        )
        assert message_id is not None
        assert fs.bridge.message_queue.removed == [queue_entry]
        hit = tracker.observe_duplicate(
            _record(packet_hash=msg["packet_hash"], original_path=["AA"])
        )
        assert len(hit) == 1
        assert hit[0]["message_id"] == message_id

    def test_storage_failure_discards_provisional_registration(self, tmp_path):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        journal = MagicMock()
        journal.store_inbound_message.side_effect = RuntimeError("disk failed")
        fs = _frame_server_with_tracker(handler, journal, tracker)

        with pytest.raises(RuntimeError, match="disk failed"):
            asyncio.run(
                fs._persist_companion_message(
                    {
                        "text": "not stored",
                        "packet_hash": "DE" * 8,
                        "timestamp": 1,
                    },
                    object(),
                )
            )

        assert len(tracker) == 0

    def test_dedup_non_insert_rebinds_one_durable_entry_and_counts_reception(
        self,
        tmp_path,
    ):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server_with_tracker(handler, journal, tracker)
        msg = {
            "text": "one durable row",
            "packet_hash": "EF" * 8,
            "timestamp": 1,
        }

        asyncio.run(fs._persist_companion_message(msg, object()))
        assert len(tracker) == 1
        asyncio.run(fs._persist_companion_message(msg, object()))

        assert len(tracker) == 1
        messages = handler.companion_get_messages(_HASH)
        assert len(messages) == 1
        assert messages[0]["observation_count"] == 2
        hit = tracker.observe_duplicate(
            _record(packet_hash=msg["packet_hash"], original_path=["AA"])
        )
        assert len(hit) == 1
        assert hit[0]["message_id"] == messages[0]["id"]
        assert hit[0]["observation_count"] == 3

    def test_cold_dedup_rebind_seeds_counts_from_durable_history(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        msg = {
            "text": "survives restart",
            "packet_hash": "F1" * 8,
            "timestamp": 1,
        }
        stored = journal.store_inbound_message(msg, 10)
        handler.companion_record_inbound_reception(
            _HASH,
            stored["message_id"],
            {
                "packet_hash": msg["packet_hash"],
                "path": ["11"],
                "observation_count": 4,
                "unique_path_count": 3,
            },
        )

        # A new process has no hot tracker entry, but SQLite still deduplicates
        # the first post-restart reception to the existing logical row.
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        fs = _frame_server_with_tracker(handler, journal, tracker)
        asyncio.run(fs._persist_companion_message(msg, object()))

        message = handler.companion_message_get_by_id(
            _HASH,
            stored["message_id"],
        )
        assert message["observation_count"] == 5
        assert message["unique_path_count"] == 3
        assert len(tracker) == 1
        next_hit = tracker.observe_duplicate(
            _record(packet_hash=msg["packet_hash"], original_path=["22"])
        )
        assert len(next_hit) == 1
        assert next_hit[0]["message_id"] == stored["message_id"]
        assert next_hit[0]["observation_count"] == 6

    def test_missing_packet_hash_skips_registration(self, tmp_path):
        handler = _handler(tmp_path)
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        fs = _frame_server_with_tracker(handler, journal=None, tracker=tracker)

        asyncio.run(fs._persist_companion_message({"text": "hi", "timestamp": 1}, object()))
        assert len(tracker) == 0

    def test_no_tracker_configured_does_not_error(self, tmp_path):
        handler = _handler(tmp_path)
        fs = CompanionFrameServer.__new__(CompanionFrameServer)
        fs.sqlite_handler = handler
        fs.companion_hash = _HASH
        fs.journal = None

        class _FakeMessageQueue:
            max_size = 10

            def remove(self, _entry):
                return True

        fs.bridge = type("B", (), {"message_queue": _FakeMessageQueue()})()

        asyncio.run(
            fs._persist_companion_message(
                {"text": "hi", "packet_hash": "ph-x", "timestamp": 1}, object()
            )
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
    async def test_non_message_packet_is_not_registered_as_chat(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        bridge = RepeaterCompanionBridge(
            LocalIdentity(), _ok_injector, tracker=tracker, companion_hash=_HASH
        )
        pkt = _make_pkt(payload=b"\x01\x02\x03")
        expected_hash = pkt.calculate_packet_hash().hex().upper()[:16]

        sent = await bridge._send_packet(pkt)
        assert sent is True

        hits = tracker.observe_duplicate(_record(packet_hash=expected_hash))
        assert hits == []

    @pytest.mark.asyncio
    async def test_semantic_message_event_registers_outbound_with_tracker(self):
        tracker = CompanionCorrelationTracker(ttl_seconds=300)
        bridge = RepeaterCompanionBridge(
            LocalIdentity(), _ok_injector, tracker=tracker, companion_hash=_HASH
        )
        event = OutboundMessageEvent(
            companion_hash=_HASH,
            packet_hash="AB" * 32,
            text="hello",
            timestamp=1,
            is_channel=True,
            recipient_key=None,
            channel_idx=1,
            txt_type=0,
            expected_ack=None,
            source="frame",
            message_id=41,
            result=True,
        )

        await bridge._record_outbound_message(event)

        hits = tracker.observe_duplicate(_record(packet_hash=event.packet_hash[:16]))
        assert len(hits) == 1
        assert hits[0]["direction"] == "out"
        assert hits[0]["message_id"] == 41

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
        cherrypy.serving.request.user = {
            "username": "adam",
            "auth_type": "jwt",
            "scope": "admin",
        }
        cherrypy.serving.response.headers = {}
        cherrypy.serving.response.status = None
        yield
        cherrypy.serving.response.status = None

    def test_successful_send_response_includes_truncated_packet_hash(self, tmp_path):
        from types import SimpleNamespace

        from repeater.companion.journal import CompanionEventJournal
        from repeater.web.mobile_endpoints import CompanionsV1

        handler = _handler(tmp_path)
        full_hash = "AABBCCDDEE112233" + "00" * 24

        class _FakeIdentity:
            def get_public_key(self):
                return bytes([0x01]) + b"\x22" * 31

        class _FakeBridge:
            def get_public_key(self):
                return bytes([0x01]) + b"\x22" * 31

            async def send_text_message(
                self,
                pub_key,
                text,
                txt_type=0,
                wait_for_ack=False,
            ):
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
            companion_journals={"0x01": CompanionEventJournal(handler, "0x01")},
            repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
        )
        endpoints = CompanionsV1(daemon_instance=daemon, config={}, event_loop=object())
        endpoints._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
        endpoints._get_json_body = lambda: {"to": "aa" * 32, "text": "hello"}

        result = endpoints.messages.__wrapped__(endpoints, companion_name="comp-test")
        assert result["success"] is True
        assert result["data"]["packet_hash"] == full_hash[:16]
