"""
Mobile Companion API v1 endpoints (phase 1 sync core + phase 2 SSE stream +
actions).

Mounted as ``APIEndpoints.v1`` so CherryPy serves it at ``/api/v1/``.
Implements the synchronization and action surface from
docs/architecture/mobile-companion-api.md §7:

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
Pairing (§11.2) arrives in a later phase.
"""

import asyncio
import hashlib
import json
import logging
import queue
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Optional, Tuple

import cherrypy

from .auth.middleware import require_auth
from .companion_endpoints import _to_json_safe

logger = logging.getLogger("MobileAPI")

try:
    from repeater._version import __version__ as _REPEATER_VERSION
except Exception:  # pragma: no cover - version metadata is best-effort
    _REPEATER_VERSION = None


class MobileAPIEndpoints:
    """Root of the ``/api/v1/`` tree (attach as ``APIEndpoints.v1``)."""

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop
        self.companions = CompanionsV1(daemon_instance, self.config, event_loop=event_loop)


class CompanionsV1:
    """``/api/v1/companions`` collection and per-companion sync/action resources."""

    _ACTIONS = ("snapshot", "sync", "messages", "events")
    # Sub-resource actions under /companions/{name}/contacts/{pubkey}/{action}
    # (§7.3). All four are POST-only JSON handlers with no idempotency
    # requirement (only message sends transmit RF that a duplicate retry
    # would repeat).
    _CONTACT_ACTIONS = ("login", "status_request", "telemetry_request", "reset_path")

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config or {}
        self.event_loop = event_loop

    # ------------------------------------------------------------------
    # Dispatch / helpers
    # ------------------------------------------------------------------

    def _cp_dispatch(self, vpath):
        """Route ``/companions/{name}/{action}`` and
        ``/companions/{name}/contacts/{pubkey}/{action}`` to their handlers.

        The companion name segment becomes the ``companion_name`` request
        param; for the contacts form, the pubkey segment becomes
        ``contact_pubkey``. Unknown actions fall through to CherryPy's 404.
        """
        if len(vpath) == 2:
            name = vpath.pop(0)
            action = vpath.pop(0)
            if action in self._ACTIONS:
                cherrypy.request.params["companion_name"] = name
                return getattr(self, action)
        elif len(vpath) == 4 and vpath[1] == "contacts" and vpath[3] in self._CONTACT_ACTIONS:
            name = vpath.pop(0)
            vpath.pop(0)  # literal 'contacts' segment
            pubkey = vpath.pop(0)
            action = vpath.pop(0)
            cherrypy.request.params["companion_name"] = name
            cherrypy.request.params["contact_pubkey"] = pubkey
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
            result = self._run_async(bridge.send_text_message(pub_key, text, txt_type=txt_type))
            sent_ok = result.success
            response = self._success(
                {
                    "sent": result.success,
                    "is_flood": result.is_flood,
                    "expected_ack": result.expected_ack,
                }
            )
        else:
            idx = int(channel_idx)
            sent_ok = self._run_async(bridge.send_channel_message(idx, text))
            response = self._success({"sent": sent_ok})

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
    def events(self, companion_name=None, cursor=None, **kwargs):
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
        """
        self._require_get()
        _bridge, companion_hash = self._resolve(companion_name)
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
