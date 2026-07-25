"""
Live RF correlation for the Mobile Companion API (design doc §10.4).

``CompanionCorrelationTracker`` is a small in-memory TTL multimap from a
companion message's 16-char ``packet_hash`` (the same truncated form stored
in ``packets.packet_hash`` / ``companion_messages.packet_hash``) to every
companion + message it belongs to. The engine's duplicate handlers
(``repeater/engine.py``: ``record_duplicate`` and the ``is_dupe`` branch of
``__call__``) consult it on every duplicate reception; a hit means "this OTA
copy is a repeat of a known companion message or a companion's own recent
send", which is what makes ``message_reception`` / ``message_send_state``
journal events (§9) possible without scanning ``packets``.

Process-wide, not per-companion: one dedup cache (``RepeaterHandler.
seen_packets``) backs every companion sharing this repeater's radio, and a
duplicate reception arrives keyed only by ``packet_hash`` — it doesn't know
which companion cares until the tracker tells it. A single tracker instance,
built once in main.py with the same TTL as ``seen_packets``, keeps the
correlation window from outliving the dedup window it rides on.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any, Callable, Optional

logger = logging.getLogger("CompanionCorrelation")

# Set by the caller (mobile_endpoints._send_message) inside the coroutine
# that awaits a bridge send, so RepeaterCompanionBridge._send_packet can
# publish the packet_hash it computed back to that specific request without
# a global — contextvars scope the value to the awaiting call chain, so two
# concurrent sends never see each other's holder.
outbound_send_capture: ContextVar[Optional[dict]] = ContextVar(
    "outbound_send_capture", default=None
)

# Set only while the bridge awaits its packet injector.  Packet objects are
# slotted in openhop-core, so transport adapters cannot safely attach ad-hoc
# attributes to communicate "RF accepted, later ACK/echo work failed".  This
# task-local holder preserves the injector's bool API and cannot leak between
# concurrent Frame, REST, or operator sends.
injected_tx_outcome: ContextVar[Optional[dict]] = ContextVar(
    "injected_tx_outcome",
    default=None,
)


async def await_to_thread_outcome(
    function: Callable[..., Any],
    *args: Any,
) -> tuple[Any, Optional[asyncio.CancelledError]]:
    """Finish a started local worker and report cancellation after its result.

    ``asyncio.to_thread`` does not stop its worker when the awaiting task is
    cancelled.  Callers that must reconcile an SQLite commit therefore need
    the real worker outcome before propagating ``CancelledError``.  A storage
    exception wins over cancellation because it is the authoritative outcome.
    """

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancellation = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            # ``shield`` keeps the worker alive.  Keep waiting even if the
            # caller is cancelled more than once; once the worker finishes,
            # ``task.result()`` either returns the committed result or raises
            # the authoritative storage error.
            if cancellation is None:
                cancellation = exc
            if task.done():
                return task.result(), cancellation


def _truncate(packet_hash) -> Optional[str]:
    """Normalize any hash representation to the canonical 16-char uppercase key.

    Tolerates ``None``/empty input (no-op) so callers don't need their own
    guard before registering a possibly-absent hash.
    """
    if not packet_hash:
        return None
    if isinstance(packet_hash, (bytes, bytearray)):
        packet_hash = packet_hash.hex()
    packet_hash = str(packet_hash).strip().upper()
    if not packet_hash:
        return None
    return packet_hash[:16]


class _Entry:
    """One tracked companion message/send under a correlation hash."""

    __slots__ = (
        "registration_token",
        "packet_hash",
        "companion_hash",
        "direction",
        "message_id",
        "created",
        "paths",
        "terminal_hashes",
        "observation_count",
        "unique_path_count",
        "heard_repeat_count",
        "unique_repeater_count",
        "initial_hit",
        "pending_hit",
        "pending_generation",
    )

    def __init__(
        self,
        registration_token: int,
        packet_hash: str,
        companion_hash: str,
        direction: str,
        message_id: Optional[int],
        created: float,
        initial_hit: Optional[dict] = None,
    ):
        self.registration_token = int(registration_token)
        self.packet_hash = packet_hash
        self.companion_hash = companion_hash
        self.direction = direction  # "in" | "out"
        self.message_id = message_id
        self.created = created
        self.paths: set = set()
        self.terminal_hashes: set = set()
        # The original reception/send already counts as one observation of
        # one path — duplicates only ever add to these, never start them at 0.
        self.observation_count = 1
        self.unique_path_count = 1
        self.heard_repeat_count = 0
        self.unique_repeater_count = 0
        self.initial_hit = dict(initial_hit) if initial_hit is not None else None
        # Before the durable message row exists, retain one bounded aggregate
        # of all duplicate observations. Running counters above preserve the
        # complete count; this dict preserves the latest RF detail.
        self.pending_hit: Optional[dict] = None
        self.pending_generation = 0


class CompanionCorrelationTracker:
    """Bounded TTL map correlating duplicate RF receptions to companion messages.

    Thread-safe: registrations arrive from asyncio tasks (bridge sends),
    request threads (frame_server persistence via ``asyncio.to_thread``), and
    the dispatcher/engine's own thread(s) for ``observe_duplicate`` — all
    guarded by one lock. Mirrors ``RepeaterHandler.seen_packets``' bounded
    ``OrderedDict`` approach: insertion order doubles as an approximate
    recency order for opportunistic pruning, plus a hard size cap.
    """

    def __init__(self, ttl_seconds: float, max_size: int = 1000):
        self._ttl = max(1.0, float(ttl_seconds))
        self._max_size = max(1, int(max_size))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[int, _Entry]" = OrderedDict()
        self._tokens_by_hash: dict[str, dict[int, None]] = {}
        self._next_token = 0
        self._reported_ambiguities: set[tuple[str, str, str]] = set()

    def _next_token_locked(self) -> int:
        self._next_token += 1
        return self._next_token

    def new_registration_token(self) -> int:
        """Reserve one process-wide opaque token for an upcoming message send."""

        with self._lock:
            return self._next_token_locked()

    def _group_size_locked(self, group: tuple[str, str, str]) -> int:
        packet_hash, companion_hash, direction = group
        return sum(
            1
            for token in self._tokens_by_hash.get(packet_hash, ())
            if (entry := self._entries.get(token)) is not None
            and entry.companion_hash == companion_hash
            and entry.direction == direction
        )

    def _remove_locked(self, registration_token: int) -> None:
        entry = self._entries.pop(int(registration_token), None)
        if entry is None:
            return
        tokens = self._tokens_by_hash.get(entry.packet_hash)
        if tokens is None:
            return
        tokens.pop(entry.registration_token, None)
        if not tokens:
            self._tokens_by_hash.pop(entry.packet_hash, None)
        group = (entry.packet_hash, entry.companion_hash, entry.direction)
        if self._group_size_locked(group) <= 1:
            self._reported_ambiguities.discard(group)
        remaining_directions = {
            candidate.direction
            for token in self._tokens_by_hash.get(entry.packet_hash, ())
            if (candidate := self._entries.get(token)) is not None
            and candidate.companion_hash == entry.companion_hash
        }
        if len(remaining_directions) <= 1:
            self._reported_ambiguities.discard(
                (entry.packet_hash, entry.companion_hash, "mixed")
            )

    def _prune_locked(self, now: float) -> None:
        while self._entries:
            registration_token = next(iter(self._entries))
            entry = self._entries[registration_token]
            if now - entry.created > self._ttl:
                self._remove_locked(registration_token)
            else:
                break
        while len(self._entries) > self._max_size:
            self._remove_locked(next(iter(self._entries)))

    def _register(
        self,
        packet_hash,
        companion_hash: str,
        direction: str,
        message_id: Optional[int],
        *,
        registration_token: Optional[int] = None,
        initial_hit: Optional[dict] = None,
    ) -> Optional[int]:
        key = _truncate(packet_hash)
        if not key or not companion_hash:
            return None
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            token = (
                self._next_token_locked()
                if registration_token is None
                else int(registration_token)
            )
            self._next_token = max(self._next_token, token)
            self._remove_locked(token)
            self._entries[token] = _Entry(
                token,
                key,
                companion_hash,
                direction,
                message_id,
                now,
                initial_hit=initial_hit,
            )
            self._tokens_by_hash.setdefault(key, {})[token] = None
            self._prune_locked(now)
            return token

    def register_inbound(
        self,
        packet_hash,
        companion_hash: str,
        message_id: Optional[int],
        *,
        registration_token: Optional[int] = None,
        initial_hit: Optional[dict] = None,
    ) -> Optional[int]:
        """Register a persisted inbound companion message for reception correlation."""
        return self._register(
            packet_hash,
            companion_hash,
            "in",
            message_id,
            registration_token=registration_token,
            initial_hit=initial_hit,
        )

    def register_outbound(
        self,
        packet_hash,
        companion_hash: str,
        message_id: Optional[int] = None,
        *,
        registration_token: Optional[int] = None,
    ) -> Optional[int]:
        """Register a just-transmitted companion send for heard-repeat correlation."""
        return self._register(
            packet_hash,
            companion_hash,
            "out",
            message_id,
            registration_token=registration_token,
        )

    @staticmethod
    def _pending_snapshot(entry: _Entry) -> list[dict]:
        if entry.pending_hit is None or entry.message_id is None:
            return []
        hit = dict(entry.pending_hit)
        hit["message_id"] = int(entry.message_id)
        hit["_correlation_token"] = entry.registration_token
        hit["_correlation_generation"] = entry.pending_generation
        return [hit]

    @staticmethod
    def _set_pending_locked(entry: _Entry, hit: dict) -> None:
        entry.pending_generation += 1
        entry.pending_hit = dict(hit)

    def _promote(
        self,
        packet_hash,
        companion_hash: str,
        direction: str,
        message_id: int,
        *,
        registration_token: Optional[int] = None,
        existing_message: Optional[dict] = None,
    ) -> list[dict]:
        """Attach a durable id and expose, but do not clear, buffered evidence."""
        key = _truncate(packet_hash)
        if not key or not companion_hash:
            return []
        durable_id = int(message_id)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            if registration_token is None:
                candidates = [
                    token
                    for token in self._tokens_by_hash.get(key, ())
                    if (candidate := self._entries.get(token)) is not None
                    and candidate.companion_hash == companion_hash
                    and candidate.direction == direction
                    and candidate.message_id is None
                ]
                if len(candidates) != 1:
                    return []
                registration_token = candidates[0]
            entry = self._entries.get(int(registration_token))
            if (
                entry is None
                or entry.packet_hash != key
                or entry.companion_hash != companion_hash
                or entry.direction != direction
            ):
                # Eviction/expiry remains authoritative. Promotion never
                # resurrects an old registration with a fresh TTL.
                return []

            was_transient = entry.message_id is None

            if direction == "in" and existing_message is not None and was_transient:
                # A repeated receive can race the first persistence callback.
                # If this durable row is already tracked, fold the new
                # provisional reception into that exact row instead of
                # creating two durable candidates for one logical message.
                existing_entries = [
                    candidate
                    for token in self._tokens_by_hash.get(key, ())
                    if token != entry.registration_token
                    and (candidate := self._entries.get(token)) is not None
                    and candidate.companion_hash == companion_hash
                    and candidate.direction == direction
                    and candidate.message_id == durable_id
                ]
                if len(existing_entries) == 1:
                    target = existing_entries[0]
                    # This is a new accepted reception and therefore a new
                    # dedup/correlation window, even though history reuses the
                    # same durable row.  Keep cumulative counters but let the
                    # fresh provisional registration own the new TTL.
                    target.created = entry.created
                    self._entries.move_to_end(target.registration_token)
                    target.observation_count = max(
                        target.observation_count,
                        int(existing_message.get("observation_count") or 1),
                    ) + entry.observation_count
                    target.unique_path_count = max(
                        target.unique_path_count,
                        int(existing_message.get("unique_path_count") or 1),
                    ) + max(0, entry.unique_path_count - 1)
                    target.paths.update(entry.paths)
                    seed = dict(
                        entry.pending_hit
                        or entry.initial_hit
                        or target.pending_hit
                        or {}
                    )
                    seed.update(
                        {
                            "direction": "in",
                            "companion_hash": companion_hash,
                            "message_id": durable_id,
                            "packet_hash": key,
                            "path": seed.get("path") or [],
                            "rssi": seed.get("rssi"),
                            "snr": seed.get("snr"),
                            "observed_at": seed.get("observed_at", now),
                            "observation_count": target.observation_count,
                            "unique_path_count": target.unique_path_count,
                        }
                    )
                    self._set_pending_locked(target, seed)
                    self._remove_locked(entry.registration_token)
                    return self._pending_snapshot(target)
                if len(existing_entries) > 1:
                    # Existing ambiguity is safer left untouched.  Remove only
                    # the known replay registration so it cannot make the
                    # candidate set even less attributable.
                    self._remove_locked(entry.registration_token)
                    return []

            entry.message_id = durable_id

            if direction == "in" and existing_message is not None and was_transient:
                # A logical message already existed (normally after restart).
                # The provisional entry represents new OTA copies: its initial
                # reception plus every duplicate buffered during the lookup.
                base_observations = max(
                    1,
                    int(existing_message.get("observation_count") or 1),
                )
                base_unique_paths = max(
                    1,
                    int(existing_message.get("unique_path_count") or 1),
                )
                entry.observation_count = base_observations + entry.observation_count
                # The original full path set is unavailable after restart.
                # Preserve the durable base and add only distinct paths seen
                # among racing duplicates; the running value remains explicitly
                # approximate while bounded pull queries stay exact.
                entry.unique_path_count = base_unique_paths + max(
                    0,
                    entry.unique_path_count - 1,
                )
                seed = (
                    dict(entry.pending_hit)
                    if entry.pending_hit is not None
                    else dict(entry.initial_hit or {})
                )
                seed.update(
                    {
                        "direction": "in",
                        "companion_hash": companion_hash,
                        "message_id": durable_id,
                        "packet_hash": key,
                        "path": seed.get("path") or [],
                        "rssi": seed.get("rssi"),
                        "snr": seed.get("snr"),
                        "observed_at": seed.get("observed_at", now),
                        "observation_count": entry.observation_count,
                        "unique_path_count": entry.unique_path_count,
                    }
                )
                self._set_pending_locked(entry, seed)
            elif entry.pending_hit is not None:
                buffered = dict(entry.pending_hit)
                buffered["message_id"] = durable_id
                if direction == "in":
                    buffered["observation_count"] = entry.observation_count
                    buffered["unique_path_count"] = entry.unique_path_count
                else:
                    buffered["heard_repeat_count"] = entry.heard_repeat_count
                    buffered["unique_repeater_count"] = entry.unique_repeater_count
                self._set_pending_locked(entry, buffered)
            return self._pending_snapshot(entry)

    def promote_outbound(
        self,
        packet_hash,
        companion_hash: str,
        message_id: int,
        *,
        registration_token: Optional[int] = None,
    ) -> list[dict]:
        """Promote a sent message and expose its pre-persistence RF aggregate."""
        return self._promote(
            packet_hash,
            companion_hash,
            "out",
            message_id,
            registration_token=registration_token,
        )

    def promote_inbound(
        self,
        packet_hash,
        companion_hash: str,
        message_id: int,
        *,
        registration_token: Optional[int] = None,
        existing_message: Optional[dict] = None,
    ) -> list[dict]:
        """Promote a received message and expose its pre-persistence RF aggregate."""
        return self._promote(
            packet_hash,
            companion_hash,
            "in",
            message_id,
            registration_token=registration_token,
            existing_message=existing_message,
        )

    def acknowledge(self, correlation: dict) -> None:
        """Clear one pending generation only after its durable event commits."""

        token = correlation.get("_correlation_token")
        generation = correlation.get("_correlation_generation")
        if token is None or generation is None:
            return
        with self._lock:
            entry = self._entries.get(int(token))
            if (
                entry is not None
                and entry.pending_generation == int(generation)
            ):
                entry.pending_hit = None

    def discard_registration(self, registration_token: Optional[int]) -> None:
        if registration_token is None:
            return
        with self._lock:
            self._remove_locked(int(registration_token))

    def discard_inbound_pending(
        self,
        packet_hash,
        companion_hash: str,
        *,
        registration_token: Optional[int] = None,
    ) -> None:
        """Forget a provisional inbound registration that was not inserted."""
        key = _truncate(packet_hash)
        if not key or not companion_hash:
            return
        with self._lock:
            if registration_token is not None:
                entry = self._entries.get(int(registration_token))
                if (
                    entry is not None
                    and entry.packet_hash == key
                    and entry.companion_hash == companion_hash
                    and entry.direction == "in"
                    and entry.message_id is None
                ):
                    self._remove_locked(entry.registration_token)
                return
            for token in tuple(self._tokens_by_hash.get(key, ())):
                entry = self._entries.get(token)
                if (
                    entry is not None
                    and entry.companion_hash == companion_hash
                    and entry.direction == "in"
                    and entry.message_id is None
                ):
                    self._remove_locked(token)

    def observe_duplicate(self, packet_record: dict) -> list:
        """Look up a duplicate reception's ``packet_hash`` and update running counts.

        Cost on miss is one dict lookup (design doc §10.4's cost budget).
        Returns one correlation dict per interested companion (empty on miss
        or expiry). The same OTA packet is commonly delivered to multiple
        virtual companions sharing this radio, so a hash is intentionally a
        multimap key rather than last-writer-wins state. Hits whose durable
        message row is still being stored are aggregated internally and
        returned by the corresponding promotion method with a real ID.
        """
        key = _truncate(packet_record.get("packet_hash"))
        if not key:
            return []
        now = time.time()
        with self._lock:
            tokens = tuple(self._tokens_by_hash.get(key, ()))
            if not tokens:
                return []
            path = list(packet_record.get("original_path") or ())
            path_key = tuple(path)
            observed_at = packet_record.get("timestamp", now)
            rssi = packet_record.get("rssi")
            snr = packet_record.get("snr")
            groups: dict[str, list[_Entry]] = {}
            for token in tokens:
                entry = self._entries.get(token)
                if entry is None:
                    continue
                if now - entry.created > self._ttl:
                    self._remove_locked(token)
                    continue
                groups.setdefault(entry.companion_hash, []).append(entry)

            targets: list[_Entry] = []
            for companion_hash, entries in groups.items():
                directions = {entry.direction for entry in entries}
                if len(directions) != 1:
                    ambiguity = (key, companion_hash, "mixed")
                    if ambiguity not in self._reported_ambiguities:
                        self._reported_ambiguities.add(ambiguity)
                        logger.warning(
                            "Ambiguous companion RF correlation suppressed "
                            "(companion=%s direction=mixed packet_hash=%s "
                            "candidates=%d)",
                            companion_hash,
                            key,
                            len(entries),
                        )
                    continue
                direction = next(iter(directions))
                if direction == "in":
                    durable = [entry for entry in entries if entry.message_id is not None]
                    if len(durable) == 1:
                        # A repeated persistence callback may temporarily add
                        # a provisional entry beside the already-known row.
                        # Route duplicate RF evidence to the durable row only.
                        targets.extend(durable)
                        continue
                    if len(durable) == 0 and len(entries) == 1:
                        targets.extend(entries)
                        continue
                elif len(entries) == 1:
                    targets.extend(entries)
                    continue

                ambiguity = (key, companion_hash, direction)
                if ambiguity not in self._reported_ambiguities:
                    self._reported_ambiguities.add(ambiguity)
                    logger.warning(
                        "Ambiguous companion RF correlation suppressed "
                        "(companion=%s direction=%s packet_hash=%s candidates=%d)",
                        companion_hash,
                        direction,
                        key,
                        len(entries),
                    )

            hits = []
            for entry in targets:
                if entry.direction == "in":
                    entry.observation_count += 1
                    # The original reception's path bytes never reach the
                    # tracker, so a same-path duplicate can overcount unique
                    # paths by at most one. Pull endpoints remain exact.
                    if path_key not in entry.paths:
                        entry.paths.add(path_key)
                        entry.unique_path_count += 1
                    hit = {
                        "direction": "in",
                        "companion_hash": entry.companion_hash,
                        "message_id": entry.message_id,
                        "packet_hash": key,
                        "path": path,
                        "rssi": rssi,
                        "snr": snr,
                        "observed_at": observed_at,
                        "observation_count": entry.observation_count,
                        "unique_path_count": entry.unique_path_count,
                    }
                    self._set_pending_locked(entry, hit)
                    if entry.message_id is not None:
                        hits.extend(self._pending_snapshot(entry))
                    continue

                # direction == "out": a heard repeat of our own recent send.
                terminal_hash = path[-1] if path else None
                entry.heard_repeat_count += 1
                if (
                    terminal_hash is not None
                    and terminal_hash not in entry.terminal_hashes
                ):
                    entry.terminal_hashes.add(terminal_hash)
                    entry.unique_repeater_count += 1
                hit = {
                    "direction": "out",
                    "companion_hash": entry.companion_hash,
                    "message_id": entry.message_id,
                    "packet_hash": key,
                    "path": path,
                    "terminal_hash": terminal_hash,
                    "rssi": rssi,
                    "snr": snr,
                    "observed_at": observed_at,
                    "heard_repeat_count": entry.heard_repeat_count,
                    "unique_repeater_count": entry.unique_repeater_count,
                }
                self._set_pending_locked(entry, hit)
                if entry.message_id is not None:
                    hits.extend(self._pending_snapshot(entry))
            return hits

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
