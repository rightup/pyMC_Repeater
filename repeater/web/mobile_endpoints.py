"""
Mobile Companion API v1 endpoints (phase 1 sync core + phase 2 SSE stream +
actions + auth).

Mounted as ``APIEndpoints.v1`` so CherryPy serves it at ``/api/v1/``.
Implements the synchronization, action, and auth surface from
docs/architecture/mobile-companion-api.md §7:

- ``GET /api/v1/server_info`` — unauthenticated discovery (§7.1, §11.3)
- ``POST /api/v1/pair/start`` — admin-only pairing code generation (§11.2)
- ``POST /api/v1/pair`` — exchange a pairing code for a device token (§11.2)
- ``GET /api/v1/devices`` / ``DELETE /api/v1/devices/{device_id}`` — device
  registry (§11.2 step 4)
- ``GET /api/v1/companions`` — list companion identities
- ``GET /api/v1/companions/{name}/snapshot`` — bootstrap document (§7.4)
- ``GET /api/v1/companions/{name}/sync?cursor=&limit=`` — journal delta (§7.5)
- ``GET /api/v1/companions/{name}/messages?before_id=&limit=`` — history page
- ``POST /api/v1/companions/{name}/messages`` — send DM/channel message (§7.3)
- ``GET /api/v1/companions/{name}/events`` — resumable SSE live stream (§8)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/login`` — room login (§7.3)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/status_request`` (§7.3)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/telemetry_request`` (§7.3)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/reset_path`` (§7.3)

Cursor semantics (§5.3): clients hold an opaque cursor string (the journal
seq); the server keeps no per-client read state. ``journal_epoch`` detects
DB resets; ``prune_floor`` turns aged-out cursors into ``snapshot_required``
instead of silently incomplete deltas. All reads are bounded, index-served
SQLite queries executed directly on the request thread (§13).

Action endpoints mirror the bridge calls of the existing
``/api/companion/*`` surface (companion_endpoints.py), which remains for the
web UI. The one behavioral addition is the mandatory ``Idempotency-Key``
contract on sends (§6): every ``POST …/messages`` transmits RF, so retries
after a timeout must replay the original response instead of double-sending.

Scope enforcement (§11.1) lives at ``CompanionsV1._resolve`` — the one
choke point every companion handler (including the undecorated ``events``
SSE stream) passes through — checked against ``cherrypy.request.user``'s
``scope``, which ``auth/middleware.py`` and ``auth/cherrypy_tool.py`` both
set alongside the existing JWT/API-token verification.
"""

import asyncio
import hashlib
import json
import logging
import queue
import secrets
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional, Tuple
from urllib.parse import urlparse

import cherrypy

from repeater.companion.correlation import outbound_send_capture
from repeater.companion.path_resolution import resolve_path
from repeater.companion.rf_window import observations_pruned, parse_window_seconds

from .auth.middleware import require_auth
from .companion_endpoints import _to_json_safe

logger = logging.getLogger("MobileAPI")

try:
    from repeater._version import __version__ as _REPEATER_VERSION
except Exception:  # pragma: no cover - version metadata is best-effort
    _REPEATER_VERSION = None


def _normalize_hash16(value) -> Optional[str]:
    """Normalize any hash representation (bytes or str, any length, either
    case, optionally ``0x``-prefixed) to the canonical 16-char uppercase
    truncated form used by ``packets.packet_hash`` (design doc §10.2)."""
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.hex()
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    text = text.upper()
    if not text:
        return None
    return text[:16]


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except (TypeError, ValueError):
        return False


# Opt-in uncorrelated RF-reception firehose event type (design doc §9
# "Correlated vs. uncorrelated receptions"). Excluded from sync/SSE output
# unless the request's ``include`` param names it — see
# ``_include_rf_receptions``.
_RF_RECEPTION_EVENT_TYPE = "rf_reception"
_INCLUDE_RF_RECEPTIONS_TOKEN = "rf_receptions"


def _include_rf_receptions(include) -> bool:
    """Parse the ``?include=`` query param (comma-separated, unknown tokens
    ignored) and report whether ``rf_receptions`` was requested."""
    if not include:
        return False
    tokens = {tok.strip() for tok in str(include).split(",")}
    return _INCLUDE_RF_RECEPTIONS_TOKEN in tokens


async def _send_and_capture(coro):
    """Await ``coro`` with a fresh ``outbound_send_capture`` holder in scope.

    ``RepeaterCompanionBridge._send_packet`` (awaited somewhere inside
    ``coro``, transitively) publishes the packet_hash it computed into
    whatever holder is set in the *current* context (design doc §10.4).
    Setting the ContextVar here — inside the coroutine actually scheduled
    onto the event loop — rather than in the request thread that builds
    ``coro``, is what scopes the holder to this one send: contextvars
    propagate down an await chain within the same task, so two concurrent
    sends (two separate calls to this function, two separate tasks) never
    see each other's holder.
    """
    holder: dict = {}
    token = outbound_send_capture.set(holder)
    try:
        result = await coro
    finally:
        outbound_send_capture.reset(token)
    return result, holder.get("hash")


class MobileAPIEndpoints:
    """Root of the ``/api/v1/`` tree (attach as ``APIEndpoints.v1``)."""

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop
        self.companions = CompanionsV1(daemon_instance, self.config, event_loop=event_loop)
        self.pair = PairV1(daemon_instance, self.config, event_loop=event_loop)
        self.devices = DevicesV1(daemon_instance, self.config, event_loop=event_loop)

    # ------------------------------------------------------------------
    # GET /api/v1/server_info  (unauthenticated, design doc §7.1 / §11.3)
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def server_info(self, **kwargs):
        """Unauthenticated-safe minimum so an app can validate a scanned
        pairing URL before it has any credential (§7.1): server/site name,
        supported API versions, supported auth modes, and repeater
        version/time. Deliberately excludes companion names — the
        ``/api/v1/companions`` listing requires auth, matching the design
        doc's explicit "companion names list requires auth" note.
        """
        if cherrypy.request.method not in ("GET", "OPTIONS"):
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")
        # Same source as the legacy /api/site_info (api_endpoints.py).
        site_name = self.config.get("web", {}).get("site_name", "") or ""
        return {
            "success": True,
            "data": {
                "site_name": str(site_name),
                "api_versions": ["v1"],
                "auth_modes": ["jwt", "api_token"],
                "server": {"version": _REPEATER_VERSION, "time": time.time()},
            },
        }


class CompanionsV1:
    """``/api/v1/companions`` collection and per-companion sync/action resources."""

    _ACTIONS = ("snapshot", "sync", "messages", "events")
    # Sub-resource actions under /companions/{name}/contacts/{pubkey}/{action}
    # (§7.3). All four are POST-only JSON handlers with no idempotency
    # requirement (only message sends transmit RF that a duplicate retry
    # would repeat).
    _CONTACT_ACTIONS = ("login", "status_request", "telemetry_request", "reset_path")
    # GET-only sub-resource actions on the same /contacts/{pubkey}/{action}
    # URL shape (§10): route identically to _CONTACT_ACTIONS, just a
    # different HTTP method and handler set.
    _CONTACT_GET_ACTIONS = ("paths",)
    # Sub-resource actions under /companions/{name}/messages/{id}/{action}
    # and /companions/{name}/transmissions/{packet_hash}/{action} (§10): the
    # RF observation surface's other two URL shapes.
    _MESSAGE_SUB_ACTIONS = ("receptions",)
    _TRANSMISSION_SUB_ACTIONS = ("repeats",)

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop

    # ------------------------------------------------------------------
    # Dispatch / helpers
    # ------------------------------------------------------------------

    def _cp_dispatch(self, vpath):
        """Route ``/companions/{name}/{action}``,
        ``/companions/{name}/contacts/{pubkey}/{action}``,
        ``/companions/{name}/messages/{id}/{action}``, and
        ``/companions/{name}/transmissions/{packet_hash}/{action}`` to their
        handlers.

        The companion name segment becomes the ``companion_name`` request
        param; the second/third segments become ``contact_pubkey``,
        ``message_id``, or ``packet_hash`` depending on the URL shape.
        Unknown actions fall through to CherryPy's 404.
        """
        if len(vpath) == 2:
            name = vpath.pop(0)
            action = vpath.pop(0)
            if action in self._ACTIONS:
                cherrypy.request.params["companion_name"] = name
                return getattr(self, action)
        elif len(vpath) == 4:
            collection = vpath[1]
            action = vpath[3]
            if collection == "contacts" and action in (
                self._CONTACT_ACTIONS + self._CONTACT_GET_ACTIONS
            ):
                name = vpath.pop(0)
                vpath.pop(0)  # literal 'contacts' segment
                pubkey = vpath.pop(0)
                vpath.pop(0)
                cherrypy.request.params["companion_name"] = name
                cherrypy.request.params["contact_pubkey"] = pubkey
                return getattr(self, action)
            if collection == "messages" and action in self._MESSAGE_SUB_ACTIONS:
                name = vpath.pop(0)
                vpath.pop(0)  # literal 'messages' segment
                message_id = vpath.pop(0)
                vpath.pop(0)
                cherrypy.request.params["companion_name"] = name
                cherrypy.request.params["message_id"] = message_id
                return getattr(self, action)
            if collection == "transmissions" and action in self._TRANSMISSION_SUB_ACTIONS:
                name = vpath.pop(0)
                vpath.pop(0)  # literal 'transmissions' segment
                packet_hash = vpath.pop(0)
                vpath.pop(0)
                cherrypy.request.params["companion_name"] = name
                cherrypy.request.params["packet_hash"] = packet_hash
                return getattr(self, action)
        return None

    @staticmethod
    def _success(data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    @staticmethod
    def _error(msg):
        return {"success": False, "error": str(msg)}

    @staticmethod
    def _require_get():
        if cherrypy.request.method not in ("GET", "OPTIONS"):
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")

    @staticmethod
    def _require_post():
        # Mirrors companion_endpoints.CompanionAPIEndpoints._require_post
        # exactly (no OPTIONS exception) — the action handlers below copy
        # its bridge calls and response shapes, so they copy this too.
        if cherrypy.request.method != "POST":
            cherrypy.response.headers["Allow"] = "POST"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST.")

    def _get_json_body(self) -> dict:
        """Read and parse the JSON request body (mirrors
        companion_endpoints._get_json_body)."""
        try:
            raw = cherrypy.request.body.read()
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid JSON body: {exc}")

    def _pub_key_from_hex(self, hex_str: str) -> bytes:
        """Decode a hex public key, raising 400 on error (mirrors
        companion_endpoints._pub_key_from_hex)."""
        try:
            key = bytes.fromhex(hex_str or "")
            if len(key) != 32:
                raise ValueError("Expected 32-byte key")
            return key
        except (ValueError, TypeError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid public key: {exc}")

    def _run_async(self, coro, timeout: float = 30.0):
        """Run an async bridge coroutine on the daemon event loop and return
        its result (copies companion_endpoints.CompanionAPIEndpoints._run_async's
        503-if-no-loop check). Unlike the legacy helper, a timeout or any
        other exception is converted to an HTTPError here (504 / 500) rather
        than left to propagate as a raw Python exception — every caller of
        this helper is a @cherrypy.tools.json_out() action handler, and an
        unconverted exception would escape as CherryPy's default HTML error
        page instead of this API's JSON envelope.
        """
        if self.event_loop is None:
            raise cherrypy.HTTPError(503, "Event loop not available")
        future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise cherrypy.HTTPError(504, "Timed out waiting for radio response")
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            raise cherrypy.HTTPError(500, str(exc))

    def _idempotency_device_id(self) -> str:
        """Scope key for the companion_idempotency table (design doc §6).

        Resolves the caller's paired ``companion_devices.device_id`` when
        authenticated with a device API token (auth_type 'api_token' —
        require_auth sets ``token_id``). JWT callers (the web UI, or a
        developer testing sends without a paired device) fall back to
        ``user:{username}`` so the endpoint still works, just scoped to the
        logged-in user rather than a specific device.
        """
        user = getattr(cherrypy.request, "user", None) or {}
        if user.get("auth_type") == "api_token" and user.get("token_id") is not None:
            device = self._get_sqlite_handler().companion_device_get_by_token(user["token_id"])
            if device and device.get("device_id"):
                return device["device_id"]
        return f"user:{user.get('username', 'unknown')}"

    def _resolve(self, name: Optional[str]) -> Tuple[object, str]:
        """Return ``(bridge, companion_hash_str)`` for a companion name.

        The hash string is lowercase ``'0x%02x'`` — the exact key format
        main.py uses for the companion_* SQLite namespaces (NOT the
        uppercase display form used by the legacy /api/companion listing).

        This is the single choke point for scope enforcement (design doc
        §11.1): every companion handler above, including the SSE ``events``
        stream (which has no ``@require_auth`` decorator of its own — see
        its docstring), resolves the companion name through here, so the
        scope check below covers all of them uniformly.
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
                        self._check_scope(name)
                        return bridge, f"0x{hash_byte:02x}"  # noqa: E231
        raise cherrypy.HTTPError(404, f"Companion '{name}' not found")

    @staticmethod
    def _check_scope(name: str) -> None:
        """Enforce the caller's token scope against companion ``name``
        (design doc §11.1): ``admin``, ``companion:*`` (all companions), or
        ``companion:{name}`` (exact resolved name) are allowed.

        Out-of-scope access raises 404 with the SAME message ``_resolve``
        uses for unknown names, not 403 — a 403 would confirm to a scoped
        device token that some other companion name exists (the
        ``/companions`` listing is filtered for the same reason). Keep the
        message in sync with ``_resolve``'s not-found error.

        A ``request.user`` dict with no ``scope`` key at all is a
        pre-scope-migration / legacy caller (e.g. a JWT payload predating
        this change, or a test harness) and is treated as ``admin`` for
        backward compatibility — ``verify_token``/``verify_jwt`` callers
        already apply this same NULL-defaults-to-admin rule (design doc
        §11.1). A genuinely missing ``request.user`` (require_auth didn't
        run, or somehow didn't set it) has no scope to fall back to, so it
        is rejected rather than silently treated as admin.
        """
        user = getattr(cherrypy.request, "user", None)
        if user is None:
            raise cherrypy.HTTPError(404, f"Companion '{name}' not found")
        scope = user.get("scope", "admin")
        if scope in ("admin", "companion:*", f"companion:{name}"):
            return
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

    def _get_journal(self, companion_hash: str):
        """Return the CompanionEventJournal for ``companion_hash``, or None.

        The journal lives on the per-companion frame server (main.py wires
        one ``CompanionEventJournal`` per companion into its
        ``CompanionFrameServer``), not on the bridge — ``daemon_instance.
        companion_frame_servers`` is a flat list, matched here by the same
        ``'0x%02x'`` hash string ``_resolve`` returns.
        """
        servers = getattr(self.daemon_instance, "companion_frame_servers", None) or []
        for server in servers:
            if getattr(server, "companion_hash", None) == companion_hash:
                return getattr(server, "journal", None)
        return None

    def _sse_settings(self) -> Tuple[int, int]:
        """Return ``(queue_maxsize, keepalive_sec)`` from ``config.http``,
        same keys and defaults as the legacy SSE stream in
        companion_endpoints.py."""
        http_cfg = self.config.get("http", {}) if isinstance(self.config, dict) else {}
        queue_maxsize = max(32, int(http_cfg.get("sse_queue_maxsize", 64)))
        keepalive_sec = max(5, int(http_cfg.get("sse_keepalive_sec", 15)))
        return queue_maxsize, keepalive_sec

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

    @staticmethod
    def _parse_window(value) -> int:
        """Parse+clamp a ``?window=`` param (design doc §10.1), raising the
        API's standard 400 on a malformed (not clamped) value."""
        try:
            return parse_window_seconds(value)
        except ValueError as exc:
            raise cherrypy.HTTPError(400, str(exc))

    # ------------------------------------------------------------------
    # GET /api/v1/companions
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def index(self, **kwargs):
        """List configured companion identities.

        Filtered by the caller's scope (design doc §11.1): a
        ``companion:{name}`` device token sees only its own companion —
        the scope grants "the full companion API for ONE companion
        identity", and that includes not enumerating the others' names
        and public keys. ``admin``/``companion:*`` (and legacy scope-less
        callers) see everything.
        """
        self._require_get()
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        user = getattr(cherrypy.request, "user", None) or {}
        scope = user.get("scope", "admin")
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
            name = name_by_hash.get(hash_byte, "")
            if scope not in ("admin", "companion:*", f"companion:{name}"):
                continue
            items.append(
                {
                    "name": name,
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
    def sync(self, companion_name=None, cursor=None, limit=None, include=None, **kwargs):
        """Journal delta since ``cursor`` (design doc §7.5).

        One indexed range scan over idx_companion_events_sync, bounded by
        ``limit``. A cursor below the prune floor gets snapshot_required
        rather than a silently incomplete delta.

        ``rf_reception`` events (§9, opt-in firehose) are excluded from the
        returned ``events`` list unless ``?include=rf_receptions`` is given
        (comma-separated, unknown tokens ignored). The filter is applied
        after the storage query, so it never changes what the query itself
        scans: ``next_cursor`` still reflects the last row scanned (matching
        rows or not), and a client that later opts in simply re-reads those
        rows from an older cursor — same semantics as any other cursor
        re-read.
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
        want_rf_receptions = _include_rf_receptions(include)
        events = [
            {
                "seq": row["seq"],
                "type": row["event_type"],
                "ts": row["created_at"],
                "packet_hash": row.get("packet_hash"),
                "data": row.get("payload", {}),
            }
            for row in rows
            if want_rf_receptions or row["event_type"] != _RF_RECEPTION_EVENT_TYPE
        ]
        # Cursor tracks the last row the query scanned, not the last row
        # returned to the client — a filtered-out rf_reception row still
        # advances it, so a client that opts in later doesn't re-scan rows
        # it already passed.
        last_seq = rows[-1]["seq"] if rows else cursor_seq
        return self._success(
            {
                "journal_epoch": epoch,
                "events": events,
                "next_cursor": str(last_seq),
                # head was read before the page; if the page filled the limit
                # but didn't reach head, more events are already waiting.
                "has_more": len(rows) == limit_n and last_seq < head,
                "snapshot_required": False,
            }
        )

    # ------------------------------------------------------------------
    # GET/POST /api/v1/companions/{name}/messages
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def messages(self, companion_name=None, before_id=None, limit=None, **kwargs):
        """GET: newest-first message-history page (infinite scroll; not the
        journal). POST: send a DM or channel message (§7.3, §6)."""
        method = cherrypy.request.method
        if method == "POST":
            return self._send_message(companion_name)
        if method in ("GET", "OPTIONS"):
            return self._message_history(companion_name, before_id, limit)
        cherrypy.response.headers["Allow"] = "GET, POST"
        raise cherrypy.HTTPError(405, "Method not allowed. Use GET or POST.")

    def _message_history(self, companion_name, before_id, limit):
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

    def _send_message(self, companion_name):
        """POST /api/v1/companions/{name}/messages — send a DM or channel
        message (§7.3): bridge calls mirror companion_endpoints.send_text /
        send_channel_message exactly. Wrapped with the mandatory
        Idempotency-Key contract (§6): a retry with the same key and the
        same body replays the stored response without touching the radio;
        the same key against a different body — or a different companion —
        is a 409. Only a *successful* send claims the key; a failed send
        (radio/queue rejection) leaves it unclaimed so a retry reaches the
        radio again instead of replaying a cached failure forever.

        A successful response also carries ``packet_hash``: the 16-char
        correlation key (§10.2) for the packet that was just transmitted,
        captured off ``RepeaterCompanionBridge._send_packet`` via the
        ``outbound_send_capture`` contextvar (§10.4). Clients can use it to
        match a send against later ``message_send_state`` heard-repeat
        events without waiting on a round trip through the journal. Absent
        on failure — a send that never reached the radio has no packet_hash.
        """
        bridge, _companion_hash = self._resolve(companion_name)

        idempotency_key = cherrypy.request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise cherrypy.HTTPError(400, "Idempotency-Key header required")

        body = self._get_json_body()
        to_hex = body.get("to")
        channel_idx = body.get("channel_idx")
        has_to = bool(to_hex)
        has_channel = channel_idx is not None  # 0 is a valid channel index
        if has_to == has_channel:
            raise cherrypy.HTTPError(400, "Exactly one of 'to' or 'channel_idx' required")
        text = body.get("text", "")
        if not text:
            raise cherrypy.HTTPError(400, "text required")

        handler = self._get_sqlite_handler()
        device_id = self._idempotency_device_id()
        # Canonical request for hash comparison: the parsed body plus the
        # companion name, so the same Idempotency-Key reused against a
        # different companion, or a different body, is detectable (§6).
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")) + companion_name
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        existing = handler.companion_idempotency_get(device_id, idempotency_key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise cherrypy.HTTPError(409, "Idempotency-Key reuse with different request")
            # Replay verbatim — the original send already happened; a retry
            # never touches the radio a second time.
            return json.loads(existing["response_json"])

        if has_to:
            pub_key = self._pub_key_from_hex(to_hex)
            txt_type = int(body.get("txt_type", 0))
            result, packet_hash = self._run_async(
                _send_and_capture(bridge.send_text_message(pub_key, text, txt_type=txt_type))
            )
            sent_ok = result.success
            data = {
                "sent": result.success,
                "is_flood": result.is_flood,
                "expected_ack": result.expected_ack,
            }
            if sent_ok and packet_hash:
                data["packet_hash"] = packet_hash[:16]
            response = self._success(data)
        else:
            idx = int(channel_idx)
            sent_ok, packet_hash = self._run_async(
                _send_and_capture(bridge.send_channel_message(idx, text))
            )
            data = {"sent": sent_ok}
            if sent_ok and packet_hash:
                data["packet_hash"] = packet_hash[:16]
            response = self._success(data)

        if not sent_ok:
            return response

        response_json = json.dumps(response)
        if not handler.companion_idempotency_put(
            device_id, idempotency_key, request_hash, response_json
        ):
            # Lost a race against a concurrent retry using the same key: the
            # other request's PUT won this INSERT OR IGNORE. The RF send
            # this request just performed already happened — it can't be
            # undone — so the best remaining move is to converge on the one
            # canonical stored response rather than hand the two callers two
            # different bodies for the same Idempotency-Key.
            existing = handler.companion_idempotency_get(device_id, idempotency_key)
            if existing is not None and existing["request_hash"] == request_hash:
                return json.loads(existing["response_json"])
        return response

    # ------------------------------------------------------------------
    # GET /api/v1/companions/{name}/events  (SSE, design doc §8)
    # ------------------------------------------------------------------

    @staticmethod
    def _sse_frame(row: dict) -> str:
        """Format one journal row (or listener event — same shape) as an SSE
        frame: ``id:`` = seq, ``event:`` = event type, ``data:`` = the same
        JSON object ``sync`` returns for this row (§8, one schema/two
        transports)."""
        seq = row["seq"]
        event_type = row["event_type"]
        data = json.dumps(
            {
                "seq": seq,
                "type": event_type,
                "ts": row["created_at"],
                "packet_hash": row.get("packet_hash"),
                "data": row.get("payload", {}),
            },
            separators=(",", ":"),
        )
        return f"id: {seq}\nevent: {event_type}\ndata: {data}\n\n"

    @cherrypy.expose
    def events(self, companion_name=None, cursor=None, include=None, **kwargs):
        """GET /api/v1/companions/{name}/events — resumable SSE live stream.

        Connect with ``EventSource('.../events?token=JWT')``. Auth is the
        CherryPy tool-level require_auth that covers the whole /api tree
        (http_server.py) — it accepts the query-param JWT that browser
        EventSource needs and CONSUMES it before the handler runs, which is
        why this handler must not stack the @require_auth decorator on top
        (the decorator would re-check and 401 a query-token request whose
        token the tool already stripped; the legacy /api/companion/events
        stream omits the decorator for the same reason). Resume point is
        the standard ``Last-Event-ID`` header if present, else
        ``?cursor=``, else the current journal head (a bare connection
        with neither gets a live tail only, no replay).

        No-gap ordering (§8, §6 at-least-once): the journal listener is
        registered BEFORE the backlog is drained, so anything appended
        during the drain lands in this client's queue instead of being
        missed; events the drain already yielded are then skipped once the
        queue is consumed (``seq <= last_sent_seq``). Net effect: at most a
        few duplicate events around the handoff, never a gap — the same
        contract sync gives clients that re-poll from an old cursor.

        ``rf_reception`` events (§9, opt-in firehose) are omitted from both
        the backlog replay and the live tail unless ``?include=rf_receptions``
        is given — same rule and token format as ``sync``. Skipped rows still
        advance ``last_sent_seq`` internally (no gap if the client later
        reconnects with ``include`` after seeing a later id), they are just
        never framed onto the wire.
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        want_rf_receptions = _include_rf_receptions(include)
        handler = self._get_sqlite_handler()
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        last_event_id = cherrypy.request.headers.get("Last-Event-ID")
        cursor_param = last_event_id if last_event_id is not None else cursor
        if cursor_param is None:
            cursor_seq = handler.companion_journal_head(companion_hash)
        else:
            try:
                cursor_seq = int(str(cursor_param))
            except (TypeError, ValueError):
                raise cherrypy.HTTPError(400, "Invalid cursor")
            if cursor_seq < 0:
                raise cherrypy.HTTPError(400, "Invalid cursor")

        epoch = handler.companion_journal_epoch()
        prune_floor = int(handler.companion_journal_meta_get("prune_floor") or 0)
        queue_maxsize, keepalive_sec = self._sse_settings()

        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"

        if cursor_seq < prune_floor:
            # Cursor is older than the journal's retention floor: a replay
            # from here would be silently incomplete. One control event,
            # then close — client must snapshot and reconnect with a fresh
            # cursor, same rule sync applies via snapshot_required.
            def _snapshot_required_stream():
                payload = json.dumps(
                    {"journal_epoch": epoch, "snapshot_required": True},
                    separators=(",", ":"),
                )
                yield f"event: snapshot_required\ndata: {payload}\n\n"

            return _snapshot_required_stream()

        client_queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        # Plain dict, not a lock: a single bool set/read is atomic under the
        # GIL, and the callback (worker thread) only ever writes True while
        # the generator (request thread) only ever reads it.
        overflow = {"dead": False}

        def _on_event(event: dict) -> None:
            # Fires from an asyncio.to_thread worker thread (journal
            # _notify's caller). queue.Queue.put_nowait is thread-safe.
            try:
                client_queue.put_nowait(event)
            except queue.Full:
                # Slow client: drop it rather than buffer unboundedly or
                # block the journal writer. It reconnects with
                # Last-Event-ID and replays the backlog it missed.
                overflow["dead"] = True

        journal.register_listener(_on_event)

        def generate():
            last_sent_seq = cursor_seq
            try:
                # Drain the backlog from the cursor in pages, so a
                # long-offline client doesn't hold one huge query open.
                while True:
                    rows = handler.companion_get_events(companion_hash, last_sent_seq, 500)
                    if not rows:
                        break
                    for row in rows:
                        last_sent_seq = row["seq"]
                        if (
                            not want_rf_receptions
                            and row["event_type"] == _RF_RECEPTION_EVENT_TYPE
                        ):
                            continue
                        yield self._sse_frame(row)
                    if len(rows) < 500:
                        break

                # Live tail: consume the queue, skipping anything already
                # sent by the drain above (the registration-before-drain
                # overlap window).
                while True:
                    if overflow["dead"]:
                        break
                    try:
                        event = client_queue.get(timeout=keepalive_sec)
                    except queue.Empty:
                        # Keep-alive comment frame; EventSource ignores
                        # lines starting with ':'.
                        yield ": ka\n\n"
                        continue
                    if event["seq"] <= last_sent_seq:
                        continue
                    last_sent_seq = event["seq"]
                    if (
                        not want_rf_receptions
                        and event["event_type"] == _RF_RECEPTION_EVENT_TYPE
                    ):
                        continue
                    yield self._sse_frame(event)
            except GeneratorExit:
                pass
            except Exception as exc:
                logger.debug("Mobile SSE stream ended for %s: %s", companion_hash, exc)
            finally:
                journal.unregister_listener(_on_event)

        return generate()

    events._cp_config = {"response.stream": True}

    # ------------------------------------------------------------------
    # POST /api/v1/companions/{name}/contacts/{pubkey}/{action}  (§7.3)
    #
    # Thin wrappers over the same CompanionBridge coroutines
    # companion_endpoints.py calls, routed here via _cp_dispatch. No
    # idempotency handling — the design doc requires it only for message
    # sends (§6), since these actions don't transmit a duplicable payload
    # the way a text/channel message does.
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def login(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/login  {password} — room/repeater login."""
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        body = self._get_json_body()
        password = body.get("password", "")
        result = self._run_async(bridge.send_login(pub_key, password), timeout=15.0)
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def status_request(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/status_request  (empty body) — remote
        status query."""
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        timeout = 15.0
        result = self._run_async(
            bridge.send_status_request(pub_key, timeout=timeout),
            timeout=timeout + 5.0,
        )
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def telemetry_request(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/telemetry_request  (empty body) —
        remote telemetry query (base + location + environment)."""
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        timeout = 20.0
        result = self._run_async(
            bridge.send_telemetry_request(
                pub_key,
                want_base=True,
                want_location=True,
                want_environment=True,
                timeout=timeout,
            ),
            timeout=timeout + 5.0,
        )
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def reset_path(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/reset_path  (empty body) — reset the
        outbound routing path for a contact."""
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        ok = bridge.reset_path(pub_key)
        return self._success({"reset": ok})

    # ------------------------------------------------------------------
    # RF observation surface (design doc §10) -- read-only queries over
    # `packets`/`companion_messages`, no new write path. All three below
    # share the mandatory bounded-window rule (§10.1) and the path-hash
    # resolution helper (§10.5).
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def receptions(self, companion_name=None, message_id=None, window=None, **kwargs):
        """GET .../messages/{id}/receptions?window=24h -- every reception of
        that message's packet_hash: per-copy RSSI/SNR, incoming path,
        arrival time, whether we retransmitted it (design doc §10, first
        row of the table in §10).

        The window is measured back from now, not anchored on the
        message's own timestamp (per the design doc: a message can be
        heard again well after it first arrived), so a copy that predates
        the message but falls inside the window is included too.
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        try:
            msg_id = int(str(message_id))
        except (TypeError, ValueError):
            raise cherrypy.HTTPError(400, "Invalid message id")

        msg = handler.companion_message_get_by_id(companion_hash, msg_id)
        if msg is None:
            raise cherrypy.HTTPError(404, f"Message '{message_id}' not found")

        window_seconds = self._parse_window(window)
        now = time.time()
        since = now - window_seconds

        packet_hash = msg.get("packet_hash")
        if not packet_hash:
            # This message row has no RF correlation key (e.g. it predates
            # packet_hash persistence). That's an empty result, not an
            # error -- the message is real, it just has nothing to
            # correlate against `packets`.
            return self._success(
                {
                    "message_id": msg_id,
                    "packet_hash": None,
                    "window": window_seconds,
                    "receptions": [],
                    "observation_count": 0,
                    "unique_path_count": 0,
                    "observations_pruned": False,
                }
            )

        packet_hash_16 = _normalize_hash16(packet_hash)
        rows = handler.packets_receptions(packet_hash_16, since, now)
        contacts = handler.companion_load_contacts(companion_hash) or []

        unique_paths = set()
        receptions_out = []
        for row in rows:
            path = row.get("original_path") or []
            unique_paths.add(tuple(path))
            receptions_out.append(
                {
                    "observed_at": row["timestamp"],
                    "rssi": row.get("rssi"),
                    "snr": row.get("snr"),
                    "path": resolve_path(path, contacts),
                    "is_duplicate": bool(row.get("is_duplicate")),
                    "transmitted": bool(row.get("transmitted")),
                }
            )

        return self._success(
            {
                "message_id": msg_id,
                "packet_hash": packet_hash_16,
                "window": window_seconds,
                "receptions": receptions_out,
                # Exact counts from this query, not the running (and
                # approximate -- design doc §10.4) journal counters on the
                # message row.
                "observation_count": len(receptions_out),
                "unique_path_count": len(unique_paths),
                "observations_pruned": observations_pruned(since, self.config),
            }
        )

    # Message limit for contact-paths sender resolution (design doc §10 /
    # §10.1): bounded so one prolific contact can't turn the endpoint into
    # an unbounded scan.
    _CONTACT_PATHS_MESSAGE_LIMIT = 200

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def paths(self, companion_name=None, contact_pubkey=None, window=None, **kwargs):
        """GET .../contacts/{pubkey}/paths?window=24h -- incoming path
        aggregation for a contact within the window: distinct paths seen on
        their traffic, with counts and RSSI/SNR stats (design doc §10,
        second row of the table).

        The contact does not need to exist in companion_contacts to be
        queried -- resolution is via companion_messages.sender_key, which
        exists independent of whether the sender was ever saved as a
        contact.
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        sender_key = self._pub_key_from_hex(contact_pubkey)
        window_seconds = self._parse_window(window)
        now = time.time()
        since = now - window_seconds

        msg_rows = handler.companion_messages_by_sender(
            companion_hash, sender_key, since, now, limit=self._CONTACT_PATHS_MESSAGE_LIMIT
        )
        packet_hashes = []
        seen_hashes = set()
        for row in msg_rows:
            ph = _normalize_hash16(row.get("packet_hash"))
            if ph and ph not in seen_hashes:
                seen_hashes.add(ph)
                packet_hashes.append(ph)

        contacts = handler.companion_load_contacts(companion_hash) or []

        # path tuple -> running aggregate
        aggregates: dict = {}
        total_observations = 0
        for ph in packet_hashes:
            for row in handler.packets_receptions(ph, since, now):
                path = tuple(row.get("original_path") or [])
                total_observations += 1
                stat = aggregates.setdefault(
                    path,
                    {
                        "count": 0,
                        "first_seen": row["timestamp"],
                        "last_seen": row["timestamp"],
                        "rssi_values": [],
                        "snr_values": [],
                    },
                )
                stat["count"] += 1
                stat["first_seen"] = min(stat["first_seen"], row["timestamp"])
                stat["last_seen"] = max(stat["last_seen"], row["timestamp"])
                if row.get("rssi") is not None:
                    stat["rssi_values"].append(row["rssi"])
                if row.get("snr") is not None:
                    stat["snr_values"].append(row["snr"])

        paths_out = []
        for path, stat in aggregates.items():
            path_list = list(path)
            rssi_values = stat["rssi_values"]
            snr_values = stat["snr_values"]
            resolved_path = resolve_path(path_list, contacts)
            paths_out.append(
                {
                    "path": resolved_path,
                    "count": stat["count"],
                    "first_seen": stat["first_seen"],
                    "last_seen": stat["last_seen"],
                    "rssi_min": min(rssi_values) if rssi_values else None,
                    "rssi_max": max(rssi_values) if rssi_values else None,
                    "rssi_avg": (
                        sum(rssi_values) / len(rssi_values) if rssi_values else None
                    ),
                    "snr_min": min(snr_values) if snr_values else None,
                    "snr_max": max(snr_values) if snr_values else None,
                    "snr_avg": sum(snr_values) / len(snr_values) if snr_values else None,
                    "first_hop": resolved_path[0] if resolved_path else None,
                    "last_hop": resolved_path[-1] if resolved_path else None,
                }
            )
        # Most-observed path first -- the useful default ordering for a
        # "route diversity" view.
        paths_out.sort(key=lambda p: p["count"], reverse=True)

        return self._success(
            {
                "contact_pubkey": contact_pubkey.lower(),
                "window": window_seconds,
                "paths": paths_out,
                "total_observations": total_observations,
                "messages_scanned": len(msg_rows),
                "message_limit": self._CONTACT_PATHS_MESSAGE_LIMIT,
                "observations_pruned": observations_pruned(since, self.config),
            }
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def repeats(self, companion_name=None, packet_hash=None, window=None, **kwargs):
        """GET .../transmissions/{packet_hash}/repeats?window=24h -- heard
        repeats of our own transmission: receptions of the same packet_hash
        after we transmitted it (design doc §10, third row of the table;
        predicate exactly per §10.3).

        ``packet_hash`` accepts either the full or the already-truncated
        16-char form; both normalize to the same lookup key.
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        packet_hash_16 = _normalize_hash16(packet_hash)
        if not packet_hash_16 or not _is_hex(packet_hash_16):
            raise cherrypy.HTTPError(400, "Invalid packet_hash")

        window_seconds = self._parse_window(window)
        now = time.time()
        since = now - window_seconds

        tx_rows = handler.packets_transmissions(packet_hash_16, since, now)
        if not tx_rows:
            raise cherrypy.HTTPError(404, "Transmission not found")
        transmitted_at = min(row["timestamp"] for row in tx_rows)

        repeat_rows = handler.packets_heard_repeats(packet_hash_16, transmitted_at, now)
        contacts = handler.companion_load_contacts(companion_hash) or []

        repeats_out = []
        unique_terminal_hashes = set()
        for row in repeat_rows:
            path = row.get("original_path") or []
            terminal_raw = path[-1] if path else None
            if terminal_raw is not None:
                unique_terminal_hashes.add(terminal_raw)
            terminal_resolved = (
                resolve_path([terminal_raw], contacts)[0] if terminal_raw is not None else None
            )
            repeats_out.append(
                {
                    "observed_at": row["timestamp"],
                    "rssi": row.get("rssi"),
                    "snr": row.get("snr"),
                    "path": resolve_path(path, contacts),
                    "terminal_repeater": terminal_resolved,
                }
            )

        return self._success(
            {
                "packet_hash": packet_hash_16,
                "transmitted_at": transmitted_at,
                "window": window_seconds,
                # Every matching OTA copy counts (a repeater heard twice
                # counts twice) vs. distinct terminal hashes -- neither is
                # collapsed into the other (design doc §10.3). Both, plus
                # the hash-changing-rebroadcast caveat in §10.3, make these
                # a lower bound, not an exact repeater census.
                "repeats": repeats_out,
                "heard_repeat_count": len(repeats_out),
                "unique_repeater_count": len(unique_terminal_hashes),
                "observations_pruned": observations_pruned(since, self.config),
            }
        )


class PairV1:
    """``/api/v1/pair`` and ``/api/v1/pair/start`` — QR pairing (design doc
    §11.2).

    Pairing codes live in an in-memory dict on this instance, guarded by a
    lock (a single process serves the whole daemon, and codes are 5-minute
    TTL by design — nothing here needs to survive a restart). Not reused
    from ``CompanionsV1._resolve``: pairing has its own auth shape (``start``
    requires admin scope explicitly; the exchange endpoint has no auth at
    all — the code *is* the credential), so it deliberately does not route
    through ``_resolve``'s per-companion scope gate.
    """

    _TTL_SEC = 300.0
    _RATE_LIMIT_MAX = 10
    _RATE_LIMIT_WINDOW_SEC = 60.0

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop
        self._lock = threading.Lock()
        self._codes: dict = {}  # code -> {companion_name, companion_hash, created_at}
        self._attempts: list = []  # POST /pair attempt timestamps, for rate limiting

    # ------------------------------------------------------------------
    # Small helpers (deliberately not shared with CompanionsV1 — pairing's
    # auth/resolve shape differs enough that reuse would be more confusing
    # than a few duplicated lines; see class docstring).
    # ------------------------------------------------------------------

    @staticmethod
    def _success(data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    @staticmethod
    def _require_post():
        if cherrypy.request.method != "POST":
            cherrypy.response.headers["Allow"] = "POST"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST.")

    def _get_json_body(self) -> dict:
        try:
            raw = cherrypy.request.body.read()
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid JSON body: {exc}")

    @staticmethod
    def _check_admin_scope() -> None:
        """pair/start is admin-only (design doc §11.2 step 1). Same
        legacy-scope-less-dict-is-admin rule as
        ``CompanionsV1._check_scope``."""
        user = getattr(cherrypy.request, "user", None)
        if user is None or user.get("scope", "admin") != "admin":
            raise cherrypy.HTTPError(403, "Admin scope required")

    def _resolve_companion(self, name: Optional[str]) -> Tuple[str, bytes]:
        """Return ``(companion_hash_str, public_key_bytes)`` for a companion
        name, 404 if unknown (mirrors the identity_manager lookup in
        ``CompanionsV1._resolve``, without the bridge/scope-gate parts that
        don't apply here)."""
        if not name:
            raise cherrypy.HTTPError(400, "companion_name required")
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        identity_manager = getattr(self.daemon_instance, "identity_manager", None)
        if identity_manager:
            for reg_name, identity, _cfg in identity_manager.get_identities_by_type(
                "companion"
            ):
                if reg_name == name:
                    pub_key = identity.get_public_key()
                    return f"0x{pub_key[0]:02x}", pub_key  # noqa: E231
        raise cherrypy.HTTPError(404, f"Companion '{name}' not found")

    def _get_sqlite_handler(self):
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

    def _sweep_expired_locked(self) -> None:
        """Drop expired codes. Caller must hold ``self._lock``."""
        now = time.time()
        expired = [
            code
            for code, entry in self._codes.items()
            if now - entry["created_at"] > self._TTL_SEC
        ]
        for code in expired:
            del self._codes[code]

    def _check_rate_limit(self) -> None:
        """Small in-memory fixed-window counter (design doc §11.3): max
        ``_RATE_LIMIT_MAX`` POST /pair attempts per ``_RATE_LIMIT_WINDOW_SEC``
        across all callers. A single global counter is fine for a Pi-class
        single-tenant deployment; applied before code lookup so it blunts
        guessing regardless of whether the guessed code exists."""
        with self._lock:
            now = time.time()
            self._attempts = [
                t for t in self._attempts if now - t < self._RATE_LIMIT_WINDOW_SEC
            ]
            if len(self._attempts) >= self._RATE_LIMIT_MAX:
                raise cherrypy.HTTPError(429, "Too many pairing attempts, try again later")
            self._attempts.append(now)

    # ------------------------------------------------------------------
    # POST /api/v1/pair/start  (admin scope, design doc §11.2 step 1)
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def start(self, **kwargs):
        """Generate a single-use, 5-minute pairing code for a companion.

        Body: ``{companion_name}``. The device's name arrives from the app
        at the exchange step (``POST /pair``'s required ``name``), so this
        endpoint deliberately takes no device label — an operator-typed
        guess would just be superseded seconds later.
        Response data: ``{code, expires_in, companion_name,
        fingerprint}`` — ``fingerprint`` is the sha256 hexdigest of the
        companion identity's public key; this is what the app pins on
        first pair (TOFU, §11.3) to detect later server substitution even
        without TLS. Assembling the QR code / pairing URL from these
        ingredients is the web UI's job, not this endpoint's.
        """
        self._require_post()
        self._check_admin_scope()
        body = self._get_json_body()
        companion_name = body.get("companion_name")
        companion_hash, pub_key = self._resolve_companion(companion_name)

        code = secrets.token_hex(16)  # 128-bit pairing code (§11.3)
        fingerprint = hashlib.sha256(pub_key).hexdigest()
        with self._lock:
            self._sweep_expired_locked()
            self._codes[code] = {
                "companion_name": companion_name,
                "companion_hash": companion_hash,
                "created_at": time.time(),
            }

        return self._success(
            {
                "code": code,
                "expires_in": int(self._TTL_SEC),
                "companion_name": companion_name,
                "fingerprint": fingerprint,
            }
        )

    # ------------------------------------------------------------------
    # POST /api/v1/pair  (no auth — the pairing code is the credential)
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self, **kwargs):
        """Exchange a pairing code for a device API token (design doc
        §11.2 steps 2-3).

        Deliberately NOT decorated with ``@require_auth`` — the pairing
        code itself is the credential. http_server.py exempts
        ``"/api/v1/pair"`` from the tool-level ``require_auth`` config;
        because CherryPy config paths cascade to descendants, that same
        exemption also covers ``"/api/v1/pair/start"``, which is why
        ``start()`` above carries its own ``@require_auth`` decorator
        instead of relying on the tool-level gate.

        Body: ``{code, device_id, name, platform?}``. The code is consumed
        atomically (popped under the lock) so a code can only ever produce
        one token; unknown or expired codes both 404 with the same message
        so a caller can't distinguish "wrong code" from "code expired".
        """
        self._require_post()
        self._check_rate_limit()

        body = self._get_json_body()
        code = body.get("code")
        device_id = body.get("device_id")
        name = body.get("name")
        platform = body.get("platform")
        if not code or not device_id or not name:
            raise cherrypy.HTTPError(400, "code, device_id, and name are required")

        with self._lock:
            self._sweep_expired_locked()
            entry = self._codes.pop(code, None)
        if entry is None:
            raise cherrypy.HTTPError(404, "Invalid or expired pairing code")

        companion_name = entry["companion_name"]
        companion_hash = entry["companion_hash"]

        token_manager = cherrypy.config.get("token_manager")
        if not token_manager:
            raise cherrypy.HTTPError(500, "Authentication not configured")

        scope = f"companion:{companion_name}"  # noqa: E231
        token_id, plaintext = token_manager.create_token(name=f"device:{name}", scope=scope)

        handler = self._get_sqlite_handler()
        created = handler.companion_device_create(
            companion_hash, device_id, name, token_id, platform
        )
        if created is None:
            # device_id already registered (UNIQUE constraint) -- the token
            # we just minted is an orphan; clean it up rather than leaving
            # an unreachable credential behind.
            handler.revoke_api_token(token_id)
            raise cherrypy.HTTPError(409, "device_id already registered")

        return self._success(
            {
                "token": plaintext,
                "device_id": device_id,
                "companion_name": companion_name,
                "scope": scope,
            }
        )


class DevicesV1:
    """``/api/v1/devices`` — admin-only paired-device registry (design doc
    §11.2 step 4)."""

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop

    def _cp_dispatch(self, vpath):
        """Route ``DELETE /devices/{device_id}`` and
        ``POST|DELETE /devices/{device_id}/push``. ``GET /devices`` (empty
        vpath) resolves to ``index`` through CherryPy's normal dispatch."""
        if len(vpath) == 1:
            device_id = vpath.pop(0)
            cherrypy.request.params["device_id"] = device_id
            return self.delete
        if len(vpath) == 2 and vpath[1] == "push":
            device_id = vpath.pop(0)
            vpath.pop(0)  # literal 'push' segment
            cherrypy.request.params["device_id"] = device_id
            return self.push
        return None

    @staticmethod
    def _success(data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    @staticmethod
    def _check_admin_scope() -> None:
        user = getattr(cherrypy.request, "user", None)
        if user is None or user.get("scope", "admin") != "admin":
            raise cherrypy.HTTPError(403, "Admin scope required")

    def _get_sqlite_handler(self):
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

    # ------------------------------------------------------------------
    # GET /api/v1/devices
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def index(self, **kwargs):
        """List paired devices across all companions. ``last_seen`` is
        filled in from the linked api_token's ``last_used`` when that is
        newer than the device row's own ``last_seen`` — presence is derived
        from token use rather than a per-request DB write here (no write
        amplification for something that's purely informational)."""
        if cherrypy.request.method not in ("GET", "OPTIONS"):
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")
        self._check_admin_scope()
        handler = self._get_sqlite_handler()

        last_used_by_token = {t["id"]: t.get("last_used") for t in handler.list_api_tokens()}

        items = []
        for device in handler.companion_device_list():
            item = dict(device)
            token_last_used = last_used_by_token.get(device["token_id"])
            if token_last_used is not None and (
                item["last_seen"] is None or token_last_used > item["last_seen"]
            ):
                item["last_seen"] = token_last_used
            items.append(item)
        return self._success(items)

    # ------------------------------------------------------------------
    # DELETE /api/v1/devices/{device_id}  (revoke, routed via _cp_dispatch)
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def delete(self, device_id=None, **kwargs):
        """Revoke a paired device: deletes its api_token row (the next
        request with that token 401s naturally) and its companion_devices
        row."""
        if cherrypy.request.method != "DELETE":
            cherrypy.response.headers["Allow"] = "DELETE"
            raise cherrypy.HTTPError(405, "Method not allowed. Use DELETE.")
        self._check_admin_scope()
        handler = self._get_sqlite_handler()
        device = handler.companion_device_get(device_id)
        if device is None:
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
        handler.revoke_api_token(device["token_id"])
        handler.companion_device_delete(device_id)
        return self._success({"revoked": True, "device_id": device_id})

    # ------------------------------------------------------------------
    # POST | DELETE /api/v1/devices/{device_id}/push  (routed via _cp_dispatch)
    # ------------------------------------------------------------------

    _VALID_PUSH_DETAIL = ("none", "count", "preview")

    def _get_json_body(self) -> dict:
        try:
            raw = cherrypy.request.body.read()
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid JSON body: {exc}")

    def _check_device_or_admin(self, handler, device_id: str) -> None:
        """A device manages its own push registration; admins manage any.

        Admin scope (web UI / admin token) passes unconditionally. Otherwise
        the caller must be authenticating with the very device-token paired to
        ``device_id`` — a scoped device token can register push only for
        itself, never for another device (mirrors the 404-folding choke point
        the rest of /api/v1 uses so a token can't probe other device ids).
        """
        user = getattr(cherrypy.request, "user", None) or {}
        if user.get("scope", "admin") == "admin":
            return
        token_id = user.get("token_id")
        if token_id is not None:
            own = handler.companion_device_get_by_token(token_id)
            if own is not None and own["device_id"] == device_id:
                return
        # Indistinguishable from "device not found" for a non-owning caller
        # (no cross-device existence leak).
        raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")

    @staticmethod
    def _validate_relay_url(url):
        """Relay URL is client-supplied (design doc §12.2): require an
        absolute http(s) URL with a host. Private/LAN targets are allowed on
        purpose — self-hosted relays are a supported deployment."""
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise cherrypy.HTTPError(400, "push_relay_url must be an absolute http(s) URL")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def push(self, device_id=None, **kwargs):
        """Register (POST) or clear (DELETE) a device's push credentials
        (design doc §12.2). POST body: ``{push_token, push_relay_url?,
        push_detail?}`` — ``push_token`` required; ``push_detail`` defaults to
        the stored value (``none`` for a fresh device). Auth: the device's own
        token, or admin."""
        method = cherrypy.request.method
        if method not in ("POST", "DELETE", "OPTIONS"):
            cherrypy.response.headers["Allow"] = "POST, DELETE"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST or DELETE.")
        handler = self._get_sqlite_handler()
        self._check_device_or_admin(handler, device_id)

        if method == "DELETE":
            if not handler.companion_device_clear_push(device_id):
                raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
            return self._success({"unregistered": True, "device_id": device_id})

        body = self._get_json_body()
        push_token = body.get("push_token")
        if not push_token or not isinstance(push_token, str):
            raise cherrypy.HTTPError(400, "push_token is required")

        push_relay_url = body.get("push_relay_url")
        if push_relay_url is not None:
            self._validate_relay_url(push_relay_url)

        push_detail = body.get("push_detail")
        if push_detail is not None and push_detail not in self._VALID_PUSH_DETAIL:
            raise cherrypy.HTTPError(
                400, f"push_detail must be one of {', '.join(self._VALID_PUSH_DETAIL)}"
            )

        if not handler.companion_device_set_push(
            device_id, push_token, push_relay_url=push_relay_url, push_detail=push_detail
        ):
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")

        device = handler.companion_device_get(device_id)
        return self._success(
            {
                "device_id": device_id,
                "push_detail": device["push_detail"] if device else (push_detail or "none"),
                "registered": True,
            }
        )
