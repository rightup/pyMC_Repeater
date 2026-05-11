"""
Companion Bridge REST API and SSE event stream endpoints.

Mounted as a nested CherryPy object at /api/companion/ via APIEndpoints.
Provides browser-accessible REST endpoints that proxy into the CompanionBridge
async methods, plus a Server-Sent Events stream for real-time push callbacks.
"""

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Optional

import cherrypy

from repeater.companion.utils import validate_companion_node_name

from .auth.middleware import require_auth

logger = logging.getLogger("CompanionAPI")


class CompanionAPIEndpoints:
    """REST + SSE endpoints for a companion bridge.

    CherryPy auto-mounts this at ``/api/companion/`` when assigned as
    ``APIEndpoints.companion``.  All async bridge calls are dispatched
    to the daemon's event loop via ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(self, daemon_instance=None, event_loop=None, config=None, config_manager=None):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.config = config or {}
        self.config_manager = config_manager

        http_cfg = self.config.get("http", {}) if isinstance(self.config, dict) else {}
        self._sse_queue_maxsize = max(32, int(http_cfg.get("sse_queue_maxsize", 64)))
        self._sse_keepalive_sec = max(5, int(http_cfg.get("sse_keepalive_sec", 15)))

        # SSE clients: each gets a thread-safe queue
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()

        # Flag: have we registered push callbacks yet?
        self._callbacks_registered = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_bridge(self, name: Optional[str] = None, companion_hash: Optional[int] = None):
        """Return the companion bridge, or raise 503/404 if unavailable.

        Resolution order (mirrors room-server pattern):
        1. *name* — look up via identity_manager by registered name.
        2. *companion_hash* — direct lookup in ``companion_bridges`` dict.
        3. Neither — return the first (and typically only) bridge.
        """
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        bridges = getattr(self.daemon_instance, "companion_bridges", {})
        if not bridges:
            raise cherrypy.HTTPError(503, "No companion bridges configured")

        # --- resolve by name via identity_manager (same pattern as room servers) ---
        if name is not None:
            identity_manager = getattr(self.daemon_instance, "identity_manager", None)
            if identity_manager:
                for reg_name, identity, _cfg in identity_manager.get_identities_by_type(
                    "companion"
                ):
                    if reg_name == name:
                        hash_byte = identity.get_public_key()[0]
                        bridge = bridges.get(hash_byte)
                        if bridge:
                            return bridge
            raise cherrypy.HTTPError(404, f"Companion '{name}' not found")

        # --- resolve by hash (fallback) ---
        if companion_hash is not None:
            bridge = bridges.get(companion_hash)
            if not bridge:
                msg = f"Companion 0x{companion_hash:02X} not found"  # noqa: E231
                raise cherrypy.HTTPError(404, msg)
            return bridge

        # --- default: first bridge ---
        return next(iter(bridges.values()))

    def _resolve_bridge_params(self, params) -> dict:
        """Extract optional companion name/hash from request params.

        Returns kwargs suitable for ``_get_bridge(**result)``.
        Follows the room-server convention: ``companion_name`` is the
        primary selector, ``companion_hash`` is the fallback.
        """
        name = params.get("companion_name")
        raw_hash = params.get("companion_hash")
        result: dict = {}
        if name is not None:
            result["name"] = str(name)
        elif raw_hash is not None:
            try:
                result["companion_hash"] = int(str(raw_hash), 0)
            except (ValueError, TypeError):
                raise cherrypy.HTTPError(400, "Invalid companion_hash")
        return result

    def _run_async(self, coro, timeout: float = 30.0):
        """Run an async coroutine on the daemon event loop and return result."""
        if self.event_loop is None:
            raise cherrypy.HTTPError(503, "Event loop not available")
        future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        return future.result(timeout=timeout)

    @staticmethod
    def _success(data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    @staticmethod
    def _error(msg):
        return {"success": False, "error": str(msg)}

    def _require_post(self):
        if cherrypy.request.method != "POST":
            cherrypy.response.headers["Allow"] = "POST"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST.")

    def _get_json_body(self) -> dict:
        """Read and parse the JSON request body."""
        try:
            raw = cherrypy.request.body.read()
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid JSON body: {exc}")

    def _pub_key_from_hex(self, hex_str: str) -> bytes:
        """Decode a hex public key, raising 400 on error."""
        try:
            key = bytes.fromhex(hex_str)
            if len(key) != 32:
                raise ValueError("Expected 32-byte key")
            return key
        except (ValueError, TypeError) as exc:
            raise cherrypy.HTTPError(400, f"Invalid public key: {exc}")

    def _get_sqlite_handler(self):
        """Return the repeater's sqlite_handler, or raise 503 if unavailable."""
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        if (
            not hasattr(self.daemon_instance, "repeater_handler")
            or not self.daemon_instance.repeater_handler
        ):
            raise cherrypy.HTTPError(503, "Repeater handler not initialized")
        storage = getattr(self.daemon_instance.repeater_handler, "storage", None)
        if not storage:
            raise cherrypy.HTTPError(503, "Storage not initialized")
        sqlite_handler = getattr(storage, "sqlite_handler", None)
        if not sqlite_handler:
            raise cherrypy.HTTPError(503, "SQLite storage not available")
        return sqlite_handler

    # ------------------------------------------------------------------
    # SSE push-event plumbing
    # ------------------------------------------------------------------

    def _ensure_callbacks(self):
        """Register push callbacks on the bridge (once)."""
        if self._callbacks_registered:
            return
        try:
            bridge = self._get_bridge()
        except cherrypy.HTTPError:
            return  # bridge not yet available

        def _make_cb(event_name):
            """Create a callback that serialises event data for SSE clients."""

            def _cb(*args, **kwargs):
                payload = self._serialise_event(event_name, args, kwargs)
                self._broadcast_sse(payload)

            return _cb

        callback_names = [
            "message_received",
            "channel_message_received",
            "advert_received",
            "contact_path_updated",
            "send_confirmed",
            "login_result",
        ]
        for name in callback_names:
            register_fn = getattr(bridge, f"on_{name}", None)
            if register_fn:
                register_fn(_make_cb(name))

        self._callbacks_registered = True

    @staticmethod
    def _serialise_event(event_name: str, args: tuple, kwargs: dict) -> dict:
        """Convert callback arguments to a JSON-safe dict."""
        data: dict = {"event": event_name, "timestamp": int(time.time())}
        for i, arg in enumerate(args):
            data[f"arg{i}"] = _to_json_safe(arg)
        for k, v in kwargs.items():
            data[k] = _to_json_safe(v)
        return data

    def _broadcast_sse(self, payload: dict):
        """Put *payload* into every active SSE client queue."""
        with self._sse_lock:
            dead = []
            for q in self._sse_clients:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._sse_clients.remove(q)

    # ==================================================================
    # REST Endpoints
    # ==================================================================

    # ----- Index / listing -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def index(self, **kwargs):
        """GET /api/companion/ — list configured companions."""
        bridges = getattr(self.daemon_instance, "companion_bridges", {})
        identity_manager = getattr(self.daemon_instance, "identity_manager", None)

        # Build name lookup from identity_manager (same pattern as room servers)
        name_by_hash: dict[int, str] = {}
        if identity_manager:
            for reg_name, identity, _cfg in identity_manager.get_identities_by_type("companion"):
                name_by_hash[identity.get_public_key()[0]] = reg_name

        items = []
        for h, b in bridges.items():
            items.append(
                {
                    "companion_name": name_by_hash.get(h, ""),
                    "companion_hash": f"0x{h:02X}",  # noqa: E231
                    "node_name": b.prefs.node_name,
                    "public_key": b.get_public_key().hex(),
                    "is_running": b.is_running,
                    "contacts_count": b.contacts.get_count(),
                    "channels_count": b.channels.get_count(),
                }
            )
        return self._success(items)

    # ----- Identity -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def self_info(self, **kwargs):
        """GET /api/companion/self_info — node identity and preferences."""
        bridge = self._get_bridge(**self._resolve_bridge_params(kwargs))
        prefs = bridge.get_self_info()
        return self._success(
            {
                "public_key": bridge.get_public_key().hex(),
                "node_name": prefs.node_name,
                "adv_type": prefs.adv_type,
                "tx_power_dbm": prefs.tx_power_dbm,
                "frequency_hz": prefs.frequency_hz,
                "bandwidth_hz": prefs.bandwidth_hz,
                "spreading_factor": prefs.spreading_factor,
                "coding_rate": prefs.coding_rate,
                "latitude": prefs.latitude,
                "longitude": prefs.longitude,
            }
        )

    # ----- Contacts -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def contacts(self, **kwargs):
        """GET /api/companion/contacts — list all contacts."""
        bridge = self._get_bridge(**self._resolve_bridge_params(kwargs))
        since = int(kwargs.get("since", 0))
        contacts = bridge.get_contacts(since=since)
        items = []
        for c in contacts:
            items.append(
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
        return self._success(items)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def contact(self, **kwargs):
        """GET /api/companion/contact?pub_key=<hex> — get single contact."""
        bridge = self._get_bridge(**self._resolve_bridge_params(kwargs))
        pk_hex = kwargs.get("pub_key")
        if not pk_hex:
            raise cherrypy.HTTPError(400, "pub_key required")
        pub_key = self._pub_key_from_hex(pk_hex)
        c = bridge.get_contact_by_key(pub_key)
        if not c:
            raise cherrypy.HTTPError(404, "Contact not found")
        return self._success(
            {
                "public_key": (
                    c.public_key.hex() if isinstance(c.public_key, bytes) else c.public_key
                ),
                "name": c.name,
                "adv_type": c.adv_type,
                "flags": c.flags,
                "out_path_len": c.out_path_len,
                "out_path": c.out_path.hex() if isinstance(c.out_path, bytes) else "",
                "last_advert_timestamp": c.last_advert_timestamp,
                "lastmod": c.lastmod,
                "gps_lat": c.gps_lat,
                "gps_lon": c.gps_lon,
            }
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def import_repeater_contacts(self, **kwargs):
        """POST /api/companion/import_repeater_contacts  {companion_name, contact_types?, hours?, limit?}

        Import repeater adverts into this companion's contact store (one-time seed).
        Optional: contact_types (list), hours (only adverts seen in last N hours),
        limit (max contacts to import, capped by companion max_contacts).
        Results are sorted by last_seen DESC. After import, contacts are hot-reloaded.
        """
        self._require_post()
        body = self._get_json_body()
        companion_name = body.get("companion_name")
        if not companion_name:
            raise cherrypy.HTTPError(400, "companion_name required")
        contact_types = body.get("contact_types")
        if contact_types is not None:
            if not isinstance(contact_types, list):
                raise cherrypy.HTTPError(400, "contact_types must be a list")
            allowed = {"companion", "repeater", "room_server", "sensor"}
            for t in contact_types:
                if not isinstance(t, str) or t not in allowed:
                    raise cherrypy.HTTPError(
                        400,
                        f"contact_types must contain only: companion, repeater, room_server, sensor (got {t!r})",
                    )
            if not contact_types:
                contact_types = None
        hours = body.get("hours")
        if hours is not None:
            try:
                hours = int(hours)
            except (TypeError, ValueError):
                raise cherrypy.HTTPError(400, "hours must be a positive integer")
            if hours < 1:
                raise cherrypy.HTTPError(400, "hours must be a positive integer")
        limit = body.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise cherrypy.HTTPError(400, "limit must be a positive integer")
            if limit < 1:
                raise cherrypy.HTTPError(400, "limit must be a positive integer")
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        if limit is not None:
            max_contacts = getattr(bridge, "max_contacts", 1000)
            limit = min(limit, max_contacts)
        companion_hash = getattr(bridge, "_companion_hash", None)
        if not companion_hash:
            raise cherrypy.HTTPError(503, "Companion hash not available")
        sqlite_handler = self._get_sqlite_handler()
        count = sqlite_handler.companion_import_repeater_contacts(
            companion_hash,
            contact_types=contact_types,
            hours=hours,
            limit=limit,
        )
        contact_rows = sqlite_handler.companion_load_contacts(companion_hash)
        if contact_rows:
            records = []
            for row in contact_rows:
                d = dict(row)
                d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                records.append(d)
            bridge.contacts.load_from_dicts(records)
        return self._success({"imported": count})

    # ----- Channels -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def channels(self, **kwargs):
        """GET /api/companion/channels — list configured channels."""
        try:
            bridge = self._get_bridge(**self._resolve_bridge_params(kwargs))
            items = []
            for idx in range(bridge.channels.max_channels):
                ch = bridge.channels.get(idx)
                if ch:
                    items.append(
                        {
                            "index": idx,
                            "name": ch.name,
                            # Don't expose the PSK secret over REST
                        }
                    )
            return self._success(items)
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.error(f"channels endpoint error: {exc}", exc_info=True)
            return self._error(str(exc))

    # ----- Statistics -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def stats(self, **kwargs):
        """GET /api/companion/stats?type=packets — local companion stats."""
        bridge = self._get_bridge(**self._resolve_bridge_params(kwargs))
        stats_type_map = {"core": 0, "radio": 1, "packets": 2}
        stype = stats_type_map.get(kwargs.get("type", "packets"), 2)
        return self._success(bridge.get_stats(stype))

    # ----- Messaging -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def send_text(self, **kwargs):
        """POST /api/companion/send_text  {pub_key, text, txt_type?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        text = body.get("text", "")
        if not text:
            raise cherrypy.HTTPError(400, "text required")
        txt_type = int(body.get("txt_type", 0))
        result = self._run_async(bridge.send_text_message(pub_key, text, txt_type=txt_type))
        return self._success(
            {
                "sent": result.success,
                "is_flood": result.is_flood,
                "expected_ack": result.expected_ack,
            }
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def send_channel_message(self, **kwargs):
        """POST /api/companion/send_channel_message  {channel_idx, text, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        channel_idx = int(body.get("channel_idx", 0))
        text = body.get("text", "")
        if not text:
            raise cherrypy.HTTPError(400, "text required")
        success = self._run_async(bridge.send_channel_message(channel_idx, text))
        return self._success({"sent": success})

    # ----- Login -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def login(self, **kwargs):
        """POST /api/companion/login  {pub_key, password?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        password = body.get("password", "")
        result = self._run_async(bridge.send_login(pub_key, password), timeout=15.0)
        return self._success(_to_json_safe(result))

    # ----- Status / Telemetry Requests -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def request_status(self, **kwargs):
        """POST /api/companion/request_status  {pub_key, timeout?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        timeout = float(body.get("timeout", 15.0))
        result = self._run_async(
            bridge.send_status_request(pub_key, timeout=timeout),
            timeout=timeout + 5.0,
        )
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def request_telemetry(self, **kwargs):
        """POST /api/companion/request_telemetry.

        Body: pub_key, want_base?, want_location?, want_environment?,
        timeout?, companion_name?

        On success, telemetry_data includes raw_bytes (LPP hex), sensors (parsed),
        and frame_bytes (hex): companion-style frame 0x8B + 0 + 6B pubkey prefix + LPP.
        """
        self._require_post()
        try:
            body = self._get_json_body()
            bridge = self._get_bridge(**self._resolve_bridge_params(body))
            pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
            timeout = float(body.get("timeout", 20.0))
            result = self._run_async(
                bridge.send_telemetry_request(
                    pub_key,
                    want_base=bool(body.get("want_base", True)),
                    want_location=bool(body.get("want_location", True)),
                    want_environment=bool(body.get("want_environment", True)),
                    timeout=timeout,
                ),
                timeout=timeout + 5.0,
            )
            # Ensure all values are JSON-serialisable (telemetry may contain bytes)
            return self._success(_to_json_safe(result))
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.error(f"request_telemetry endpoint error: {exc}", exc_info=True)
            return self._error(str(exc))

    # ----- Repeater Commands -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def send_command(self, **kwargs):
        """POST /api/companion/send_command  {pub_key, command, parameters?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        command = body.get("command", "")
        if not command:
            raise cherrypy.HTTPError(400, "command required")
        parameters = body.get("parameters")
        result = self._run_async(
            bridge.send_repeater_command(pub_key, command, parameters),
            timeout=20.0,
        )
        return self._success(_to_json_safe(result))

    # ----- Path / Routing -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def reset_path(self, **kwargs):
        """POST /api/companion/reset_path  {pub_key, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        ok = bridge.reset_path(pub_key)
        return self._success({"reset": ok})

    # ----- Device Configuration -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def set_advert_name(self, **kwargs):
        """POST /api/companion/set_advert_name  {advert_name, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        name = body.get("advert_name", body.get("name", ""))
        if not name:
            raise cherrypy.HTTPError(400, "name required")
        try:
            validated_name = validate_companion_node_name(name)
        except ValueError as e:
            raise cherrypy.HTTPError(400, str(e)) from e
        bridge.set_advert_name(validated_name)
        # Optionally sync node_name to config.yaml so it survives restart
        companion_name = body.get("companion_name")
        if companion_name is None and getattr(self.daemon_instance, "identity_manager", None):
            pubkey = bridge.get_public_key()
            for reg_name, identity, _ in self.daemon_instance.identity_manager.get_identities_by_type(
                "companion"
            ):
                if identity.get_public_key() == pubkey:
                    companion_name = reg_name
                    break
        if companion_name and self.config_manager:
            companions = (self.config.get("identities") or {}).get("companions") or []
            for entry in companions:
                if entry.get("name") == companion_name:
                    if "settings" not in entry:
                        entry["settings"] = {}
                    entry["settings"]["node_name"] = validated_name
                    try:
                        if not self.config_manager.save_to_file():
                            logger.warning("Failed to save config after set_advert_name")
                    except Exception as e:
                        logger.warning("Error saving config after set_advert_name: %s", e)
                    break
        return self._success({"name": bridge.prefs.node_name})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def set_advert_location(self, **kwargs):
        """POST /api/companion/set_advert_location  {latitude, longitude, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        bridge = self._get_bridge(**self._resolve_bridge_params(body))
        lat = float(body.get("latitude", 0.0))
        lon = float(body.get("longitude", 0.0))
        bridge.set_advert_latlon(lat, lon)
        return self._success({"latitude": lat, "longitude": lon})

    # ==================================================================
    # SSE Event Stream
    # ==================================================================

    @cherrypy.expose
    def events(self, **kwargs):
        """GET /api/companion/events — Server-Sent Events stream for push callbacks.

        Connect with ``EventSource('/api/companion/events?token=JWT')``.
        Auth is handled by the CherryPy tool-level require_auth (supports
        query-param JWT tokens needed by the browser EventSource API).
        """
        self._ensure_callbacks()

        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["Connection"] = "keep-alive"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"

        client_queue: queue.Queue = queue.Queue(maxsize=self._sse_queue_maxsize)
        with self._sse_lock:
            self._sse_clients.append(client_queue)

        def generate():
            try:
                payload = {"event": "connected", "timestamp": int(time.time())}
                yield f"data: {json.dumps(payload)}\n\n"

                while True:
                    try:
                        item = client_queue.get(timeout=float(self._sse_keepalive_sec))
                        yield f"data: {json.dumps(item)}\n\n"
                    except queue.Empty:
                        # Keep-alive comment frame keeps EventSource connected
                        # without allocating additional JSON payload objects.
                        yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            except Exception as exc:
                logger.debug(f"SSE stream ended: {exc}")
            finally:
                with self._sse_lock:
                    if client_queue in self._sse_clients:
                        self._sse_clients.remove(client_queue)

        return generate()

    events._cp_config = {"response.stream": True}


# ======================================================================
# Utility: make arbitrary objects JSON-serialisable for SSE events
# ======================================================================


def _to_json_safe(obj):
    """Convert common companion objects to JSON-safe dicts/values."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, bytearray):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    # Dataclass / namedtuple with __dict__
    if hasattr(obj, "__dict__"):
        return {k: _to_json_safe(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)
