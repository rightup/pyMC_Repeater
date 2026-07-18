"""
Live RF correlation for the Mobile Companion API (design doc §10.4).

``CompanionCorrelationTracker`` is a small in-memory TTL map from a
companion message's 16-char ``packet_hash`` (the same truncated form stored
in ``packets.packet_hash`` / ``companion_messages.packet_hash``) to the
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

import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from typing import Optional

# Set by the caller (mobile_endpoints._send_message) inside the coroutine
# that awaits a bridge send, so RepeaterCompanionBridge._send_packet can
# publish the packet_hash it computed back to that specific request without
# a global — contextvars scope the value to the awaiting call chain, so two
# concurrent sends never see each other's holder.
outbound_send_capture: ContextVar[Optional[dict]] = ContextVar(
    "outbound_send_capture", default=None
)


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
    """One tracked companion message/send, keyed by its correlation hash."""

    __slots__ = (
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
    )

    def __init__(self, companion_hash: str, direction: str, message_id: Optional[int], created: float):
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
        self._max_size = max_size
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    def _prune_locked(self, now: float) -> None:
        while self._entries:
            key = next(iter(self._entries))
            entry = self._entries[key]
            if now - entry.created > self._ttl:
                self._entries.popitem(last=False)
            else:
                break
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def _register(self, packet_hash, companion_hash: str, direction: str, message_id: Optional[int]) -> None:
        key = _truncate(packet_hash)
        if not key or not companion_hash:
            return
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._entries[key] = _Entry(companion_hash, direction, message_id, now)
            self._entries.move_to_end(key)
            self._prune_locked(now)

    def register_inbound(self, packet_hash, companion_hash: str, message_id: Optional[int]) -> None:
        """Register a persisted inbound companion message for reception correlation."""
        self._register(packet_hash, companion_hash, "in", message_id)

    def register_outbound(self, packet_hash, companion_hash: str) -> None:
        """Register a just-transmitted companion send for heard-repeat correlation."""
        self._register(packet_hash, companion_hash, "out", None)

    def observe_duplicate(self, packet_record: dict) -> list:
        """Look up a duplicate reception's ``packet_hash`` and update running counts.

        Cost on miss is one dict lookup (design doc §10.4's cost budget).
        Returns a list of correlation dicts for the caller to journal (empty
        on miss or expiry) — a list, not a single optional dict, so the
        signature never has to change if a hash is ever shared by more than
        one tracked entry.
        """
        key = _truncate(packet_record.get("packet_hash"))
        if not key:
            return []
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return []
            if now - entry.created > self._ttl:
                del self._entries[key]
                return []

            path = list(packet_record.get("original_path") or ())
            path_key = tuple(path)
            observed_at = packet_record.get("timestamp", now)
            rssi = packet_record.get("rssi")
            snr = packet_record.get("snr")

            if entry.direction == "in":
                entry.observation_count += 1
                # The original reception's path bytes never reach the tracker
                # (companion_messages keeps only path_len), so a duplicate
                # arriving on the SAME path as the original still counts as a
                # new unique path — an overcount of at most 1. Live cue only;
                # the §10 pull endpoints compute exact counts from `packets`.
                if path_key not in entry.paths:
                    entry.paths.add(path_key)
                    entry.unique_path_count += 1
                return [
                    {
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
                ]

            # direction == "out": a heard repeat of our own recent send.
            terminal_hash = path[-1] if path else None
            entry.heard_repeat_count += 1
            if terminal_hash is not None and terminal_hash not in entry.terminal_hashes:
                entry.terminal_hashes.add(terminal_hash)
                entry.unique_repeater_count += 1
            return [
                {
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
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
