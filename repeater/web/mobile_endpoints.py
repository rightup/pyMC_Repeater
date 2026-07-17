"""
Mobile Companion API v1 endpoints (phase 1: journal-backed sync core).

Mounted as ``APIEndpoints.v1`` so CherryPy serves it at ``/api/v1/``.
Implements the read-only synchronization surface from
docs/architecture/mobile-companion-api.md §7:

- ``GET /api/v1/companions`` — list companion identities
- ``GET /api/v1/companions/{name}/snapshot`` — bootstrap document (§7.4)
- ``GET /api/v1/companions/{name}/sync?cursor=&limit=`` — journal delta (§7.5)
- ``GET /api/v1/companions/{name}/messages?before_id=&limit=`` — history page

Cursor semantics (§5.3): clients hold an opaque cursor string (the journal
seq); the server keeps no per-client read state. ``journal_epoch`` detects
DB resets; ``prune_floor`` turns aged-out cursors into ``snapshot_required``
instead of silently incomplete deltas. All reads are bounded, index-served
SQLite queries executed directly on the request thread (§13).

Action endpoints (send/login/telemetry), SSE, and pairing arrive in later
phases; the existing ``/api/companion/*`` surface remains for the web UI.
"""

import logging
import time
from typing import Optional, Tuple

import cherrypy

from .auth.middleware import require_auth

logger = logging.getLogger("MobileAPI")

try:
    from repeater._version import __version__ as _REPEATER_VERSION
except Exception:  # pragma: no cover - version metadata is best-effort
    _REPEATER_VERSION = None


class MobileAPIEndpoints:
    """Root of the ``/api/v1/`` tree (attach as ``APIEndpoints.v1``)."""

    def __init__(self, daemon_instance=None, config=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.companions = CompanionsV1(daemon_instance, self.config)


class CompanionsV1:
    """``/api/v1/companions`` collection and per-companion sync resources."""

    _ACTIONS = ("snapshot", "sync", "messages")

    def __init__(self, daemon_instance=None, config=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}

    # ------------------------------------------------------------------
    # Dispatch / helpers
    # ------------------------------------------------------------------

    def _cp_dispatch(self, vpath):
        """Route ``/companions/{name}/{action}`` to the action handler.

        The companion name segment becomes the ``companion_name`` request
        param; unknown actions fall through to CherryPy's 404.
        """
        if len(vpath) == 2:
            name = vpath.pop(0)
            action = vpath.pop(0)
            if action in self._ACTIONS:
                cherrypy.request.params["companion_name"] = name
                return getattr(self, action)
        return None

    @staticmethod
    def _success(data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    @staticmethod
    def _require_get():
        if cherrypy.request.method not in ("GET", "OPTIONS"):
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")

    def _resolve(self, name: Optional[str]) -> Tuple[object, str]:
        """Return ``(bridge, companion_hash_str)`` for a companion name.

        The hash string is lowercase ``'0x%02x'`` — the exact key format
        main.py uses for the companion_* SQLite namespaces (NOT the
        uppercase display form used by the legacy /api/companion listing).
        """
        if not name:
            raise cherrypy.HTTPError(400, "companion_name required")
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        bridges = getattr(self.daemon_instance, "companion_bridges", {})
        identity_manager = getattr(self.daemon_instance, "identity_manager", None)
        if identity_manager:
            for reg_name, identity, _cfg in identity_manager.get_identities_by_type(
                "companion"
            ):
                if reg_name == name:
                    hash_byte = identity.get_public_key()[0]
                    bridge = bridges.get(hash_byte)
                    if bridge:
                        return bridge, f"0x{hash_byte:02x}"  # noqa: E231
        raise cherrypy.HTTPError(404, f"Companion '{name}' not found")

    def _get_sqlite_handler(self):
        """Return the repeater's sqlite_handler, or raise 503 (same path as
        companion_endpoints._get_sqlite_handler)."""
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        repeater_handler = getattr(self.daemon_instance, "repeater_handler", None)
        if not repeater_handler:
            raise cherrypy.HTTPError(503, "Repeater handler not initialized")
        storage = getattr(repeater_handler, "storage", None)
        if not storage:
            raise cherrypy.HTTPError(503, "Storage not initialized")
        sqlite_handler = getattr(storage, "sqlite_handler", None)
        if not sqlite_handler:
            raise cherrypy.HTTPError(503, "SQLite storage not available")
        return sqlite_handler

    @staticmethod
    def _etag_not_modified(etag: str) -> bool:
        """Set the ETag header; return True (and set 304) on If-None-Match hit."""
        cherrypy.response.headers["ETag"] = etag
        if_none_match = cherrypy.request.headers.get("If-None-Match")
        if if_none_match and if_none_match.strip() == etag:
            cherrypy.response.status = 304
            return True
        return False

    @staticmethod
    def _clamp(value, default: int, low: int, high: int) -> int:
        try:
            parsed = int(value) if value is not None else default
        except (TypeError, ValueError):
            raise cherrypy.HTTPError(400, "Invalid integer parameter")
        return max(low, min(parsed, high))

    # ------------------------------------------------------------------
    # GET /api/v1/companions
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def index(self, **kwargs):
        """List configured companion identities."""
        self._require_get()
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        bridges = getattr(self.daemon_instance, "companion_bridges", {})
        identity_manager = getattr(self.daemon_instance, "identity_manager", None)

        name_by_hash: dict = {}
        if identity_manager:
            for reg_name, identity, _cfg in identity_manager.get_identities_by_type(
                "companion"
            ):
                name_by_hash[identity.get_public_key()[0]] = reg_name

        items = []
        for hash_byte, bridge in bridges.items():
            items.append(
                {
                    "name": name_by_hash.get(hash_byte, ""),
                    "companion_hash": f"0x{hash_byte:02x}",  # noqa: E231
                    "node_name": bridge.prefs.node_name,
                    "public_key": bridge.get_public_key().hex(),
                }
            )
        return self._success(items)

    # ------------------------------------------------------------------
    # GET /api/v1/companions/{name}/snapshot
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def snapshot(self, companion_name=None, messages_limit=None, **kwargs):
        """Bootstrap document: state + the journal cursor it corresponds to.

        Head seq is read FIRST (design doc §7.4): events with seq > cursor
        may duplicate snapshot content, which clients dedupe by message id —
        the reverse order could silently lose events.
        """
        self._require_get()
        bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()
        limit = self._clamp(messages_limit, default=100, low=1, high=200)

        head = handler.companion_journal_head(companion_hash)
        epoch = handler.companion_journal_epoch()
        if self._etag_not_modified(f'"{epoch}:{head}"'):  # noqa: E231
            return None

        prefs = bridge.get_self_info()
        self_info = {
            "public_key": bridge.get_public_key().hex(),
            "node_name": prefs.node_name,
            "adv_type": prefs.adv_type,
            "latitude": prefs.latitude,
            "longitude": prefs.longitude,
        }

        contacts = []
        for c in bridge.get_contacts():
            contacts.append(
                {
                    "public_key": (
                        c.public_key.hex() if isinstance(c.public_key, bytes) else c.public_key
                    ),
                    "name": c.name,
                    "adv_type": c.adv_type,
                    "flags": c.flags,
                    "out_path_len": c.out_path_len,
                    "last_advert_timestamp": c.last_advert_timestamp,
                    "lastmod": c.lastmod,
                    "gps_lat": c.gps_lat,
                    "gps_lon": c.gps_lon,
                }
            )

        channels = []
        for idx in range(bridge.channels.max_channels):
            ch = bridge.channels.get(idx)
            if ch:
                # PSK secrets are never exposed on the mobile surface
                channels.append({"index": idx, "name": ch.name})

        # Stored newest-first; snapshot delivers oldest-first for direct
        # client-side append order.
        messages = list(reversed(handler.companion_get_messages(companion_hash, limit=limit)))

        return self._success(
            {
                "journal_epoch": epoch,
                "cursor": str(head),
                "self": self_info,
                "contacts": contacts,
                "channels": channels,
                "messages": messages,
                "server": {"time": time.time(), "version": _REPEATER_VERSION},
            }
        )

    # ------------------------------------------------------------------
    # GET /api/v1/companions/{name}/sync
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def sync(self, companion_name=None, cursor=None, limit=None, **kwargs):
        """Journal delta since ``cursor`` (design doc §7.5).

        One indexed range scan over idx_companion_events_sync, bounded by
        ``limit``. A cursor below the prune floor gets snapshot_required
        rather than a silently incomplete delta.
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        if cursor is None:
            raise cherrypy.HTTPError(400, "cursor required")
        try:
            cursor_seq = int(str(cursor))
        except (TypeError, ValueError):
            raise cherrypy.HTTPError(400, "Invalid cursor")
        if cursor_seq < 0:
            raise cherrypy.HTTPError(400, "Invalid cursor")
        limit_n = self._clamp(limit, default=100, low=1, high=500)

        head = handler.companion_journal_head(companion_hash)
        epoch = handler.companion_journal_epoch()
        if self._etag_not_modified(f'"{epoch}:{head}"'):  # noqa: E231
            return None

        prune_floor = int(handler.companion_journal_meta_get("prune_floor") or 0)
        if cursor_seq < prune_floor:
            return self._success(
                {
                    "journal_epoch": epoch,
                    "events": [],
                    "next_cursor": str(cursor_seq),
                    "has_more": False,
                    "snapshot_required": True,
                }
            )

        rows = handler.companion_get_events(companion_hash, cursor_seq, limit_n)
        events = [
            {
                "seq": row["seq"],
                "type": row["event_type"],
                "ts": row["created_at"],
                "packet_hash": row.get("packet_hash"),
                "data": row.get("payload", {}),
            }
            for row in rows
        ]
        last_seq = events[-1]["seq"] if events else cursor_seq
        return self._success(
            {
                "journal_epoch": epoch,
                "events": events,
                "next_cursor": str(last_seq),
                # head was read before the page; if the page filled the limit
                # but didn't reach head, more events are already waiting.
                "has_more": len(events) == limit_n and last_seq < head,
                "snapshot_required": False,
            }
        )

    # ------------------------------------------------------------------
    # GET /api/v1/companions/{name}/messages
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def messages(self, companion_name=None, before_id=None, limit=None, **kwargs):
        """Newest-first message-history page (infinite scroll; not the journal)."""
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        limit_n = self._clamp(limit, default=100, low=1, high=200)
        before = None
        if before_id is not None:
            try:
                before = int(str(before_id))
            except (TypeError, ValueError):
                raise cherrypy.HTTPError(400, "Invalid before_id")

        rows = handler.companion_get_messages(companion_hash, before_id=before, limit=limit_n)
        next_before_id = rows[-1]["id"] if rows else None
        return self._success({"messages": rows, "next_before_id": next_before_id})
