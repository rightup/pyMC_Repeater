"""Tests for the phase-4 push notifier (design doc §12.2).

The worker thread's timing is awkward to test directly, so these drive the
notifier's internal steps (``_on_event`` → ``_expand_dirty`` → ``_collect_due``
→ ``_send_one``) with a controllable clock and a fake relay poster against a
real SQLite handler. That exercises the debounce, payload shaping, backoff,
and 410-invalidation logic deterministically.
"""

from __future__ import annotations

import io
import threading
from unittest.mock import MagicMock, patch
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

from repeater.companion.push_notifier import CompanionPushNotifier
from repeater.companion import push_notifier
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.main import RepeaterDaemon

_HASH = "0x42"
_IDENTITY = "42" * 32


def test_push_http_opener_ignores_ambient_proxy_environment():
    with patch.object(
        push_notifier.urllib_request,
        "build_opener",
        return_value=object(),
    ) as build_opener:
        opener = push_notifier._build_post_opener()

    assert opener is build_opener.return_value
    handlers = build_opener.call_args.args
    assert isinstance(handlers[0], urllib_request.ProxyHandler)
    assert handlers[0].proxies == {}


@pytest.mark.parametrize(
    "relay_url",
    [
        "https://relay.example:0/notify",
        "https://relay.example:65536/notify",
        "https://:443/notify",
    ],
)
def test_push_relay_rejects_invalid_authority(relay_url):
    with pytest.raises(ValueError, match="push relay URL"):
        push_notifier.validate_relay_url(relay_url)


def test_default_poster_closes_http_error_response():
    body = io.BytesIO(b"rejected")
    error = urllib_error.HTTPError(
        "https://relay.example/notify",
        410,
        "Gone",
        {},
        body,
    )

    with patch.object(push_notifier._POST_OPENER, "open", side_effect=error):
        assert (
            push_notifier._default_poster(
                "https://relay.example/notify",
                {"push_token": "opaque"},
                1.0,
            )
            == 410
        )

    assert body.closed


def test_daemon_uses_operator_owned_relay_and_bounded_workers():
    daemon = RepeaterDaemon(
        {
            "companion": {
                "push": {
                    "relay_url": "https://push.example/notify",
                    "allow_insecure_http": False,
                    "worker_count": 99,
                }
            }
        },
        radio=object(),
    )
    sqlite = object()

    with patch("repeater.main.CompanionPushNotifier") as notifier_cls:
        notifier_cls.return_value.start = MagicMock()
        result = daemon._build_push_notifier(sqlite)

    assert result is notifier_cls.return_value
    kwargs = notifier_cls.call_args.kwargs
    assert kwargs["relay_url"] == "https://push.example/notify"
    assert kwargs["allow_insecure_http"] is False
    assert kwargs["worker_count"] == 4
    notifier_cls.return_value.start.assert_called_once()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("enabled", "false"),
        ("allow_insecure_http", "true"),
        ("min_interval_sec", "30"),
        ("min_interval_sec", float("nan")),
        ("min_interval_sec", float("inf")),
        ("min_interval_sec", -1),
        ("min_interval_sec", 86_401),
        ("request_timeout_sec", 0),
        ("request_timeout_sec", float("nan")),
        ("request_timeout_sec", float("inf")),
        ("request_timeout_sec", 301),
        ("worker_count", "2"),
    ],
)
def test_daemon_rejects_invalid_push_config_before_notifier_side_effects(
    setting,
    value,
):
    daemon = RepeaterDaemon(
        {"companion": {"push": {setting: value}}},
        radio=object(),
    )

    with (
        patch("repeater.main.CompanionPushNotifier") as notifier_cls,
        pytest.raises(ValueError, match=setting),
    ):
        daemon._build_push_notifier(object())

    notifier_cls.assert_not_called()


@pytest.mark.parametrize(
    "push_config",
    ["enabled", [], 1],
)
def test_daemon_rejects_non_object_push_config(push_config):
    daemon = RepeaterDaemon(
        {"companion": {"push": push_config}},
        radio=object(),
    )

    with (
        patch("repeater.main.CompanionPushNotifier") as notifier_cls,
        pytest.raises(ValueError, match="companion.push"),
    ):
        daemon._build_push_notifier(object())

    notifier_cls.assert_not_called()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("max_attempts", True),
        ("max_attempts", 0),
        ("backoff_base", 0),
        ("backoff_base", float("nan")),
        ("backoff_base", float("inf")),
        ("backoff_cap", 0),
        ("backoff_cap", float("nan")),
        ("backoff_cap", float("inf")),
        ("worker_count", True),
        ("worker_count", "2"),
    ],
)
def test_notifier_rejects_ambiguous_or_unsafe_constructor_settings(
    tmp_path,
    setting,
    value,
):
    with pytest.raises(ValueError, match=setting):
        CompanionPushNotifier(
            _handler(tmp_path),
            relay_url="https://relay.example/notify",
            **{setting: value},
        )


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
            relay="https://relay.example/notify", detail="none",
            mention_push=None, mention_keywords=None, platform="ios"):
    token_id = handler.create_api_token("t", token_hash, scope="companion:x")
    handler.companion_device_create(
        _HASH,
        device_id,
        "Phone",
        token_id,
        platform=platform,
        companion_identity=_IDENTITY,
    )
    if push_token is not None:
        handler.companion_device_set_push(
            device_id, push_token, push_relay_url=relay, push_detail=detail,
            mention_push=mention_push, mention_keywords=mention_keywords,
        )
    return device_id


def _notifier(handler, poster, clock, **kw):
    notifier = CompanionPushNotifier(
        handler,
        poster=poster,
        clock=clock,
        min_interval=30.0,
        relay_url="https://relay.example/notify",
        **kw,
    )
    notifier.make_listener(_HASH, _IDENTITY)
    return notifier


def _message_event(seq=1, text="hello"):
    return {"seq": seq, "event_type": "message", "payload": {"text": text}}


@pytest.mark.parametrize("stored_keywords", ["1", "{}", "null"])
def test_non_array_legacy_mention_keywords_fail_closed(
    tmp_path,
    stored_keywords,
):
    handler = _handler(tmp_path)
    device_id = _device(handler, mention_push=True)
    with handler._connect() as connection:
        connection.execute(
            "UPDATE companion_devices SET mention_keywords = ? WHERE device_id = ?",
            (stored_keywords, device_id),
        )
    notifier = _notifier(handler, _FakePoster(), _Clock())

    _emit(notifier, _message_event(text="hello"))
    due, _ = _drive_once(notifier)

    assert len(due) == 1
    assert due[0][1].mention is False


def _emit(notifier, event, identity=_IDENTITY):
    notifier._on_event(
        _HASH,
        event,
        companion_identity=identity,
    )


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
        _emit(n, {"event_type": et, "payload": {}})
    assert n._dirty == {}


def test_event_without_registered_full_identity_fails_closed(tmp_path):
    n = CompanionPushNotifier(
        _handler(tmp_path),
        poster=_FakePoster(),
        clock=_Clock(),
        min_interval=30.0,
        relay_url="https://relay.example/notify",
    )

    _emit(n, _message_event(text="must not fan out"))

    assert n._dirty == {}


def test_listener_rejects_second_full_identity_for_same_hash(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())

    with pytest.raises(ValueError, match="already active"):
        n.make_listener("42", "42" + ("99" * 31))


def test_listener_canonicalizes_hash_and_identity(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    listener = n.make_listener("42", _IDENTITY.upper())

    listener(_message_event())

    assert list(n._dirty) == [(_HASH, _IDENTITY)]


def test_message_event_marks_dirty(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    _emit(n, _message_event())
    assert (_HASH, _IDENTITY) in n._dirty
    assert n._dirty[(_HASH, _IDENTITY)].count == 1


def test_disabled_notifier_ignores_events(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock(), enabled=False)
    _emit(n, _message_event())
    assert n._dirty == {}


# --- fan-out + delivery --------------------------------------------------


def test_message_delivers_wake_to_registered_device(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert len(poster.calls) == 1
    payload = poster.calls[0]["payload"]
    assert payload["push_token"] == "apns-1"
    assert payload["collapse_id"] == _HASH
    # payload-free by default: no badge/alert
    assert "badge_hint" not in payload and "alert" not in payload


def test_full_identity_mismatch_cannot_select_or_preview_push_device(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    poster = _FakePoster(200)
    other_identity = "42" + ("99" * 31)
    n = CompanionPushNotifier(
        h,
        poster=poster,
        clock=_Clock(),
        min_interval=30.0,
        relay_url="https://relay.example/notify",
    )

    listener = n.make_listener(_HASH, other_identity)
    listener(_message_event(text="must not leak"))
    _drive_once(n)

    assert poster.calls == []
    assert n._device_pending == {}


def test_pending_push_rechecks_full_identity_before_delivery(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    listener = n.make_listener(_HASH, _IDENTITY)

    listener(_message_event(text="stale preview"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    with h._connect() as conn:
        conn.execute(
            """
            UPDATE companion_devices
            SET companion_identity = ?
            WHERE device_id = 'dev-1'
            """,
            ("42" + ("99" * 31),),
        )
        conn.commit()
    due, _ = n._collect_due()
    for device_id, pending in due:
        n._send_one(device_id, pending)

    assert poster.calls == []


# --- platform routing hint -----------------------------------------------


def test_payload_carries_platform_so_relay_need_not_guess(tmp_path):
    h = _handler(tmp_path)
    _device(h, platform="ios")
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert poster.calls[0]["payload"]["platform"] == "ios"


def test_platform_is_normalised(tmp_path):
    h = _handler(tmp_path)
    _device(h, platform="  Android  ")  # pairing does not validate this
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert poster.calls[0]["payload"]["platform"] == "android"


@pytest.mark.parametrize("platform", [None, "", "   "])
def test_platform_omitted_when_device_has_none(tmp_path, platform):
    """platform is optional at pairing, so the key must simply be absent
    rather than sent as null -- the relay falls back to token-shape inference."""
    h = _handler(tmp_path)
    _device(h, platform=platform)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert "platform" not in poster.calls[0]["payload"]


def test_platform_present_on_mention_payload_too(tmp_path):
    h = _handler(tmp_path)
    _device(h, platform="ios", mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="hey adam"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert payload["mention"] is True
    assert payload["platform"] == "ios"


def test_device_without_push_token_is_skipped(tmp_path):
    h = _handler(tmp_path)
    _device(h, push_token=None)  # paired but never registered push
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert poster.calls == []


# --- debounce ------------------------------------------------------------


def test_burst_within_interval_collapses_to_one_push(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    clock = _Clock()
    n = _notifier(h, poster, clock)

    _emit(n, _message_event(1))
    _drive_once(n)                       # first send at t=0
    assert len(poster.calls) == 1

    clock.advance(5)                     # still within min_interval
    _emit(n, _message_event(2))
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

    _emit(n, _message_event(1))
    _drive_once(n)
    assert poster.calls[0]["payload"]["badge_hint"] == 1

    clock.advance(5)
    _emit(n, _message_event(2))
    _emit(n, _message_event(3))
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
    _emit(n, _message_event(text=long_text))
    _drive_once(n)
    alert = poster.calls[0]["payload"]["alert"]
    assert alert.endswith("…")
    assert len(alert) <= 141


def test_none_detail_never_includes_content(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="none")
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="secret text"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert "alert" not in payload and "badge_hint" not in payload


# --- failure handling ----------------------------------------------------


def test_relay_410_clears_push_token(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(410)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert h.companion_device_get("dev-1")["push_token"] is None


def test_relay_410_does_not_clear_a_token_refreshed_in_flight(tmp_path):
    h = _handler(tmp_path)
    _device(h)

    class _RefreshThenGone:
        def __call__(self, relay_url, payload, timeout):
            assert payload["push_token"] == "apns-1"
            assert h.companion_device_set_push("dev-1", "apns-2") is True
            return 410

    n = _notifier(h, _RefreshThenGone(), _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert h.companion_device_get("dev-1")["push_token"] == "apns-2"


def test_relay_410_does_not_clear_repaired_device_with_same_id_and_token(
    tmp_path,
):
    h = _handler(tmp_path)
    _device(h)
    replacement_hash = "0x43"
    replacement_identity = "43" * 32

    class _RepairThenGone:
        def __call__(self, relay_url, payload, timeout):
            assert payload["push_token"] == "apns-1"
            assert h.companion_device_delete("dev-1") is True
            replacement_token_id = h.create_api_token(
                "replacement",
                "replacement-hash",
                scope="companion:replacement",
            )
            assert (
                h.companion_device_create(
                    replacement_hash,
                    "dev-1",
                    "Replacement",
                    replacement_token_id,
                    companion_identity=replacement_identity,
                )
                is not None
            )
            assert h.companion_device_set_push("dev-1", "apns-1") is True
            return 410

    n = _notifier(h, _RepairThenGone(), _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    replacement = h.companion_device_get("dev-1")
    assert replacement["companion_hash"] == replacement_hash
    assert replacement["companion_identity"] == replacement_identity
    assert replacement["push_token"] == "apns-1"


def test_expired_device_cooldowns_are_pruned(tmp_path):
    h = _handler(tmp_path)
    clock = _Clock()
    n = _notifier(h, _FakePoster(200), clock)
    n._device_cooldown["revoked-device"] = clock.now

    clock.advance(n.min_interval)
    due, next_delay = n._collect_due()

    assert due == []
    assert next_delay is None
    assert n._device_cooldown == {}


def test_5xx_requeues_with_backoff(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(503)
    clock = _Clock()
    n = _notifier(h, poster, clock)
    _emit(n, _message_event())
    _drive_once(n)
    assert "dev-1" in n._device_pending
    assert n._device_pending["dev-1"].attempts == 1
    assert n._device_pending["dev-1"].not_before > clock.now


def test_transient_error_requeues(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(urllib_error.URLError("connection refused"))
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
    _drive_once(n)
    assert "dev-1" in n._device_pending
    assert n._device_pending["dev-1"].attempts == 1


def test_failed_older_delivery_merges_without_overwriting_newer_events(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    clock = _Clock()
    n = _notifier(h, _FakePoster(), clock)

    _emit(n, _message_event(1, text="older"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    due, _ = n._collect_due()
    device_id, older = due[0]  # now in flight and absent from _device_pending

    _emit(n, _message_event(2, text="newer"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    n._requeue(device_id, older, reason="relay 503")

    merged = n._device_pending[device_id]
    assert merged.count == 2
    assert merged.preview == "newer"
    assert merged.attempts == 1


def test_retry_merge_keeps_newest_preview_regardless_of_completion_order(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    clock = _Clock()
    n = _notifier(h, _FakePoster(), clock)

    _emit(n, _message_event(1, text="older"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    older = n._collect_due()[0][0][1]

    _emit(n, _message_event(2, text="newer"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    clock.advance(31)
    newer = n._collect_due()[0][0][1]

    n._requeue("dev-1", older, reason="older finished first")
    n._requeue("dev-1", newer, reason="newer finished second")

    assert n._device_pending["dev-1"].count == 2
    assert n._device_pending["dev-1"].preview == "newer"


def test_exhausted_older_delivery_does_not_drop_newer_pending_event(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview")
    n = _notifier(h, _FakePoster(), _Clock(), max_attempts=2)

    _emit(n, _message_event(1, text="older"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    due, _ = n._collect_due()
    device_id, older = due[0]
    older.attempts = 1

    _emit(n, _message_event(2, text="newer"))
    dirty = dict(n._dirty)
    n._dirty = {}
    n._expand_dirty(dirty)
    n._requeue(device_id, older, reason="relay 503")

    remaining = n._device_pending[device_id]
    assert remaining.count == 1
    assert remaining.preview == "newer"
    assert remaining.attempts == 0


def test_gives_up_after_max_attempts(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(503)
    clock = _Clock()
    n = _notifier(h, poster, clock, max_attempts=3)
    _emit(n, _message_event())
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
    _emit(n, _message_event())
    _drive_once(n)
    assert "dev-1" not in n._device_pending
    assert len(poster.calls) == 1


def test_token_cleared_between_queue_and_send_is_noop(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event())
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


def test_deactivate_is_exact_and_stale_listener_fails_closed(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    poster = _FakePoster()
    n = _notifier(h, poster, _Clock())
    listener = n.make_listener(_HASH, _IDENTITY)
    listener(_message_event(text="queued"))

    assert n.deactivate(_HASH, "42" + ("99" * 31)) is False
    assert (_HASH, _IDENTITY) in n._dirty
    assert n.deactivate("42", _IDENTITY.upper()) is True
    assert n._dirty == {}

    listener(_message_event(text="stale callback"))
    _drive_once(n)
    assert n._dirty == {}
    assert poster.calls == []


def test_deactivate_wins_a_concurrent_listener_callback(tmp_path):
    n = _notifier(_handler(tmp_path), _FakePoster(), _Clock())
    listener = n.make_listener(_HASH, _IDENTITY)
    entered = threading.Event()
    release = threading.Event()

    def blocking_preview(_event):
        entered.set()
        assert release.wait(2)
        return "preview"

    n._extract_preview = blocking_preview
    callback = threading.Thread(target=listener, args=(_message_event(),))
    callback.start()
    assert entered.wait(2)
    assert n.deactivate(_HASH, _IDENTITY) is True
    release.set()
    callback.join(timeout=2)

    assert not callback.is_alive()
    assert n._dirty == {}


def test_stop_waits_for_in_flight_relay_and_rejects_new_work(tmp_path):
    h = _handler(tmp_path)
    _device(h)
    entered = threading.Event()
    release = threading.Event()

    def blocking_poster(_url, _payload, _timeout):
        entered.set()
        assert release.wait(2)
        return 200

    n = _notifier(h, blocking_poster, _Clock())
    n.start()
    _emit(n, _message_event())
    assert entered.wait(2)

    stopped = threading.Event()

    def stop_notifier():
        n.stop()
        stopped.set()

    stopper = threading.Thread(target=stop_notifier)
    stopper.start()
    assert not stopped.wait(0.05)
    release.set()
    stopper.join(timeout=2)

    assert stopped.is_set()
    assert n._dirty == {}
    assert n._device_pending == {}
    _emit(n, _message_event(text="after stop"))
    assert n._dirty == {}


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
    journal.register_listener(n.make_listener(_HASH, _IDENTITY))

    # A message append fires the listener → marks dirty.
    journal.record_message({"id": 1, "text": "ping", "packet_hash": None})
    assert (_HASH, _IDENTITY) in n._dirty

    _drive_once(n)
    assert len(poster.calls) == 1
    assert poster.calls[0]["payload"]["push_token"] == "apns-1"


def test_journal_non_message_does_not_reach_notifier(tmp_path):
    from repeater.companion.journal import CompanionEventJournal

    h = _handler(tmp_path)
    _device(h)
    n = _notifier(h, _FakePoster(), _Clock())
    journal = CompanionEventJournal(h, _HASH)
    journal.register_listener(n.make_listener(_HASH, _IDENTITY))

    journal.record_prefs({"node_name": "new-name"})
    assert n._dirty == {}


# --- mentions ------------------------------------------------------------


def test_mention_off_gives_plain_wake_even_on_match(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=False, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="hey adam!"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert "mention" not in payload and "alert" not in payload


def test_keyword_match_sends_content_free_mention_alert(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="preview", mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="ping @adam are you there"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert payload["mention"] is True
    assert payload["alert"] == "You were mentioned"
    # content-free: the actual message text must never appear
    assert "adam are you there" not in payload["alert"]


def test_mention_takes_precedence_over_count_detail(tmp_path):
    h = _handler(tmp_path)
    _device(h, detail="count", mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="adam!"))
    _drive_once(n)
    payload = poster.calls[0]["payload"]
    assert payload.get("mention") is True
    assert "badge_hint" not in payload


def test_no_match_is_plain_wake(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="hello everyone"))
    _drive_once(n)
    assert "mention" not in poster.calls[0]["payload"]


def test_word_boundary_avoids_false_positive(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="that was adamant"))
    _drive_once(n)
    assert "mention" not in poster.calls[0]["payload"]


def test_default_trigger_is_companion_node_name(tmp_path):
    h = _handler(tmp_path)
    h.companion_save_prefs(_HASH, {"node_name": "Howl"})
    _device(h, mention_push=True)  # no explicit keywords -> node_name default
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="hey Howl come in"))
    _drive_once(n)
    assert poster.calls[0]["payload"]["mention"] is True


def test_no_trigger_when_no_keywords_and_no_node_name(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=True)  # no keywords, no prefs row -> no trigger
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())
    _emit(n, _message_event(text="anybody there"))
    _drive_once(n)
    assert "mention" not in poster.calls[0]["payload"]


def test_mention_match_across_debounced_burst(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    clock = _Clock()
    n = _notifier(h, poster, clock)
    # First message (no mention) sends a wake.
    _emit(n, _message_event(1, text="hello"))
    _drive_once(n)
    assert "mention" not in poster.calls[0]["payload"]
    # A mention arrives during the debounce window; the trailing send is a mention.
    clock.advance(5)
    _emit(n, _message_event(2, text="adam ping"))
    _drive_once(n)
    clock.advance(30)
    _drive_once(n)
    assert poster.calls[1]["payload"]["mention"] is True


def test_mention_overflow_is_bounded_and_conservatively_alerts(tmp_path):
    h = _handler(tmp_path)
    _device(h, mention_push=True, mention_keywords=["adam"])
    poster = _FakePoster(200)
    n = _notifier(h, poster, _Clock())

    for seq in range(65):
        _emit(n, _message_event(seq, text="ordinary traffic"))

    assert len(n._dirty[(_HASH, _IDENTITY)].texts) == 64
    assert n._dirty[(_HASH, _IDENTITY)].mention_overflow is True
    _drive_once(n)
    assert poster.calls[0]["payload"]["mention"] is True
    assert poster.calls[0]["payload"]["alert"] == "You were mentioned"
