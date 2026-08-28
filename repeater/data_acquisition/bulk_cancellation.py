"""Bounded, owner-scoped cancellation of bulk packet queries."""

import threading
import time
import uuid


class BulkQueryCancelled(RuntimeError):
    def __init__(self):
        super().__init__("Packet history request cancelled")


class BulkQueryConflict(RuntimeError):
    pass


class BulkQueryCapacity(RuntimeError):
    pass


def normalize_bulk_request_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("request_id must be a UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("request_id must be a UUIDv4") from None
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("request_id must be a UUIDv4")
    return str(parsed)


class BulkQueryRegistry:
    """Keep active events and short-lived cancel-before-start tombstones."""

    def __init__(self, max_entries=256, tombstone_seconds=120, clock=time.monotonic):
        self._lock = threading.Lock()
        self._entries = {}
        self._max_entries = max_entries
        self._tombstone_seconds = tombstone_seconds
        self._clock = clock

    def _prune(self):
        now = self._clock()
        for key, (_, expires) in list(self._entries.items()):
            if expires is not None and expires <= now:
                del self._entries[key]

    def _check_capacity(self):
        if len(self._entries) >= self._max_entries:
            raise BulkQueryCapacity("Too many pending packet history requests")

    def register(self, owner, request_id):
        key = (owner, request_id)
        with self._lock:
            self._prune()
            if key in self._entries:
                event, _ = self._entries[key]
                if event.is_set():
                    raise BulkQueryCancelled()
                raise BulkQueryConflict("Packet history request_id is already active")
            self._check_capacity()
            event = threading.Event()
            self._entries[key] = (event, None)
            return event

    def cancel(self, owner, request_id):
        key = (owner, request_id)
        with self._lock:
            self._prune()
            if key not in self._entries:
                self._check_capacity()
                self._entries[key] = (threading.Event(), self._clock() + self._tombstone_seconds)
            event, expires = self._entries[key]
            if expires is not None:
                self._entries[key] = (event, self._clock() + self._tombstone_seconds)
            event.set()

    def finish(self, owner, request_id, event):
        key = (owner, request_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] is not event:
                return
            if event.is_set():
                self._entries[key] = (event, self._clock() + self._tombstone_seconds)
            else:
                del self._entries[key]
