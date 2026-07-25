"""Debounced push notifier for the Mobile Companion API (phase 4).

``CompanionPushNotifier`` turns companion journal events into push wakes for
paired mobile devices (design doc §12.2). It is deliberately a thin,
low-trust signal path:

- **Payload-free by default.** The push is a "you have new events, sync"
  wake. Only when a device opts into ``push_detail: count`` (a badge hint) or
  ``preview`` (a short alert string) does anything beyond the token leave the
  repeater — the relay/APNs learn *that* a device got traffic, not *what*,
  unless the operator opted in per device.
- **The relay is operator-configured.** A paired device registers only its
  push token. It cannot choose an arbitrary URL for the repeater to request.

Design shape:

- Registered as an in-process listener on every companion journal (the same
  hook SSE uses). The listener call is trivial — it marks the companion dirty
  and wakes the worker; no DB or network I/O on the journal append path.
- A coordinator thread expands dirty companions and coalesces per device.
  A small bounded worker pool performs the outbound POSTs.
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
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from repeater.companion.utils import (
    MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
    MAX_COMPANION_PUSH_REQUEST_TIMEOUT_SEC,
    validate_companion_boolean_setting,
    validate_companion_seconds_setting,
)

logger = logging.getLogger("CompanionPushNotifier")

#: event types that warrant a push wake (design doc §12.2 / §13)
_PUSH_EVENT_TYPES = frozenset({"message"})

#: preview strings are truncated so an opted-in `preview` push never carries a
#: whole message.
_PREVIEW_MAX_CHARS = 140

#: per-pass cap on message texts kept for exact mention matching. If a burst
#: exceeds it, mention-enabled devices receive the same content-free generic
#: alert conservatively; bounded memory never creates a false negative.
_MAX_MATCH_TEXTS = 64

#: how long a resolved companion node_name (the default mention trigger) is
#: cached before a re-read picks up a rename.
_NODE_NAME_TTL = 300.0

#: content-free mention alert body (design doc §11.4 / §12.2): the fact of a
#: mention, never the message text.
_MENTION_ALERT = "You were mentioned"


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Do not forward push credentials across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _build_post_opener():
    """Build a relay client that never inherits ambient proxy settings."""

    return urllib_request.build_opener(
        urllib_request.ProxyHandler({}),
        _NoRedirectHandler,
    )


_POST_OPENER = _build_post_opener()


def validate_relay_url(url: Optional[str], *, allow_insecure_http: bool = False) -> Optional[str]:
    """Validate one operator-controlled relay endpoint."""

    if url is None or not str(url).strip():
        return None
    value = str(url).strip()
    parsed = urllib_parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("push relay URL has an invalid port") from exc
    allowed_schemes = ("https", "http") if allow_insecure_http else ("https",)
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or port is not None
        and not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        requirement = "http(s)" if allow_insecure_http else "https"
        raise ValueError(f"push relay URL must be an absolute {requirement} URL")
    return value


class _CompanionAccum:
    """Accumulated new-event info for one companion since the last worker pass."""

    __slots__ = (
        "count",
        "preview",
        "preview_order",
        "newest_order",
        "texts",
        "mention_overflow",
    )

    def __init__(self) -> None:
        self.count = 0
        self.preview: Optional[str] = None
        self.preview_order = 0
        self.newest_order = 0
        # Raw message texts, for per-device mention matching in the worker.
        self.texts: List[str] = []
        self.mention_overflow = False


class _DevicePending:
    """A device awaiting a (trailing-edge) push, with backoff bookkeeping."""

    __slots__ = (
        "companion_hash",
        "companion_identity",
        "count",
        "preview",
        "preview_order",
        "newest_order",
        "mention",
        "attempts",
        "not_before",
    )

    def __init__(
        self,
        companion_hash: str,
        companion_identity: str,
    ) -> None:
        self.companion_hash = companion_hash
        self.companion_identity = companion_identity
        self.count = 0
        self.preview: Optional[str] = None
        self.preview_order = 0
        self.newest_order = 0
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
        with _POST_OPENER.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib_error.HTTPError as exc:
        with exc:
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
        relay_url: Optional[str] = None,
        allow_insecure_http: bool = False,
        worker_count: int = 2,
        clock: Callable[[], float] = time.monotonic,
        poster: Optional[Callable[[str, dict, float], int]] = None,
    ) -> None:
        self.sqlite_handler = sqlite_handler
        self.min_interval = validate_companion_seconds_setting(
            min_interval,
            "min_interval",
            minimum=0.0,
            maximum=MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
        )
        self.request_timeout = validate_companion_seconds_setting(
            request_timeout,
            "request_timeout",
            minimum=0.1,
            maximum=MAX_COMPANION_PUSH_REQUEST_TIMEOUT_SEC,
        )
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.max_attempts = max_attempts
        self.backoff_base = validate_companion_seconds_setting(
            backoff_base,
            "backoff_base",
            minimum=1.0,
            maximum=MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
        )
        self.backoff_cap = validate_companion_seconds_setting(
            backoff_cap,
            "backoff_cap",
            minimum=1.0,
            maximum=MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
        )
        self.enabled = validate_companion_boolean_setting(enabled, "enabled")
        allow_insecure_http = validate_companion_boolean_setting(
            allow_insecure_http,
            "allow_insecure_http",
        )
        self.relay_url = validate_relay_url(
            relay_url,
            allow_insecure_http=allow_insecure_http,
        )
        if type(worker_count) is not int:
            raise ValueError("worker_count must be an integer")
        self.worker_count = max(1, min(worker_count, 4))
        self._clock = clock
        self._poster = poster or _default_poster

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._dirty: Dict[object, _CompanionAccum] = {}
        self._device_pending: Dict[str, _DevicePending] = {}
        self._device_cooldown: Dict[str, float] = {}
        # Set only by make_listener after full-key validation. Internal event
        # calls without a registered active identity fail closed.
        self._active_identities: Dict[str, str] = {}
        # companion_hash -> (node_name, fetched_at); the default mention trigger.
        self._node_name_cache: Dict[str, tuple] = {}
        self._event_order = 0
        self._stop = False
        self._worker: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        # ThreadPoolExecutor's private work queue is unbounded.  A small
        # semaphore keeps a large paired-device fleet from turning one event
        # burst into an unbounded backlog of push credentials and payloads.
        self._submit_slots = threading.BoundedSemaphore(self.worker_count * 2)

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        if self.relay_url is None:
            logger.info("Companion push notifier disabled: no relay_url configured")
            return
        self._stop = False
        self._executor = ThreadPoolExecutor(
            max_workers=self.worker_count,
            thread_name_prefix="companion-push",
        )
        self._worker = threading.Thread(
            target=self._run, name="companion-push-notifier", daemon=True
        )
        self._worker.start()
        logger.info(
            "Companion push notifier started (min_interval=%ss, workers=%s)",
            self.min_interval,
            self.worker_count,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop accepting events and return only after push work is quiescent.

        ``timeout`` is a warning threshold for the coordinator thread, not
        permission to leave it running. Relay requests are bounded by
        ``request_timeout``; waiting for the executor ensures no credential or
        preview remains in flight after this method reports success.
        """

        with self._cv:
            self._stop = True
            self._dirty.clear()
            self._device_pending.clear()
            self._device_cooldown.clear()
            self._active_identities.clear()
            self._node_name_cache.clear()
            self._cv.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
            if worker.is_alive():
                logger.warning(
                    "Companion push coordinator exceeded %.1fs stop grace; waiting",
                    timeout,
                )
                worker.join()
        self._worker = None
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._cv:
            # An in-flight worker may have reached a failure callback just as
            # stop began. Requeue helpers reject `_stop`, and this final purge
            # makes the postcondition explicit.
            self._dirty.clear()
            self._device_pending.clear()
            self._device_cooldown.clear()
        self._submit_slots = threading.BoundedSemaphore(self.worker_count * 2)

    # -----------------------------------------------------------------
    # Journal listener
    # -----------------------------------------------------------------

    def make_listener(
        self,
        companion_hash: str,
        companion_identity: str,
    ) -> Callable[[dict], None]:
        """Return a listener bound to one hash and full public identity.

        Register the result on that companion's ``CompanionEventJournal`` (the
        same ``register_listener`` hook SSE uses). The callback is intentionally
        trivial — no DB, no network — so it never slows a journal append.
        """

        active_key, active_identity = self._normalize_active_identity(
            companion_hash,
            companion_identity,
        )
        with self._cv:
            existing = self._active_identities.get(active_key)
            if existing is not None and existing != active_identity:
                raise ValueError(
                    f"companion hash {active_key} is already active for another "
                    "public identity"
                )
            self._active_identities[active_key] = active_identity

        def _listener(event: dict) -> None:
            self._on_event(
                active_key,
                event,
                companion_identity=active_identity,
            )

        return _listener

    @staticmethod
    def _normalize_active_identity(
        companion_hash: str,
        companion_identity: str,
    ) -> tuple[str, str]:
        """Return one canonical ``(0xhh, full_identity)`` pair."""

        active_identity = str(companion_identity).strip().lower()
        try:
            valid_identity = (
                len(active_identity) == 64
                and len(bytes.fromhex(active_identity)) == 32
            )
        except ValueError:
            valid_identity = False
        if not valid_identity:
            raise ValueError("companion_identity must be a 32-byte public key in hex")
        active_hash = str(companion_hash).strip().lower()
        hash_hex = active_hash[2:] if active_hash.startswith("0x") else active_hash
        if (
            len(hash_hex) != 2
            or active_identity[:2] != hash_hex
        ):
            raise ValueError(
                "companion_identity does not belong to the requested companion_hash"
            )
        return f"0x{hash_hex}", active_identity

    def deactivate(
        self,
        companion_hash: str,
        companion_identity: str,
    ) -> bool:
        """Deactivate one exact listener identity and discard its queued wakes."""

        active_key, active_identity = self._normalize_active_identity(
            companion_hash,
            companion_identity,
        )
        with self._cv:
            if self._active_identities.get(active_key) != active_identity:
                return False
            del self._active_identities[active_key]
            self._dirty.pop((active_key, active_identity), None)
            self._node_name_cache.pop(active_key, None)
            removed_devices = [
                device_id
                for device_id, pending in self._device_pending.items()
                if pending.companion_hash == active_key
                and pending.companion_identity == active_identity
            ]
            for device_id in removed_devices:
                self._device_pending.pop(device_id, None)
                self._device_cooldown.pop(device_id, None)
            self._cv.notify_all()
            return True

    def _on_event(
        self,
        companion_hash: str,
        event: dict,
        *,
        companion_identity: str,
    ) -> None:
        if not self.enabled or self.relay_url is None:
            return
        active_key, event_identity = self._normalize_active_identity(
            companion_hash,
            companion_identity,
        )
        with self._cv:
            if self._stop:
                return
            registered_identity = self._active_identities.get(active_key)
            if registered_identity is None or not secrets.compare_digest(
                event_identity,
                registered_identity,
            ):
                return
            companion_identity = registered_identity
        if event.get("event_type") not in _PUSH_EVENT_TYPES:
            return
        payload = event.get("payload") or {}
        # Outbound messages are already known to the sending chat client and
        # may also originate from the parallel frame client.  Waking every
        # paired device for those local sends is noisy and can reveal activity
        # unnecessarily.  Legacy message events have no direction and retain
        # their historical inbound treatment.
        if payload.get("direction") == "out":
            return
        preview = self._extract_preview(event)
        text = payload.get("text")
        with self._cv:
            if (
                self._stop
                or self._active_identities.get(active_key)
                != companion_identity
            ):
                return
            self._event_order += 1
            event_order = self._event_order
            # Keep the full active identity attached through every fan-out
            # and delivery check; an eight-bit hash alone is never ownership.
            dirty_key = (active_key, companion_identity)
            accum = self._dirty.get(dirty_key)
            if accum is None:
                accum = _CompanionAccum()
                self._dirty[dirty_key] = accum
            accum.count += 1
            accum.newest_order = event_order
            if preview is not None:
                accum.preview = preview
                accum.preview_order = event_order
            if isinstance(text, str) and text.strip():
                if len(accum.texts) < _MAX_MATCH_TEXTS:
                    accum.texts.append(text)
                else:
                    accum.mention_overflow = True
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
            if not isinstance(parsed, list):
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
        raw_node_name = prefs.get("node_name") if isinstance(prefs, dict) else None
        node_name = raw_node_name.strip() if isinstance(raw_node_name, str) else ""
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
                try:
                    self._expand_dirty(dirty)
                except Exception:
                    # A malformed legacy row must not permanently disable all
                    # later push wakes. The batch is dropped and the
                    # coordinator stays alive for the next journal event.
                    logger.exception("push notifier: unexpected fan-out error")

            with self._cv:
                if self._stop:
                    return
            due, next_delay = self._collect_due()
            for device_id, pending in due:
                executor = self._executor
                if executor is not None:
                    if not self._submit_slots.acquire(blocking=False):
                        self._defer(device_id, pending)
                        continue
                    try:
                        executor.submit(self._send_one_releasing, device_id, pending)
                    except Exception:
                        self._submit_slots.release()
                        self._defer(device_id, pending)
                        logger.exception("push notifier: could not queue %s", device_id)

            if not due:
                # Nothing was due this pass: sleep until the soonest pending
                # device is due (or until a new event notifies us).
                with self._cv:
                    if self._stop:
                        return
                    if not self._dirty:
                        self._cv.wait(timeout=next_delay)

    def _expand_dirty(self, dirty: Dict[object, _CompanionAccum]) -> None:
        """Turn dirty companions into per-device pending entries (DB query
        outside the append hot path)."""
        for dirty_key, accum in dirty.items():
            if not isinstance(dirty_key, tuple) or len(dirty_key) != 2:
                logger.warning("push notifier: discarded unbound companion event")
                continue
            companion_hash, companion_identity = dirty_key
            try:
                devices = self.sqlite_handler.companion_devices_with_push(
                    companion_hash,
                    companion_identity,
                )
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
                        mention_hits[device["device_id"]] = bool(triggers) and (
                            accum.mention_overflow
                            or self._any_mention(accum.texts, triggers)
                        )
            with self._cv:
                if (
                    self._stop
                    or self._active_identities.get(companion_hash)
                    != companion_identity
                ):
                    continue
                for device in devices:
                    device_id = device["device_id"]
                    pending = self._device_pending.get(device_id)
                    if (
                        pending is None
                        or pending.companion_hash != companion_hash
                        or pending.companion_identity != companion_identity
                    ):
                        # Do not carry an old identity's preview or mention bit
                        # into a different identity's pending wake.
                        pending = _DevicePending(
                            companion_hash,
                            companion_identity,
                        )
                        self._device_pending[device_id] = pending
                    pending.count += accum.count
                    pending.newest_order = max(
                        pending.newest_order,
                        accum.newest_order,
                    )
                    if (
                        accum.preview is not None
                        and accum.preview_order >= pending.preview_order
                    ):
                        pending.preview = accum.preview
                        pending.preview_order = accum.preview_order
                    if mention_hits.get(device_id):
                        pending.mention = True

    def _collect_due(self):
        """Return ((device_id, pending), …) that are due to send now, and the
        delay until the next not-yet-due device (or None)."""
        now = self._clock()
        due = []
        next_delay: Optional[float] = None
        with self._cv:
            # A cooldown has no purpose after its interval expires. Removing
            # it here keeps repeated pair/revoke churn from retaining device
            # identifiers forever while preserving the exact debounce rule.
            expired_cooldowns = [
                device_id
                for device_id, sent_at in self._device_cooldown.items()
                if now >= sent_at + self.min_interval
            ]
            for device_id in expired_cooldowns:
                self._device_cooldown.pop(device_id, None)

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

    def _send_one_releasing(self, device_id: str, pending: _DevicePending) -> None:
        try:
            self._send_one(device_id, pending)
        except Exception:
            # Executor futures are intentionally fire-and-forget. Log here so
            # an unexpected legacy/corrupt row cannot fail invisibly.
            logger.exception(
                "push notifier: unexpected delivery error for %s",
                device_id,
            )
        finally:
            self._submit_slots.release()

    def _defer(self, device_id: str, pending: _DevicePending) -> None:
        """Put unscheduled work back without counting it as a relay attempt."""

        pending.not_before = max(pending.not_before, self._clock() + 0.1)
        with self._cv:
            if self._stop:
                return
            self._device_cooldown.pop(device_id, None)
            existing = self._device_pending.get(device_id)
            if (
                existing is None
                or existing.companion_hash != pending.companion_hash
                or existing.companion_identity != pending.companion_identity
            ):
                self._device_pending[device_id] = pending
            else:
                self._merge_pending(existing, pending)
            self._cv.notify()

    @staticmethod
    def _merge_pending(target: _DevicePending, other: _DevicePending) -> None:
        """Merge two deliveries while retaining the latest event preview."""

        target.count += other.count
        target.newest_order = max(target.newest_order, other.newest_order)
        if (
            other.preview is not None
            and other.preview_order > target.preview_order
        ):
            target.preview = other.preview
            target.preview_order = other.preview_order
        target.mention = target.mention or other.mention
        target.attempts = max(target.attempts, other.attempts)
        target.not_before = max(target.not_before, other.not_before)

    def _send_one(self, device_id: str, pending: _DevicePending) -> None:
        with self._cv:
            if (
                self._stop
                or pending.companion_identity is None
                or self._active_identities.get(pending.companion_hash)
                != pending.companion_identity
            ):
                return
        # Re-read the device so a token cleared/refreshed since the event is
        # honoured, and we never send to a stale token.
        try:
            device = self.sqlite_handler.companion_device_get(device_id)
        except Exception:
            logger.exception("push notifier: device re-read failed for %s", device_id)
            return
        if device is None or not device.get("push_token"):
            return  # unregistered since the event was queued: drop silently.
        if device.get("companion_hash") != pending.companion_hash:
            return
        if (
            str(device.get("companion_identity") or "").strip().lower()
            != pending.companion_identity
        ):
            # Re-check immediately before building the relay payload. This
            # closes the queue-to-send window if storage changes after fan-out.
            return
        relay_url = self.relay_url
        if not relay_url:
            return

        push_token = device["push_token"]
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
            # The relay rejected the token we sent.  Clear that exact token,
            # but preserve a replacement registered while the request was in
            # flight.
            try:
                cleared = (
                    self.sqlite_handler.companion_device_clear_push_if_token_strict(
                        device_id,
                        push_token,
                        device["companion_hash"],
                        device["companion_identity"],
                    )
                )
            except Exception:
                logger.exception(
                    "push notifier: relay 410 for %s but push_token could not be cleared",
                    device_id,
                )
                return
            if cleared:
                logger.info(
                    "push notifier: relay 410 for %s; cleared push_token",
                    device_id,
                )
            else:
                logger.info(
                    "push notifier: relay 410 for %s; token changed before cleanup",
                    device_id,
                )
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
        token's shape. It is an optional, bounded operator/client label rather
        than an enum, so the relay must still cope with it being absent or
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
        try:
            delay = min(self.backoff_base ** pending.attempts, self.backoff_cap)
        except OverflowError:
            delay = self.backoff_cap
        pending.not_before = self._clock() + delay
        # The cooldown was already stamped when this send was dequeued; clear
        # it so the backoff delay (not the min-interval) governs the retry.
        with self._cv:
            if self._stop:
                return
            self._device_cooldown.pop(device_id, None)
            existing = self._device_pending.get(device_id)
            if (
                existing is None
                or existing.companion_hash != pending.companion_hash
                or existing.companion_identity != pending.companion_identity
            ):
                self._device_pending[device_id] = pending
            else:
                # A newer event may have arrived while this older request was
                # in flight. Preserve it and merge the failed work into it.
                self._merge_pending(existing, pending)
            self._cv.notify()
        logger.debug(
            "push notifier: requeued %s in %.1fs (attempt %d, %s)",
            device_id,
            delay,
            pending.attempts,
            reason,
        )
