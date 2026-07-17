"""
Event journal writer for the Mobile Companion API (phase 1).

``CompanionEventJournal`` is a thin, per-companion wrapper around
``sqlite_handler.companion_append_event`` that:

- shapes the JSON payload for each event type (message/contact/prefs),
  reusing :func:`repeater.companion.bridge._to_json_safe` so bytes fields
  round-trip the same way prefs persistence already does;
- never raises — a failed append is logged and returns ``None``, matching
  the storage layer's own failure contract;
- supports in-process listeners for the future SSE phase (design doc §8):
  every successful append notifies registered listeners with the event
  that was just written.

See docs/architecture/mobile-companion-api.md §5 (journal) and §9 (event
schema). Append methods are intentionally synchronous/blocking-DB-call
style — callers (frame_server.py) run them via ``asyncio.to_thread``, the
same pattern already used for the other SQLite persistence hooks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from repeater.companion.bridge import _to_json_safe

logger = logging.getLogger("CompanionEventJournal")


class CompanionEventJournal:
    """Appends companion-scoped events to the SQLite event journal."""

    def __init__(self, sqlite_handler, companion_hash: str) -> None:
        self.sqlite_handler = sqlite_handler
        self.companion_hash = companion_hash
        self._listeners: List[Callable[[dict], None]] = []
        self._listeners_lock = threading.Lock()

    # -----------------------------------------------------------------
    # Listener support (future SSE phase)
    # -----------------------------------------------------------------

    def register_listener(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked after every successful append.

        Exceptions raised by ``fn`` are caught and logged; they never
        propagate to the caller of the append method that triggered them,
        and they never prevent other listeners from running.
        """
        with self._listeners_lock:
            self._listeners.append(fn)

    def _notify(self, seq: int, event_type: str, payload: dict) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        event = {
            "seq": seq,
            "event_type": event_type,
            "created_at": time.time(),
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

    # -----------------------------------------------------------------
    # Append helpers
    # -----------------------------------------------------------------

    def _append(
        self,
        event_type: str,
        payload: dict,
        packet_hash: Optional[str] = None,
    ) -> Optional[int]:
        try:
            seq = self.sqlite_handler.companion_append_event(
                self.companion_hash,
                event_type,
                payload,
                packet_hash=packet_hash,
            )
        except Exception:
            logger.exception(
                "Failed to append %s event for companion %s",
                event_type,
                self.companion_hash,
            )
            return None
        if seq is not None:
            self._notify(seq, event_type, payload)
        return seq

    def record_message(self, msg_dict: dict) -> Optional[int]:
        """Journal a persisted message (design doc §9: ``type: message``)."""
        payload = _to_json_safe(dict(msg_dict))
        packet_hash = msg_dict.get("packet_hash")
        if isinstance(packet_hash, bytes):
            packet_hash = packet_hash.hex()
        return self._append("message", payload, packet_hash=packet_hash)

    def record_contact(self, contact_dict: dict, change: str = "update") -> Optional[int]:
        """Journal a contact add/update/removal (design doc §9: ``type: contact``)."""
        payload = _to_json_safe(dict(contact_dict))
        payload["change"] = change
        return self._append("contact", payload)

    def record_prefs(self, fields: dict) -> Optional[int]:
        """Journal a node-prefs change (design doc §9: ``type: prefs``)."""
        payload = _to_json_safe(dict(fields))
        return self._append("prefs", payload)

    @property
    def epoch(self) -> str:
        """Current journal epoch (design doc §5.3), delegated to storage."""
        return self.sqlite_handler.companion_journal_epoch()
