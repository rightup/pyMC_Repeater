"""Debounced push notifier for the Mobile Companion API (phase 4).

``CompanionPushNotifier`` turns companion journal events into push wakes for
paired mobile devices (design doc §12.2). It is deliberately a thin,
low-trust signal path:

- **Payload-free by default.** The push is a "you have new events, sync"
  wake. Only when a device opts into ``push_detail: count`` (a badge hint) or
  ``preview`` (a short alert string) does anything beyond the token leave the
  repeater — the relay/APNs learn *that* a device got traffic, not *what*,
  unless the operator opted in per device.
- **The relay is a separate deliverable.** This class only POSTs to the
  ``push_relay_url`` each device registered; until a relay exists nothing
  receives the POST and the app runs on background refresh alone (§12.2).

Design shape:

- Registered as an in-process listener on every companion journal (the same
  hook SSE uses). The listener call is trivial — it marks the companion dirty
  and wakes the worker; no DB or network I/O on the journal append path.
- A single dedicated worker thread expands dirty companions to their
  push-registered devices, coalesces per device with a minimum interval
  (trailing-edge debounce — collapse a burst into one push), and POSTs.
- Only ``message`` events trigger a push. Contact/channel/prefs changes and
  the RF-correlation event types are not wake-worthy; restricting to messages
  also bounds push volume to the companion's message rate, not the mesh's
  (design doc §13).
- Backoff is non-blocking: a failed send is requeued with an exponential
  ``not_before`` delay (so one slow relay never blocks other devices) up to a
  cap, then dropped. A relay ``410 Gone`` clears the device's ``push_token``
  (the client unregistered on the relay side).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger("CompanionPushNotifier")

#: event types that warrant a push wake (design doc §12.2 / §13)
_PUSH_EVENT_TYPES = frozenset({"message"})

#: preview strings are truncated so an opted-in `preview` push never carries a
#: whole message.
_PREVIEW_MAX_CHARS = 140

#: per-pass cap on message texts kept for mention matching (design doc §12.2);
#: a burst larger than this still wakes/counts, it just stops scanning more
#: texts for a mention hit.
_MAX_MATCH_TEXTS = 64

#: how long a resolved companion node_name (the default mention trigger) is
#: cached before a re-read picks up a rename.
_NODE_NAME_TTL = 300.0

#: content-free mention alert body (design doc §11.4 / §12.2): the fact of a
#: mention, never the message text.
_MENTION_ALERT = "You were mentioned"


class _CompanionAccum:
    """Accumulated new-event info for one companion since the last worker pass."""

    __slots__ = ("count", "preview", "texts")

    def __init__(self) -> None:
        self.count = 0
        self.preview: Optional[str] = None
        # Raw message texts, for per-device mention matching in the worker.
        self.texts: List[str] = []


class _DevicePending:
    """A device awaiting a (trailing-edge) push, with backoff bookkeeping."""

    __slots__ = ("companion_hash", "count", "preview", "mention", "attempts", "not_before")

    def __init__(self, companion_hash: str) -> None:
        self.companion_hash = companion_hash
        self.count = 0
        self.preview: Optional[str] = None
        self.mention = False
        self.attempts = 0
        self.not_before = 0.0


def _default_poster(url: str, payload: dict, timeout: float) -> int:
    """POST ``payload`` as JSON to ``url``; return the HTTP status code.

    Uses only the stdlib (the daemon has no third-party HTTP client — see
    ``glass_handler.py``). Runs in the notifier's worker thread, so a blocking
    call is fine. Raises ``urllib.error.URLError``/``OSError`` on a transport
    failure (the caller treats that as transient); an HTTP error *status* (4xx/
    5xx) comes back as a code via ``HTTPError``, not an exception.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib_error.HTTPError as exc:
        return exc.code


class CompanionPushNotifier:
    """Process-wide push notifier; one per daemon."""

    def __init__(
        self,
        sqlite_handler,
        *,
        min_interval: float = 30.0,
        request_timeout: float = 10.0,
        max_attempts: int = 4,
        backoff_base: float = 2.0,
        backoff_cap: float = 300.0,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        poster: Optional[Callable[[str, dict, float], int]] = None,
    ) -> None:
        self.sqlite_handler = sqlite_handler
        self.min_interval = max(0.0, float(min_interval))
        self.request_timeout = float(request_timeout)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.enabled = bool(enabled)
        self._clock = clock
        self._poster = poster or _default_poster

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._dirty: Dict[str, _CompanionAccum] = {}
        self._device_pending: Dict[str, _DevicePending] = {}
        self._device_cooldown: Dict[str, float] = {}
        # companion_hash -> (node_name, fetched_at); the default mention trigger.
        self._node_name_cache: Dict[str, tuple] = {}
        self._stop = False
        self._worker: Optional[threading.Thread] = None

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._stop = False
        self._worker = threading.Thread(
            target=self._run, name="companion-push-notifier", daemon=True
        )
        self._worker.start()
        logger.info("Companion push notifier started (min_interval=%ss)", self.min_interval)

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
        self._worker = None

    # -----------------------------------------------------------------
    # Journal listener
    # -----------------------------------------------------------------

    def make_listener(self, companion_hash: str) -> Callable[[dict], None]:
        """Return a journal listener bound to ``companion_hash``.

        Register the result on that companion's ``CompanionEventJournal`` (the
        same ``register_listener`` hook SSE uses). The callback is intentionally
        trivial — no DB, no network — so it never slows a journal append.
        """

        def _listener(event: dict) -> None:
            self._on_event(companion_hash, event)

        return _listener

    def _on_event(self, companion_hash: str, event: dict) -> None:
        if not self.enabled:
            return
        if event.get("event_type") not in _PUSH_EVENT_TYPES:
            return
        preview = self._extract_preview(event)
        text = (event.get("payload") or {}).get("text")
        with self._cv:
            accum = self._dirty.get(companion_hash)
            if accum is None:
                accum = _CompanionAccum()
                self._dirty[companion_hash] = accum
            accum.count += 1
            if preview is not None:
                accum.preview = preview
            if isinstance(text, str) and text.strip() and len(accum.texts) < _MAX_MATCH_TEXTS:
                accum.texts.append(text)
            self._cv.notify()

    @staticmethod
    def _extract_preview(event: dict) -> Optional[str]:
        payload = event.get("payload") or {}
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return None
        text = text.strip()
        if len(text) > _PREVIEW_MAX_CHARS:
            text = text[:_PREVIEW_MAX_CHARS].rstrip() + "…"
        return text

    # -----------------------------------------------------------------
    # Mention detection (design doc §12.2)
    # -----------------------------------------------------------------

    def _device_triggers(self, device: dict, companion_hash: str) -> List[str]:
        """The mention trigger strings for a device: its explicit
        ``mention_keywords`` if any, else the companion's node_name."""
        raw = device.get("mention_keywords")
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = []
            triggers = [t for t in parsed if isinstance(t, str) and t.strip()]
            if triggers:
                return triggers
        node_name = self._node_name(companion_hash)
        return [node_name] if node_name else []

    def _node_name(self, companion_hash: str) -> str:
        now = self._clock()
        cached = self._node_name_cache.get(companion_hash)
        if cached is not None and now - cached[1] < _NODE_NAME_TTL:
            return cached[0]
        try:
            prefs = self.sqlite_handler.companion_load_prefs(companion_hash) or {}
        except Exception:
            logger.exception("push notifier: node_name lookup failed for %s", companion_hash)
            prefs = {}
        node_name = (prefs.get("node_name") or "").strip()
        self._node_name_cache[companion_hash] = (node_name, now)
        return node_name

    @staticmethod
    def _any_mention(texts: List[str], triggers: List[str]) -> bool:
        """True if any text contains any trigger as a whole token (bounded by
        non-alphanumeric characters, case-insensitive) — so ``adam`` matches
        "hey adam" and "@adam" but not "adamant"."""
        patterns = []
        for trig in triggers:
            trig = trig.strip()
            if not trig:
                continue
            patterns.append(
                re.compile(
                    r"(?<![0-9A-Za-z])" + re.escape(trig) + r"(?![0-9A-Za-z])",
                    re.IGNORECASE,
                )
            )
        for text in texts:
            for pat in patterns:
                if pat.search(text):
                    return True
        return False

    # -----------------------------------------------------------------
    # Worker
    # -----------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stop and not self._dirty and not self._device_pending:
                    self._cv.wait()
                if self._stop:
                    return
                # Move any newly-dirty companions out under the lock; the DB
                # expansion below runs without it.
                dirty = self._dirty
                self._dirty = {}

            if dirty:
                self._expand_dirty(dirty)

            due, next_delay = self._collect_due()
            for device_id, pending in due:
                self._send_one(device_id, pending)

            if not due:
                # Nothing was due this pass: sleep until the soonest pending
                # device is due (or until a new event notifies us).
                with self._cv:
                    if self._stop:
                        return
                    if not self._dirty:
                        self._cv.wait(timeout=next_delay)

    def _expand_dirty(self, dirty: Dict[str, _CompanionAccum]) -> None:
        """Turn dirty companions into per-device pending entries (DB query
        outside the append hot path)."""
        for companion_hash, accum in dirty.items():
            try:
                devices = self.sqlite_handler.companion_devices_with_push(companion_hash)
            except Exception:
                logger.exception("push notifier: device lookup failed for %s", companion_hash)
                continue
            if not devices:
                continue
            # Evaluate mentions outside the lock (node_name resolution reads
            # the DB): device_id -> True if any accumulated text mentions it.
            mention_hits: Dict[str, bool] = {}
            if accum.texts:
                for device in devices:
                    if device.get("mention_push"):
                        triggers = self._device_triggers(device, companion_hash)
                        mention_hits[device["device_id"]] = bool(triggers) and self._any_mention(
                            accum.texts, triggers
                        )
            with self._cv:
                for device in devices:
                    device_id = device["device_id"]
                    pending = self._device_pending.get(device_id)
                    if pending is None:
                        pending = _DevicePending(companion_hash)
                        self._device_pending[device_id] = pending
                    pending.count += accum.count
                    if accum.preview is not None:
                        pending.preview = accum.preview
                    if mention_hits.get(device_id):
                        pending.mention = True

    def _collect_due(self):
        """Return ((device_id, pending), …) that are due to send now, and the
        delay until the next not-yet-due device (or None)."""
        now = self._clock()
        due = []
        next_delay: Optional[float] = None
        with self._cv:
            for device_id, pending in list(self._device_pending.items()):
                ready_at = max(
                    self._device_cooldown.get(device_id, 0.0) + self.min_interval,
                    pending.not_before,
                )
                if now >= ready_at:
                    due.append((device_id, pending))
                    del self._device_pending[device_id]
                    self._device_cooldown[device_id] = now
                else:
                    delay = ready_at - now
                    next_delay = delay if next_delay is None else min(next_delay, delay)
        return due, next_delay

    # -----------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------

    def _send_one(self, device_id: str, pending: _DevicePending) -> None:
        # Re-read the device so a token cleared/refreshed since the event is
        # honoured, and we never send to a stale token.
        try:
            device = self.sqlite_handler.companion_device_get(device_id)
        except Exception:
            logger.exception("push notifier: device re-read failed for %s", device_id)
            return
        if device is None or not device.get("push_token"):
            return  # unregistered since the event was queued: drop silently.
        relay_url = device.get("push_relay_url")
        if not relay_url:
            return  # can't reach a relay we were never told about.

        payload = self._build_payload(device, pending)
        try:
            status = self._poster(relay_url, payload, self.request_timeout)
        except (urllib_error.URLError, OSError) as exc:
            self._requeue(device_id, pending, reason=str(exc))
            return
        except Exception:
            logger.exception("push notifier: unexpected poster error for %s", device_id)
            self._requeue(device_id, pending, reason="unexpected")
            return

        if status == 410:
            # Relay says the token is gone (client unregistered). Invalidate.
            self.sqlite_handler.companion_device_clear_push(device_id)
            logger.info("push notifier: relay 410 for %s; cleared push_token", device_id)
            return
        if 200 <= status < 300:
            return
        if 500 <= status < 600:
            self._requeue(device_id, pending, reason=f"relay {status}")
            return
        # Other 4xx: a request-level problem retrying won't fix — drop.
        logger.warning("push notifier: relay %s for %s; dropping push", status, device_id)

    def _build_payload(self, device: dict, pending: _DevicePending) -> dict:
        """Build the relay ``/notify`` body. Payload-free by default; badge/
        alert only when the device opted in (design doc §12.2).

        A mention takes precedence: it becomes a content-free ``mention`` alert
        (``"You were mentioned"``, never the message text — §11.4) regardless
        of ``push_detail``. The ``mention`` flag tells the relay to send a
        user-visible APNs alert rather than a silent wake, which is what makes
        a mention prompt.

        ``platform`` is included when the device recorded one at pairing, so
        the relay can route APNs vs FCM directly instead of guessing from the
        token's shape. It is optional and free-form (the pairing endpoint does
        not validate it), so the relay must still cope with it being absent or
        unrecognised — omitting it here is not an error."""
        payload = {
            "push_token": device["push_token"],
            "collapse_id": pending.companion_hash,
        }
        platform = (device.get("platform") or "").strip().lower()
        if platform:
            payload["platform"] = platform
        if pending.mention:
            payload["mention"] = True
            payload["alert"] = _MENTION_ALERT
            return payload
        detail = device.get("push_detail") or "none"
        if detail == "count":
            payload["badge_hint"] = pending.count
        elif detail == "preview" and pending.preview:
            payload["alert"] = pending.preview
        return payload

    def _requeue(self, device_id: str, pending: _DevicePending, *, reason: str) -> None:
        pending.attempts += 1
        if pending.attempts >= self.max_attempts:
            logger.warning(
                "push notifier: giving up on %s after %d attempts (%s)",
                device_id,
                pending.attempts,
                reason,
            )
            return
        delay = min(self.backoff_base ** pending.attempts, self.backoff_cap)
        pending.not_before = self._clock() + delay
        # The cooldown was already stamped when this send was dequeued; clear
        # it so the backoff delay (not the min-interval) governs the retry.
        with self._cv:
            self._device_cooldown.pop(device_id, None)
            self._device_pending[device_id] = pending
            self._cv.notify()
        logger.debug(
            "push notifier: requeued %s in %.1fs (attempt %d, %s)",
            device_id,
            delay,
            pending.attempts,
            reason,
        )
