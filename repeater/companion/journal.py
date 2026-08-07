"""Event journal writer for the Mobile Companion API.

``CompanionEventJournal`` is a thin, per-companion wrapper around
the SQLite event/state helpers that:

- shape the JSON payload for each event type (message/contact/prefs),
  reusing :func:`repeater.companion.bridge._to_json_safe` so bytes fields
  round-trip the same way prefs persistence already does;
- atomically persist chat-visible state with its journal event where those
  must agree; these helpers surface storage errors so callers can roll back;
- notify in-process SSE and push listeners after every committed event.

The lower-level ``_append`` helper remains best-effort for observational
events: it logs a failed append and returns ``None``. State-coupled helpers
intentionally do not swallow persistence failures.

See docs/architecture/mobile-companion-api.md §§4–6. Methods are synchronous
because SQLite access is blocking; async callers move them off their event
loop where needed.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Callable, List, Optional

from repeater.companion.bridge import _to_json_safe

logger = logging.getLogger("CompanionEventJournal")


class CompanionEventJournal:
    """Appends companion-scoped events to the SQLite event journal."""

    # Journals for the same database/companion must serialize state+event
    # transactions, but hot-reloaded companions must not leave process-lifetime
    # lock entries behind after their last journal is gone.
    _append_locks = weakref.WeakValueDictionary()
    _append_locks_guard = threading.Lock()

    def __init__(self, sqlite_handler, companion_hash: str) -> None:
        self.sqlite_handler = sqlite_handler
        self.companion_hash = companion_hash
        self._listeners: List[Callable[[dict], None]] = []
        self._listeners_lock = threading.Lock()
        sqlite_path = getattr(sqlite_handler, "sqlite_path", None)
        database_key = str(sqlite_path) if sqlite_path is not None else str(id(sqlite_handler))
        lock_key = (database_key, companion_hash)
        with self._append_locks_guard:
            self._append_lock = self._append_locks.setdefault(lock_key, threading.RLock())

    # -----------------------------------------------------------------
    # Listener support (live SSE and push wakeups)
    # -----------------------------------------------------------------

    def register_listener(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked after every successful append.

        Exceptions raised by ``fn`` are caught and logged; they never
        propagate to the caller of the append method that triggered them,
        and they never prevent other listeners from running.
        """
        with self._listeners_lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def unregister_listener(self, fn: Callable[[dict], None]) -> None:
        """Remove a previously registered listener.

        A no-op if ``fn`` isn't registered, so disconnect cleanup can safely
        run from more than one error path.
        """
        with self._listeners_lock:
            try:
                self._listeners.remove(fn)
            except ValueError:
                pass

    def _notify(
        self,
        seq: int,
        event_type: str,
        payload: dict,
        created_at: float,
        packet_hash: Optional[str],
    ) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        event = {
            "seq": seq,
            "event_type": event_type,
            "created_at": created_at,
            "packet_hash": packet_hash,
            "payload": payload,
        }
        for fn in listeners:
            try:
                fn(event)
            except Exception:
                logger.exception(
                    "Companion event journal listener raised for seq=%s event_type=%s",
                    seq,
                    event_type,
                )

    def notify_committed(self, event: dict) -> None:
        """Notify listeners about an event already committed by a transaction helper."""
        self._notify(
            int(event["seq"]),
            str(event["event_type"]),
            dict(event.get("payload") or {}),
            float(event["created_at"]),
            event.get("packet_hash"),
        )

    # -----------------------------------------------------------------
    # Append helpers
    # -----------------------------------------------------------------

    def _append(
        self,
        event_type: str,
        payload: dict,
        packet_hash: Optional[str] = None,
    ) -> Optional[int]:
        # Captured once and passed through to the DB write so the row's
        # created_at and the notified event's created_at agree exactly. A
        # listener may fire before the caller reads the row back, and the two
        # must not drift.
        with self._append_lock:
            created_at = time.time()
            try:
                seq = self.sqlite_handler.companion_append_event(
                    self.companion_hash,
                    event_type,
                    payload,
                    packet_hash=packet_hash,
                    created_at=created_at,
                )
            except Exception:
                logger.exception(
                    "Failed to append %s event for companion %s",
                    event_type,
                    self.companion_hash,
                )
                return None
            if seq is not None:
                self._notify(seq, event_type, payload, created_at, packet_hash)
            return seq

    # -----------------------------------------------------------------
    # Atomic state + event helpers
    # -----------------------------------------------------------------

    def store_inbound_message(self, msg_dict: dict, max_pending: Optional[int] = None) -> dict:
        """Store inbound history and its event atomically, then notify."""
        with self._append_lock:
            result = self.sqlite_handler.companion_store_inbound_message(
                self.companion_hash,
                msg_dict,
                max_pending,
            )
            if result.get("event") is not None:
                self.notify_committed(result["event"])
            return result

    def store_outbound_message(
        self,
        msg_dict: dict,
        source: str,
        state: str = "pending",
    ) -> dict:
        """Create an outbound history row and event atomically, then notify."""
        with self._append_lock:
            result = self.sqlite_handler.companion_store_outbound_message(
                self.companion_hash,
                msg_dict,
                source,
                state,
            )
            self.notify_committed(result["event"])
            return result

    def reserve_outbound_send(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        msg_dict: dict,
        source: str = "rest",
    ) -> dict:
        """Reserve a retry key and create its outbound message atomically."""
        with self._append_lock:
            result = self.sqlite_handler.companion_reserve_outbound_send(
                self.companion_hash,
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                msg_dict,
                source,
            )
            if result.get("event") is not None:
                self.notify_committed(result["event"])
            return result

    def complete_outbound_send(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        message_state: str,
        response_json: str,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> dict:
        """Commit final history state and the replay response atomically."""
        with self._append_lock:
            result = self.sqlite_handler.companion_complete_outbound_send(
                self.companion_hash,
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                message_state,
                response_json,
                packet_hash,
                expected_ack,
            )
            if result.get("event") is not None:
                self.notify_committed(result["event"])
            return result

    def mark_outbound_send_indeterminate(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> dict:
        """Commit an ambiguous outcome to both linked records atomically."""
        with self._append_lock:
            result = self.sqlite_handler.companion_mark_outbound_send_indeterminate(
                self.companion_hash,
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                packet_hash,
                expected_ack,
            )
            if result.get("event") is not None:
                self.notify_committed(result["event"])
            return result

    def update_outbound_state(
        self,
        message_id: int,
        state: str,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> dict:
        """Update an outbound lifecycle row and event atomically, then notify."""
        with self._append_lock:
            result = self.sqlite_handler.companion_update_outbound_state(
                self.companion_hash,
                message_id,
                state,
                packet_hash=packet_hash,
                expected_ack=expected_ack,
            )
            if result.get("event") is not None:
                self.notify_committed(result["event"])
            return result

    def record_outbound_heard_repeat(self, correlation: dict) -> dict:
        """Advance a durable send and publish its detailed RF event atomically."""
        message_id = correlation.get("message_id")
        if message_id is None:
            raise ValueError("a durable message_id is required")
        with self._append_lock:
            result = self.sqlite_handler.companion_record_outbound_heard_repeat(
                self.companion_hash,
                int(message_id),
                correlation,
            )
            self.notify_committed(result["event"])
            return result

    def record_inbound_reception(self, correlation: dict) -> dict:
        """Advance durable receive counters and publish their RF event atomically."""
        message_id = correlation.get("message_id")
        if message_id is None:
            raise ValueError("a durable message_id is required")
        with self._append_lock:
            result = self.sqlite_handler.companion_record_inbound_reception(
                self.companion_hash,
                int(message_id),
                correlation,
            )
            self.notify_committed(result["event"])
            return result

    def store_contact(self, contact_dict: dict, change: str = "update") -> dict:
        """Persist a contact mutation and event atomically, then notify."""
        with self._append_lock:
            result = self.sqlite_handler.companion_upsert_contact_with_event(
                self.companion_hash,
                contact_dict,
                change,
            )
            self.notify_committed(result["event"])
            return result

    def remove_contact(self, pubkey: bytes) -> Optional[dict]:
        """Persist a contact removal and event atomically, then notify."""
        with self._append_lock:
            result = self.sqlite_handler.companion_delete_contact_with_event(
                self.companion_hash,
                pubkey,
            )
            if result is not None:
                self.notify_committed(result["event"])
            return result

    def apply_contact_changes(self, changes: list[dict]) -> dict:
        """Persist one complete contact diff, then publish committed events."""
        with self._append_lock:
            result = self.sqlite_handler.companion_apply_contact_changes(
                self.companion_hash,
                changes,
            )
            for event in result.get("events", ()):
                self.notify_committed(event)
            return result

    def store_channel(
        self,
        index: int,
        name: Optional[str],
        secret: Optional[bytes],
    ) -> dict:
        """Persist a channel mutation and secret-free event atomically."""
        with self._append_lock:
            result = self.sqlite_handler.companion_set_channel_with_event(
                self.companion_hash,
                index,
                name,
                secret,
            )
            self.notify_committed(result["event"])
            return result

    def store_prefs(self, prefs: dict, event_fields: dict) -> dict:
        """Persist prefs and explicitly public event fields atomically."""
        with self._append_lock:
            result = self.sqlite_handler.companion_save_prefs_with_event(
                self.companion_hash,
                prefs,
                event_fields,
            )
            self.notify_committed(result["event"])
            return result

    def record_message(self, msg_dict: dict) -> Optional[int]:
        """Journal a persisted message (architecture §6: ``type: message``)."""
        payload = _to_json_safe(dict(msg_dict))
        packet_hash = msg_dict.get("packet_hash")
        if isinstance(packet_hash, bytes):
            packet_hash = packet_hash.hex()
        return self._append("message", payload, packet_hash=packet_hash)

    def record_contact(self, contact_dict: dict, change: str = "update") -> Optional[int]:
        """Journal a contact add/update/removal (architecture §6: ``type: contact``)."""
        payload = _to_json_safe(dict(contact_dict))
        payload["change"] = change
        return self._append("contact", payload)

    def record_channel(
        self, index: int, name: Optional[str], change: str = "update"
    ) -> Optional[int]:
        """Journal a channel add/rename/removal (architecture §6: ``type: channel``).

        Deliberately carries only ``index`` and ``name`` — never the PSK
        secret. This event reaches mobile clients through ``/sync``, and the
        snapshot surface strips secrets for exactly that reason
        (``mobile_endpoints.snapshot``); journaling one here would leak it to
        every synced device and, unlike a snapshot field, it would persist in
        the journal table.

        ``change`` is ``update`` for an add-or-rename and ``remove`` when the
        slot was cleared.
        """
        payload = {"index": int(index), "name": name, "change": change}
        return self._append("channel", payload)

    def record_prefs(self, fields: dict) -> Optional[int]:
        """Journal a node-prefs change (architecture §6: ``type: prefs``)."""
        payload = _to_json_safe(dict(fields))
        return self._append("prefs", payload)

    def record_message_reception(self, correlation: dict) -> Optional[int]:
        """Compatibility wrapper for the durable, atomic reception helper."""
        return self.record_inbound_reception(correlation)["event_seq"]

    def record_send_state(self, correlation: dict) -> Optional[int]:
        """Compatibility wrapper for the durable, atomic heard-repeat helper."""
        return self.record_outbound_heard_repeat(correlation)["event_seq"]

    def record_rf_reception(self, packet_record: dict) -> Optional[int]:
        """Journal any packet heard again, regardless of companion relevance
        (architecture §6: ``type: rf_reception``, opt-in firehose).

        ``packet_record`` is the engine's duplicate-reception dict (the same
        shape ``RepeaterHandler``'s ``duplicate_observer`` hook receives for
        every genuine OTA duplicate). Path entries remain raw hashes in the
        journal, matching ``record_message_reception``.
        """
        packet_hash = packet_record.get("packet_hash")
        payload = {
            "packet_hash": packet_hash,
            "rssi": packet_record.get("rssi"),
            "snr": packet_record.get("snr"),
            "path": packet_record.get("original_path") or [],
            "observed_at": packet_record.get("timestamp"),
        }
        return self._append("rf_reception", payload, packet_hash=packet_hash)

    @property
    def epoch(self) -> str:
        """Current journal epoch (architecture §4.1), delegated to storage."""
        return self.sqlite_handler.companion_journal_epoch()
