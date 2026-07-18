"""Tests for the phase-4 push notifier (design doc §12.2).

The worker thread's timing is awkward to test directly, so these drive the
notifier's internal steps (``_on_event`` → ``_expand_dirty`` → ``_collect_due``
→ ``_send_one``) with a controllable clock and a fake relay poster against a
real SQLite handler. That exercises the debounce, payload shaping, backoff,
and 410-invalidation logic deterministically.
"""

from __future__ import annotations

from urllib import error as urllib_error

import pytest

from repeater.companion.push_notifier import CompanionPushNotifier
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x42"


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class _FakePoster:
    """Scriptable relay poster. ``script`` is a list of ints (status codes) or
    exceptions to raise, consumed per call; a bare int/exception is reused."""

    def __init__(self, script=200):
        self.script = script
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        item = self.script
        if isinstance(self.script, list):
            item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


def _device(handler, device_id="dev-1", token_hash="h1", push_token="apns-1",
            relay="https://relay.example/notify", detail="none"):
    token_id = handler.create_api_token("t", token_hash, scope=f"companion:x")
    handler.companion_device_create(_HASH, device_id, "Phone", token_id, platform="ios")
    if push_token is not None:
        handler.companion_device_set_push(
            device_id, push_token, push_relay_url=relay, push_detail=detail
        )
    return device_id


def _notifier(handler, poster, clock, **kw):
    return CompanionPushNotifier(
        handler, poster=poster, clock=clock, min_interval=30.0, **kw
    )


def _message_event(seq=1, text="hello"):
    return {"seq": seq, "event_type": "message", "payload": {"text": text}}


def _drive_once(n):
    """Run one expand+collect+send cycle over whatever is dirty/pending."""
    dirty = dict(n._dirty)
    n._dirty = {}
    if dirty:
        n._expand_dirty(dirty)
    due, next_delay = n._collect_due()
    for device_id, pending in due:
        n._send_one(device_id, pending)
    return due, next_delay


# --- trigger filtering ---------------------------------------------------


def test_non_message_events_do_not_trigger(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    for et in ("contact", "channel", "prefs", "message_reception", "rf_reception"):
        n._on_event(_HASH, {"event_type": et, "payload": {}})
    assert n._dirty == {}


def test_message_event_marks_dirty(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    n._on_event(_HASH, _message_event())
    assert _HASH in n._dirty
    assert n._dirty[_HASH].count == 1


def test_disabled_notifier_ignores_events(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock(), enabled=False)
    n._on_event(_HASH, _message_event())
    assert n._dirty == {}


# --- fan-out + delivery --------------------------------------------------


def test_message_delivers_wake_to_registered_device(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert len(poster.calls) == 1
    payload = poster.calls[0]["payload"]
    assert payload["push_token"] == "apns-1"
    assert payload["collapse_id"] == _HASH
    # payload-free by default: no badge/alert
    assert "badge_hint" not in payload and "alert" not in payload


def test_device_without_push_token_is_skipped(tmp_path):
    h = _handler(tmp_path)
    _device(h, push_token=None)  # paired but never registered push
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert poster.calls == []


# --- debounce ------------------------------------------------------------


def test_burst_within_interval_collapses_to_one_push(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    clock = _Clock()
    n = _notifier(h, poster, clock)

    n._on_event(_HASH, _message_event(1))
    _drive_once(n)                       # first send at t=0
    assert len(poster.calls) == 1

    clock.advance(5)                     # still within min_interval
    n._on_event(_HASH, _message_event(2))
    due, next_delay = _drive_once(n)     # not due yet
    assert len(poster.calls) == 1
    assert next_delay is not None and next_delay > 0

    clock.advance(30)                    # past the interval
    _drive_once(n)                       # the trailing event now sends
    assert len(poster.calls) == 2


def test_count_detail_accumulates_across_debounced_events(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="count")
    poster = _FakePoster(200)
    clock = _Clock()
    n = _notifier(h, poster, clock)

    n._on_event(_HASH, _message_event(1))
    _drive_once(n)
    assert poster.calls[0]["payload"]["badge_hint"] == 1

    clock.advance(5)
    n._on_event(_HASH, _message_event(2))
    n._on_event(_HASH, _message_event(3))
    _drive_once(n)                        # queued, not sent
    clock.advance(30)
    _drive_once(n)
    assert poster.calls[1]["payload"]["badge_hint"] == 2


# --- payload detail levels ----------------------------------------------


def test_preview_detail_includes_truncated_alert(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    long_text = "x" * 500
    n._on_event(_HASH, _message_event(text=long_text))
    _drive_once(n)
    alert = poster.calls[0]["payload"]["alert"]
    assert alert.endswith("…")
    assert len(alert) <= 141


def test_none_detail_never_includes_content(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="none")
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event(text="secret text"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert "alert" not in payload and "badge_hint" not in payload


# --- failure handling ----------------------------------------------------


def test_relay_410_clears_push_token(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(410)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert h.companion_device_get("dev-1")["push_token"] is None


def test_5xx_requeues_with_backoff(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(503)
    clock = _Clock()
    n = _notifier(h, poster, clock)
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert "dev-1" in n._device_pending
    assert n._device_pending["dev-1"].attempts == 1
    assert n._device_pending["dev-1"].not_before > clock.now


def test_transient_error_requeues(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(urllib_error.URLError("connection refused"))
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert "dev-1" in n._device_pending
    assert n._device_pending["dev-1"].attempts == 1


def test_gives_up_after_max_attempts(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(503)
    clock = _Clock()
    n = _notifier(h, poster, clock, max_attempts=3)
    n._on_event(_HASH, _message_event())
    for _ in range(5):
        clock.advance(1000)  # blow past every backoff window
        _drive_once(n)
    assert "dev-1" not in n._device_pending
    assert len(poster.calls) == 3  # exactly max_attempts sends, then dropped


def test_4xx_drops_without_requeue(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(400)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    _drive_once(n)
    assert "dev-1" not in n._device_pending
    assert len(poster.calls) == 1


def test_token_cleared_between_queue_and_send_is_noop(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    n._on_event(_HASH, _message_event())
    # simulate an unregister landing after the event was queued
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    h.companion_device_clear_push("dev-1")
    due, _ = n._collect_due()
    for device_id, pending in due:
        n._send_one(device_id, pending)
    assert poster.calls == []


# --- lifecycle -----------------------------------------------------------


def test_start_stop_is_clean(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    n.start()
    n.stop()
    assert n._worker is None


# --- journal wiring contract --------------------------------------------


def test_journal_record_message_reaches_notifier(tmp_path):
    """The make_listener/register_listener/record_message contract: a real
    journal append must invoke the notifier's listener with event_type
    'message', so the wiring in main.py delivers a push."""
    from repeater.companion.journal import CompanionEventJournal

    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())

    journal = CompanionEventJournal(h, _HASH)
    journal.register_listener(n.make_listener(_HASH))

    # A message append fires the listener → marks dirty.
    journal.record_message({"id": 1, "text": "ping", "packet_hash": None})
    assert _HASH in n._dirty

    _drive_once(n)
    assert len(poster.calls) == 1
    assert poster.calls[0]["payload"]["push_token"] == "apns-1"


def test_journal_non_message_does_not_reach_notifier(tmp_path):
    from repeater.companion.journal import CompanionEventJournal

    h = _handler(tmp_path)
    _device(h)
    n = _notifier(h, _FakePoster(), _Clock())
    journal = CompanionEventJournal(h, _HASH)
    journal.register_listener(n.make_listener(_HASH))

    journal.record_prefs({"node_name": "new-name"})
    assert n._dirty == {}
