"""Tests for the Mobile Companion API event journal writer (phase 1).

Covers ``repeater.companion.journal.CompanionEventJournal`` (append helpers,
JSON-safe payload shaping, listener hook for the future SSE phase) and its
wiring into ``CompanionFrameServer._persist_companion_message`` /
``_persist_contact``. See docs/architecture/mobile-companion-api.md §5
(journal) and §9 (event schema).
"""

from __future__ import annotations

import asyncio

import pytest

from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


def _journal(tmp_path):
    return CompanionEventJournal(_handler(tmp_path), _HASH)


# --- Append helpers ------------------------------------------------------


class TestRecordMessage:
    def test_appends_event_with_packet_hash_and_json_safe_payload(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)

        msg = {
            "sender_key": b"\x11" * 32,
            "text": "hello world",
            "timestamp": 1000,
            "packet_hash": "abc123",
            "is_channel": False,
        }
        seq = journal.record_message(msg)
        assert seq is not None

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        event = events[0]
        assert event["seq"] == seq
        assert event["event_type"] == "message"
        assert event["packet_hash"] == "abc123"
        # bytes fields must round-trip as hex strings (JSON-safe payload).
        assert event["payload"]["sender_key"] == "11" * 32
        assert event["payload"]["text"] == "hello world"

    def test_no_packet_hash_when_absent(self, tmp_path):
        journal = _journal(tmp_path)
        seq = journal.record_message({"text": "no hash"})
        events = journal.sqlite_handler.companion_get_events(_HASH, 0)
        assert events[0]["seq"] == seq
        assert events[0]["packet_hash"] is None


class TestRecordContact:
    def test_appends_event_with_change_and_json_safe_payload(self, tmp_path):
        journal = _journal(tmp_path)
        contact = {
            "pubkey": b"\xaa" * 32,
            "name": "Alice",
            "out_path": b"\x01\x02",
        }
        seq = journal.record_contact(contact, change="new")

        events = journal.sqlite_handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        event = events[0]
        assert event["seq"] == seq
        assert event["event_type"] == "contact"
        assert event["payload"]["pubkey"] == "aa" * 32
        assert event["payload"]["out_path"] == "0102"
        assert event["payload"]["change"] == "new"

    def test_default_change_is_update(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_contact({"pubkey": b"\xbb" * 32, "name": "Bob"})
        events = journal.sqlite_handler.companion_get_events(_HASH, 0)
        assert events[0]["payload"]["change"] == "update"


class TestRecordPrefs:
    def test_appends_event_with_json_safe_payload(self, tmp_path):
        journal = _journal(tmp_path)
        seq = journal.record_prefs({"node_name": "new-name", "flag": True})

        events = journal.sqlite_handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        event = events[0]
        assert event["seq"] == seq
        assert event["event_type"] == "prefs"
        assert event["payload"] == {"node_name": "new-name", "flag": True}


# --- Listeners -------------------------------------------------------------


class TestListeners:
    def test_listener_fires_with_seq_on_append(self, tmp_path):
        journal = _journal(tmp_path)
        received = []
        journal.register_listener(received.append)

        seq = journal.record_message({"text": "hi", "packet_hash": "ph-listen"})

        assert len(received) == 1
        assert received[0]["seq"] == seq
        assert received[0]["event_type"] == "message"
        assert "created_at" in received[0]
        assert "payload" in received[0]
        # SSE phase (§8): the notified event carries packet_hash and the
        # exact created_at written to the DB row, so a listener-fed stream
        # can emit the same wire object sync/snapshot would for this seq.
        assert received[0]["packet_hash"] == "ph-listen"
        stored = journal.sqlite_handler.companion_get_events(_HASH, 0)
        assert stored[0]["created_at"] == received[0]["created_at"]

    def test_listener_packet_hash_none_when_absent(self, tmp_path):
        journal = _journal(tmp_path)
        received = []
        journal.register_listener(received.append)

        journal.record_prefs({"node_name": "x"})

        assert received[0]["packet_hash"] is None

    def test_registering_the_same_listener_twice_notifies_it_once(self, tmp_path):
        journal = _journal(tmp_path)
        received = []
        journal.register_listener(received.append)
        journal.register_listener(received.append)

        journal.record_message({"text": "once"})

        assert len(received) == 1

    def test_unregister_listener_stops_future_notifications(self, tmp_path):
        journal = _journal(tmp_path)
        received = []
        journal.register_listener(received.append)

        journal.record_message({"text": "one"})
        journal.unregister_listener(received.append)
        journal.record_message({"text": "two"})

        assert len(received) == 1

    def test_unregister_unknown_listener_is_a_noop(self, tmp_path):
        journal = _journal(tmp_path)

        def _never_registered(event):
            pass

        journal.unregister_listener(_never_registered)  # must not raise

    def test_raising_listener_does_not_break_append_or_other_listeners(self, tmp_path):
        journal = _journal(tmp_path)
        received = []

        def _bad_listener(event):
            raise RuntimeError("boom")

        journal.register_listener(_bad_listener)
        journal.register_listener(received.append)

        seq = journal.record_message({"text": "hi"})

        assert seq is not None
        assert len(received) == 1
        assert received[0]["seq"] == seq

    def test_no_listeners_registered_does_not_error(self, tmp_path):
        journal = _journal(tmp_path)
        seq = journal.record_message({"text": "hi"})
        assert seq is not None


# --- Epoch -------------------------------------------------------------


class TestEpoch:
    def test_epoch_is_stable_across_calls(self, tmp_path):
        journal = _journal(tmp_path)
        e1 = journal.epoch
        e2 = journal.epoch
        assert e1 == e2
        assert isinstance(e1, str)
        assert e1 != ""

    def test_epoch_matches_handler_directly(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        assert journal.epoch == handler.companion_journal_epoch()


# --- Frame-server integration -----------------------------------------------


def _frame_server(handler, journal):
    """Minimal CompanionFrameServer, mirroring the fixture pattern used in
    tests/test_companion_bridge_frame_utils.py and
    tests/test_companion_settings.py (``__new__`` to skip the base-class
    ``__init__``, hand-set the attributes the persistence hooks touch).
    """
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    fs.journal = journal

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


class TestFrameServerMessageJournaling:
    def test_persist_companion_message_appends_journal_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server(handler, journal)

        msg = {"text": "hello", "packet_hash": "ph-1", "timestamp": 1}
        queue_entry = object()
        asyncio.run(fs._persist_companion_message(msg, queue_entry))

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        assert events[0]["event_type"] == "message"
        assert events[0]["packet_hash"] == "ph-1"
        assert fs.bridge.message_queue.removed == [queue_entry]

    def test_duplicate_push_does_not_journal_a_second_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server(handler, journal)

        msg = {"text": "hello", "packet_hash": "dup-hash", "timestamp": 1}
        first_entry = object()
        second_entry = object()
        asyncio.run(fs._persist_companion_message(msg, first_entry))
        # Same packet_hash: atomic storage deduplicates it, so the message must
        # not be journaled again.
        asyncio.run(fs._persist_companion_message(msg, second_entry))

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        # Both in-memory copies are removed: the second is already durable and
        # must not be delivered to the frame client twice.
        assert fs.bridge.message_queue.removed == [first_entry, second_entry]

    def test_no_journal_configured_does_not_error(self, tmp_path):
        handler = _handler(tmp_path)
        fs = _frame_server(handler, journal=None)

        queue_entry = object()
        asyncio.run(
            fs._persist_companion_message(
                {"text": "hello", "packet_hash": "ph-2"}, queue_entry
            )
        )

        assert fs.bridge.message_queue.removed == [queue_entry]


class TestFrameServerContactJournaling:
    def test_persist_contact_appends_journal_event(self, tmp_path):
        handler = _handler(tmp_path)
        journal = CompanionEventJournal(handler, _HASH)
        fs = _frame_server(handler, journal)

        class _FakeContact:
            public_key = b"\xcc" * 32
            name = "Carol"
            adv_type = 1
            flags = 0
            out_path_len = 0
            out_path = b""
            last_advert_timestamp = 0
            last_advert_packet = None
            lastmod = 0
            gps_lat = None
            gps_lon = None
            sync_since = 0

        asyncio.run(fs._persist_contact(_FakeContact()))

        events = handler.companion_get_events(_HASH, 0)
        assert len(events) == 1
        assert events[0]["event_type"] == "contact"
        assert events[0]["payload"]["change"] == "update"
        assert events[0]["payload"]["name"] == "Carol"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
