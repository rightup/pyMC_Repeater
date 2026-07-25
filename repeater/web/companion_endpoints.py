"""
Companion Bridge REST API and SSE event stream endpoints.

Mounted as a nested CherryPy object at /api/companion/ via APIEndpoints.
Provides browser-accessible REST endpoints that proxy into the CompanionBridge
async methods, plus a Server-Sent Events stream for real-time push callbacks.
"""

import asyncio
import copy
import json
import logging
import math
import queue
import threading
import time
import weakref
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Callable, Optional

import cherrypy
from openhop_core.companion.constants import ADV_TYPE_NONE, DEFAULT_OFFLINE_QUEUE_SIZE
from openhop_core.companion.models import Contact
from openhop_core.protocol.constants import MAX_TEXT_LEN

from repeater.companion.bridge import (
    ChannelTextCapacityError,
    outbound_message_source,
)
from repeater.companion.utils import (
    validate_companion_node_name,
)

from .api_validation import (
    boolean_field,
    read_json_object,
    reject_unknown_fields,
    text_field,
)
from .auth.lease import AuthorizationLease
from .auth.middleware import require_auth
from .auth.policy import is_admin_scope
from .rate_limit import (
    SSEAdmission,
    sse_stream_settings,
    validate_sse_connection_capacity,
)

logger = logging.getLogger("CompanionAPI")

_COMPANION_SELECTOR_FIELDS = {"companion_name", "companion_hash"}
_LEGACY_IMPORT_HOURS_MAX = 876_000  # 100 years; effectively "all history".
_LEGACY_IMPORT_LIMIT_MAX = 1_000_000
_LEGACY_REQUEST_TIMEOUT_MAX_SEC = 60.0
_SQLITE_SIGNED_INT_MAX = (1 << 63) - 1


class _LegacyClosingIterator:
    """Run stream cleanup even when the response closes before its first pull."""

    def __init__(self, iterator, on_close):
        self._iterator = iterator
        self._on_close = on_close
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterator, "close", None)
            if callable(close):
                close()
        finally:
            self._on_close()


_SSE_OVERFLOW = object()


class CompanionAPIEndpoints:
    """REST + SSE endpoints for a companion bridge.

    CherryPy auto-mounts this at ``/api/companion/`` when assigned as
    ``APIEndpoints.companion``.  All async bridge calls are dispatched
    to the daemon's event loop via ``asyncio.run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        daemon_instance=None,
        event_loop=None,
        config=None,
        config_manager=None,
        sse_admission=None,
    ):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.config = config if config is not None else {}
        self.config_manager = config_manager

        (
            self._sse_queue_maxsize,
            self._sse_keepalive_sec,
        ) = sse_stream_settings(self.config)

        # SSE clients: each gets a selected bridge key and thread-safe queue.
        self._sse_clients: list[tuple[object, queue.Queue]] = []
        self._sse_lock = threading.Lock()
        api_cfg = (
            self.config.get("mobile_api", {})
            if isinstance(self.config, dict)
            else {}
        )
        if not isinstance(api_cfg, dict):
            raise ValueError("mobile_api must be an object")
        configured_admission = SSEAdmission(
            api_cfg.get("sse_max_connections", 8)
        )
        sse_max_connections = configured_admission.max_connections
        validate_sse_connection_capacity(self.config, sse_max_connections)
        self._sse_admission = sse_admission or configured_admission
        if self._sse_admission.max_connections != sse_max_connections:
            raise ValueError(
                "shared SSE admission limit does not match "
                "mobile_api.sse_max_connections"
            )

        # Accessed only on the daemon loop. A bridge is registered at most once.
        self._callback_registrations = weakref.WeakKeyDictionary()
        # Event streams retain the exact bridge object they subscribed to.
        # A one-byte hash alone is not enough: a hot remove/re-add must close
        # the old stream rather than silently attaching it to a replacement.
        self._callback_bridges = weakref.WeakValueDictionary()

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
            if not isinstance(name, str) or not name.strip():
                raise cherrypy.HTTPError(400, "Invalid companion_name")
            result["name"] = name.strip()
        elif raw_hash is not None:
            try:
                companion_hash = int(str(raw_hash), 0)
            except (ValueError, TypeError):
                raise cherrypy.HTTPError(400, "Invalid companion_hash")
            if not 0 <= companion_hash <= 0xFF:
                raise cherrypy.HTTPError(
                    400,
                    "companion_hash must be between 0x00 and 0xFF",
                )
            result["companion_hash"] = companion_hash
        return result

    def _run_async(self, coro, timeout: float = 30.0):
        """Run an async coroutine on the daemon event loop and return result."""
        if self.event_loop is None:
            coro.close()
            raise cherrypy.HTTPError(503, "Event loop not available")
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        except Exception as exc:
            coro.close()
            logger.warning("Companion event loop unavailable: %s", exc)
            raise cherrypy.HTTPError(503, "Event loop not available") from exc
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise cherrypy.HTTPError(504, "Companion operation timed out") from exc

    async def _read_bridge_state(self, bridge_params: dict, reader: Callable):
        """Resolve and read one bridge on the daemon loop under its state lock."""
        bridge = self._get_bridge(**bridge_params)
        async with bridge.state_mutation_lock:
            return reader(bridge)

    async def _call_bridge(self, bridge_params: dict, operation: Callable):
        """Resolve one bridge on the daemon loop and run an async operation."""
        bridge = self._get_bridge(**bridge_params)
        return await operation(bridge)

    async def _call_bridge_as_operator(
        self,
        bridge_params: dict,
        operation: Callable,
    ):
        """Run one legacy HTTP message send with observable source attribution."""
        token = outbound_message_source.set("operator")
        try:
            return await self._call_bridge(bridge_params, operation)
        finally:
            outbound_message_source.reset(token)

    @staticmethod
    def _replace_contacts(bridge, contacts) -> None:
        """Replace durable contacts while preserving transient request contacts."""
        contacts = list(contacts)
        durable = [
            copy.deepcopy(contact)
            for contact in contacts
            if contact.adv_type != ADV_TYPE_NONE
        ]
        transient = [
            copy.deepcopy(contact)
            for contact in contacts
            if contact.adv_type == ADV_TYPE_NONE
        ]
        bridge.contacts.load_from(durable)
        for contact in transient:
            bridge.contacts.add_transient(contact)

    @staticmethod
    async def _notify_contact_changes(bridge, changes: list[dict]) -> None:
        notify_batch = getattr(bridge, "notify_contact_changes", None)
        if callable(notify_batch):
            await notify_batch(changes)
            return
        notify = getattr(bridge, "_notify_observers", None)
        if notify is None:
            return
        for change in changes:
            await notify(
                "contact_committed",
                change["change"],
                change["contact"],
            )

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

    def _require_get(self):
        if cherrypy.request.method != "GET":
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")

    def _get_json_body(self) -> dict:
        """Read one generous but bounded legacy operator JSON object."""
        return read_json_object(max_bytes=256 * 1024)

    @staticmethod
    def _legacy_integer(
        value,
        name: str,
        *,
        low: Optional[int] = None,
        high: Optional[int] = None,
    ) -> int:
        """Parse a historic numeric/string integer without lossy coercion."""

        if isinstance(value, bool):
            raise cherrypy.HTTPError(400, f"{name} must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise cherrypy.HTTPError(400, f"{name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            raise cherrypy.HTTPError(400, f"{name} must be an integer") from None
        if low is not None and parsed < low:
            raise cherrypy.HTTPError(400, f"{name} must be at least {low}")
        if high is not None and parsed > high:
            raise cherrypy.HTTPError(400, f"{name} must be at most {high}")
        return parsed

    @staticmethod
    def _legacy_float(
        value,
        name: str,
        *,
        low: Optional[float] = None,
        high: Optional[float] = None,
    ) -> float:
        """Parse a historic numeric/string float, rejecting bool and nonfinite."""

        if isinstance(value, bool):
            raise cherrypy.HTTPError(400, f"{name} must be a number")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise cherrypy.HTTPError(400, f"{name} must be a number") from None
        if not math.isfinite(parsed):
            raise cherrypy.HTTPError(400, f"{name} must be finite")
        if low is not None and parsed < low:
            raise cherrypy.HTTPError(400, f"{name} must be at least {low:g}")
        if high is not None and parsed > high:
            raise cherrypy.HTTPError(400, f"{name} must be at most {high:g}")
        return parsed

    @classmethod
    def _legacy_timeout(cls, value, default: float) -> float:
        """Return a finite request timeout in the operator API's 1..60s range."""

        return cls._legacy_float(
            default if value is None else value,
            "timeout",
            low=1.0,
            high=_LEGACY_REQUEST_TIMEOUT_MAX_SEC,
        )

    def _pub_key_from_hex(self, hex_str: str) -> bytes:
        """Decode a hex public key, raising 400 on error."""
        try:
            if (
                not isinstance(hex_str, str)
                or len(hex_str) != 64
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in hex_str
                )
            ):
                raise ValueError("Expected 64 hexadecimal characters")
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

    def _ensure_callbacks(self, bridge_params: dict) -> object:
        """Register one selected bridge and return its stable process-local key."""
        return self._run_async(self._register_callbacks(bridge_params))

    async def _register_callbacks(self, bridge_params: dict) -> object:
        """Register callback lists only from the daemon event-loop thread."""
        bridge = self._get_bridge(**bridge_params)
        bridge_key = self._callback_registrations.get(bridge)
        if bridge_key is not None:
            callback_bridges = getattr(self, "_callback_bridges", None)
            if callback_bridges is not None:
                callback_bridges[bridge_key] = bridge
            return bridge_key
        get_public_key = getattr(bridge, "get_public_key", None)
        if callable(get_public_key):
            bridge_key = get_public_key().hex().lower()
        else:
            # Older test/double bridges may expose only the process-local
            # companion hash. Real bridges always take the full-identity path.
            bridge_key = str(getattr(bridge, "_companion_hash", id(bridge))).lower()
        companion_hash = str(getattr(bridge, "_companion_hash", ""))

        def _make_cb(event_name):
            """Create a callback that serialises event data for SSE clients."""

            def _cb(*args, **kwargs):
                payload = self._serialise_event(event_name, args, kwargs)
                if companion_hash:
                    payload["companion_hash"] = companion_hash
                self._broadcast_sse(bridge_key, payload)

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
        self._callback_registrations[bridge] = bridge_key
        callback_bridges = getattr(self, "_callback_bridges", None)
        if callback_bridges is None:
            callback_bridges = weakref.WeakValueDictionary()
            self._callback_bridges = callback_bridges
        callback_bridges[bridge_key] = bridge
        return bridge_key

    def _is_active_stream_bridge(self, bridge) -> bool:
        """Return whether the exact subscribed bridge is still published."""
        if bridge is None or self.daemon_instance is None:
            # Standalone/test embeddings may provide callback keys without a
            # daemon registry; retain their established stream behavior.
            return True
        try:
            hash_byte = bridge.get_public_key()[0]
        except (AttributeError, IndexError, TypeError):
            return False
        bridges = getattr(self.daemon_instance, "companion_bridges", {})
        return bridges.get(hash_byte) is bridge

    @staticmethod
    def _serialise_event(event_name: str, args: tuple, kwargs: dict) -> dict:
        """Convert callback arguments to a JSON-safe dict."""
        data: dict = {"event": event_name, "timestamp": int(time.time())}
        for i, arg in enumerate(args):
            data[f"arg{i}"] = _to_json_safe(arg)
        for k, v in kwargs.items():
            data[k] = _to_json_safe(v)
        return data

    def _broadcast_sse(self, bridge_key: object, payload: dict):
        """Put *payload* into clients subscribed to the originating bridge."""
        with self._sse_lock:
            dead = []
            for selected_bridge, client_queue in self._sse_clients:
                if selected_bridge != bridge_key:
                    continue
                try:
                    client_queue.put_nowait(payload)
                except queue.Full:
                    # A callback-only legacy stream has no replay cursor. Close
                    # a slow consumer visibly instead of leaving it connected
                    # to a silently incomplete event feed.
                    try:
                        client_queue.get_nowait()
                    except queue.Empty:
                        pass
                    client_queue.put_nowait(_SSE_OVERFLOW)
                    dead.append((selected_bridge, client_queue))
            for client in dead:
                self._sse_clients.remove(client)

    @staticmethod
    def _sse_principal() -> str:
        """Return a stable, non-secret identity for legacy stream admission."""
        user = getattr(cherrypy.request, "user", None)
        if not isinstance(user, dict):
            user = {}
        auth_type = str(user.get("auth_type") or "unknown")
        if auth_type in ("jwt", "jwt_query"):
            username = str(user.get("username") or "unknown")
            client_id = str(user.get("client_id") or "unknown")
            return f"jwt:{username}:{client_id}"
        if auth_type == "api_token" and user.get("token_id") is not None:
            return f"api_token:{user['token_id']}"
        remote_ip = (
            getattr(getattr(cherrypy.request, "remote", None), "ip", None)
            or "unknown"
        )
        return f"{auth_type}:{remote_ip}"

    def _begin_sse(self, stream_principal: tuple[str, object]) -> bool:
        return self._sse_admission.acquire(*stream_principal)

    def _end_sse(
        self,
        stream_principal: tuple[str, object],
        client: tuple[object, queue.Queue],
    ) -> None:
        with self._sse_lock:
            if client in self._sse_clients:
                self._sse_clients.remove(client)
        self._sse_admission.release(*stream_principal)

    @property
    def _sse_total(self) -> int:
        """Compatibility/readability view used by diagnostics and tests."""

        return self._sse_admission.active_count

    # ==================================================================
    # REST Endpoints
    # ==================================================================

    # ----- Index / listing -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def index(self, **kwargs):
        """GET /api/companion/ — list configured companions."""
        self._require_get()

        async def _snapshot():
            if not self.daemon_instance:
                raise cherrypy.HTTPError(503, "Daemon not initialized")
            bridges = list(
                getattr(self.daemon_instance, "companion_bridges", {}).items()
            )
            identity_manager = getattr(
                self.daemon_instance,
                "identity_manager",
                None,
            )

            name_by_hash: dict[int, str] = {}
            if identity_manager:
                for (
                    reg_name,
                    identity,
                    _cfg,
                ) in identity_manager.get_identities_by_type("companion"):
                    name_by_hash[identity.get_public_key()[0]] = reg_name

            items = []
            for companion_hash, bridge in bridges:
                async with bridge.state_mutation_lock:
                    items.append(
                        {
                            "companion_name": name_by_hash.get(companion_hash, ""),
                            "companion_hash": f"0x{companion_hash:02X}",  # noqa: E231
                            "node_name": bridge.prefs.node_name,
                            "public_key": bridge.get_public_key().hex(),
                            "is_running": bridge.is_running,
                            "contacts_count": bridge.contacts.get_count(),
                            "channels_count": bridge.channels.get_count(),
                            "max_contacts": bridge.contacts.max_contacts,
                            "offline_queue_size": getattr(
                                bridge.message_queue,
                                "_max_size",
                                DEFAULT_OFFLINE_QUEUE_SIZE,
                            ),
                        }
                    )
            return items

        return self._success(self._run_async(_snapshot()))

    # ----- Identity -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def self_info(self, **kwargs):
        """GET /api/companion/self_info — node identity and preferences."""
        self._require_get()
        bridge_params = self._resolve_bridge_params(kwargs)

        def _read(bridge):
            prefs = bridge.get_self_info()
            return {
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

        return self._success(
            self._run_async(self._read_bridge_state(bridge_params, _read))
        )

    # ----- Contacts -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def contacts(self, **kwargs):
        """GET /api/companion/contacts — list all contacts."""
        self._require_get()
        since = self._legacy_integer(kwargs.get("since", 0), "since", low=0)
        bridge_params = self._resolve_bridge_params(kwargs)

        def _read(bridge):
            return [
                {
                    "public_key": (
                        contact.public_key.hex()
                        if isinstance(contact.public_key, bytes)
                        else contact.public_key
                    ),
                    "name": contact.name,
                    "adv_type": contact.adv_type,
                    "flags": contact.flags,
                    "out_path_len": contact.out_path_len,
                    "last_advert_timestamp": contact.last_advert_timestamp,
                    "lastmod": contact.lastmod,
                    "gps_lat": contact.gps_lat,
                    "gps_lon": contact.gps_lon,
                }
                for contact in bridge.get_contacts(since=since)
            ]

        return self._success(
            self._run_async(self._read_bridge_state(bridge_params, _read))
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def contact(self, **kwargs):
        """GET /api/companion/contact?pub_key=<hex> — get single contact."""
        self._require_get()
        pk_hex = kwargs.get("pub_key")
        if not pk_hex:
            raise cherrypy.HTTPError(400, "pub_key required")
        pub_key = self._pub_key_from_hex(pk_hex)
        bridge_params = self._resolve_bridge_params(kwargs)

        def _read(bridge):
            contact = bridge.get_contact_by_key(pub_key)
            if not contact:
                raise cherrypy.HTTPError(404, "Contact not found")
            return {
                "public_key": (
                    contact.public_key.hex()
                    if isinstance(contact.public_key, bytes)
                    else contact.public_key
                ),
                "name": contact.name,
                "adv_type": contact.adv_type,
                "flags": contact.flags,
                "out_path_len": contact.out_path_len,
                "out_path": (
                    contact.out_path.hex()
                    if isinstance(contact.out_path, bytes)
                    else ""
                ),
                "last_advert_timestamp": contact.last_advert_timestamp,
                "lastmod": contact.lastmod,
                "gps_lat": contact.gps_lat,
                "gps_lon": contact.gps_lon,
            }

        return self._success(
            self._run_async(self._read_bridge_state(bridge_params, _read))
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
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"contact_types", "hours", "limit"},
        )
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
            hours = self._legacy_integer(
                hours,
                "hours",
                low=1,
                high=_LEGACY_IMPORT_HOURS_MAX,
            )
        limit = body.get("limit")
        if limit is not None:
            limit = self._legacy_integer(
                limit,
                "limit",
                low=1,
                high=_LEGACY_IMPORT_LIMIT_MAX,
            )
        result = self._run_async(
            self._import_repeater_contacts(
                self._resolve_bridge_params(body),
                contact_types=contact_types,
                hours=hours,
                limit=limit,
            )
        )
        return self._success(result)

    async def _import_repeater_contacts(
        self,
        bridge_params: dict,
        *,
        contact_types: Optional[list[str]],
        hours: Optional[int],
        limit: Optional[int],
    ) -> dict:
        """Apply one bounded, journaled contact import on the daemon loop."""
        bridge = self._get_bridge(**bridge_params)
        sqlite_handler = self._get_sqlite_handler()
        neighbours = await asyncio.to_thread(sqlite_handler.get_neighbors)
        if not isinstance(neighbours, dict):
            raise RuntimeError("Repeater advert query returned an invalid result")

        type_map = {
            "companion": 1,
            "repeater": 2,
            "room_server": 3,
            "sensor": 4,
        }
        cutoff = time.time() - (hours * 3600) if hours is not None else None
        candidates = []
        for raw_public_key, row in neighbours.items():
            if not isinstance(row, dict):
                continue
            contact_type = str(row.get("contact_type") or "").lower()
            contact_type = contact_type.replace(" ", "_").strip()
            if contact_type not in type_map:
                continue
            if contact_types and contact_type not in contact_types:
                continue
            try:
                last_seen = float(row.get("last_seen") or 0)
                latitude = float(row.get("latitude") or 0.0)
                longitude = float(row.get("longitude") or 0.0)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "Skipping repeater advert with malformed timestamp or coordinates"
                )
                continue
            if (
                not math.isfinite(last_seen)
                or last_seen < 0.0
                or last_seen > _SQLITE_SIGNED_INT_MAX
                or not math.isfinite(latitude)
                or not -90.0 <= latitude <= 90.0
                or not math.isfinite(longitude)
                or not -180.0 <= longitude <= 180.0
            ):
                logger.warning(
                    "Skipping repeater advert with invalid timestamp or coordinates"
                )
                continue
            if cutoff is not None and last_seen < cutoff:
                continue
            if (
                not isinstance(raw_public_key, str)
                or len(raw_public_key) != 64
                or any(
                    character not in "0123456789abcdefABCDEF"
                    for character in raw_public_key
                )
            ):
                logger.warning("Skipping repeater advert with invalid public key")
                continue
            public_key = bytes.fromhex(raw_public_key)
            node_name = row.get("node_name")
            if node_name is None:
                node_name = ""
            if not isinstance(node_name, str):
                logger.warning("Skipping repeater advert with invalid node name")
                continue
            try:
                node_name_size = len(node_name.encode("utf-8"))
            except UnicodeEncodeError:
                logger.warning("Skipping repeater advert with invalid node name")
                continue
            if (
                node_name_size > 31
                or any(character in node_name for character in "\x00\r\n")
            ):
                logger.warning("Skipping repeater advert with invalid node name")
                continue
            candidates.append(
                (last_seen, public_key, contact_type, node_name, row)
            )

        candidates.sort(key=lambda item: (-item[0], item[1]))

        committed_changes = []
        async with bridge.state_mutation_lock:
            max_contacts = bridge.contacts.max_contacts
            if limit is not None:
                candidates = candidates[: min(limit, max_contacts)]
            before_contacts = copy.deepcopy(bridge.contacts.get_all())
            before_durable = {
                contact.public_key: bridge._contact_storage_dict(contact)
                for contact in before_contacts
                if contact.adv_type != ADV_TYPE_NONE
            }
            merged = dict(before_durable)

            for last_seen, public_key, contact_type, node_name, row in candidates:
                previous = merged.get(public_key)
                if previous is None:
                    previous = {
                        "flags": 0,
                        "out_path_len": -1,
                        "out_path": b"",
                        "last_advert_packet": None,
                        "sync_since": 0,
                    }
                merged[public_key] = {
                    "pubkey": public_key,
                    "name": node_name,
                    "adv_type": type_map[contact_type],
                    # Import refreshes advert-owned facts only.  Flags, learned
                    # routing, sync progress, and the raw advert belong to the
                    # companion/chat state and survive repeat imports.
                    "flags": previous.get("flags", 0),
                    "out_path_len": previous.get("out_path_len", -1),
                    "out_path": previous.get("out_path", b""),
                    "last_advert_timestamp": int(last_seen),
                    "last_advert_packet": previous.get("last_advert_packet"),
                    "lastmod": int(last_seen),
                    "gps_lat": latitude,
                    "gps_lon": longitude,
                    "sync_since": previous.get("sync_since", 0),
                }

            favourites = [
                contact
                for contact in merged.values()
                if int(contact.get("flags", 0)) & 0x01
            ]
            if len(favourites) > max_contacts:
                raise cherrypy.HTTPError(
                    409,
                    f"Favourite contacts exceed max_contacts={max_contacts}",
                )
            non_favourites = [
                contact
                for contact in merged.values()
                if not (int(contact.get("flags", 0)) & 0x01)
            ]
            non_favourites.sort(
                key=lambda contact: (
                    -int(contact.get("lastmod", 0)),
                    contact["pubkey"],
                )
            )
            selected = favourites + non_favourites[: max_contacts - len(favourites)]
            selected.sort(key=lambda contact: contact["pubkey"])
            selected_keys = {contact["pubkey"] for contact in selected}
            candidate_keys = {
                public_key
                for _seen, public_key, _type, _name, _row in candidates
            }
            imported_keys = candidate_keys & selected_keys
            added = len(imported_keys - before_durable.keys())
            # Preserve the legacy import/trim counters: ``imported`` is the
            # number considered and ``removed`` is the number rejected by the
            # capacity selection, even though this atomic path never exposes
            # those transient rows.
            removed = len(merged) - len(selected)

            advert_fields = (
                "name",
                "adv_type",
                "last_advert_timestamp",
                "lastmod",
                "gps_lat",
                "gps_lon",
            )
            updated = 0
            retained = 0
            for public_key in imported_keys & before_durable.keys():
                before = before_durable[public_key]
                after = merged[public_key]
                if any(before.get(field) != after.get(field) for field in advert_fields):
                    updated += 1
                else:
                    retained += 1

            after_contacts = [
                Contact(
                    public_key=contact["pubkey"],
                    name=contact["name"],
                    adv_type=contact["adv_type"],
                    flags=contact["flags"],
                    out_path_len=contact["out_path_len"],
                    out_path=contact["out_path"],
                    last_advert_timestamp=contact["last_advert_timestamp"],
                    lastmod=contact["lastmod"],
                    gps_lat=contact["gps_lat"],
                    gps_lon=contact["gps_lon"],
                    sync_since=contact["sync_since"],
                    last_advert_packet=contact["last_advert_packet"],
                )
                for contact in selected
            ]
            transients = [
                contact
                for contact in before_contacts
                if contact.adv_type == ADV_TYPE_NONE
            ]
            self._replace_contacts(bridge, after_contacts + transients)
            committed_changes = bridge._contact_changes(
                before_contacts,
                bridge.contacts.get_all(),
            )
            try:
                await bridge._persist_contact_changes(committed_changes)
            except Exception:
                self._replace_contacts(bridge, before_contacts)
                raise

        await self._notify_contact_changes(bridge, committed_changes)
        return {
            "imported": len(candidates),
            "added": added,
            "updated": updated,
            "retained": retained,
            "removed": removed,
        }

    # ----- Channels -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def channels(self, **kwargs):
        """GET /api/companion/channels — list configured channels."""
        self._require_get()
        try:
            bridge_params = self._resolve_bridge_params(kwargs)

            def _read(bridge):
                items = []
                for index in range(bridge.channels.max_channels):
                    channel = bridge.channels.get(index)
                    if channel:
                        items.append(
                            {
                                "index": index,
                                "name": channel.name,
                                # Never expose the PSK secret over REST.
                            }
                        )
                return items

            return self._success(
                self._run_async(self._read_bridge_state(bridge_params, _read))
            )
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.error("channels endpoint error: %s", exc, exc_info=True)
            return self._error(str(exc))

    # ----- Statistics -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def stats(self, **kwargs):
        """GET /api/companion/stats?type=packets — local companion stats."""
        self._require_get()
        stats_type_map = {"core": 0, "radio": 1, "packets": 2}
        stats_type = kwargs.get("type", "packets")
        if stats_type not in stats_type_map:
            raise cherrypy.HTTPError(
                400,
                "type must be one of: core, radio, packets",
            )
        stype = stats_type_map[stats_type]
        bridge_params = self._resolve_bridge_params(kwargs)
        return self._success(
            self._run_async(
                self._read_bridge_state(
                    bridge_params,
                    lambda bridge: bridge.get_stats(stype),
                )
            )
        )

    # ----- Messaging -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def send_text(self, **kwargs):
        """POST /api/companion/send_text  {pub_key, text, txt_type?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"pub_key", "text", "txt_type"},
        )
        bridge_params = self._resolve_bridge_params(body)
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        text = text_field(
            body,
            "text",
            required=True,
            max_bytes=MAX_TEXT_LEN,
        )
        if "\x00" in text:
            raise cherrypy.HTTPError(400, "text must not contain NUL")
        txt_type = self._legacy_integer(
            body.get("txt_type", 0),
            "txt_type",
            low=0,
            high=1,
        )
        result = self._run_async(
            self._call_bridge_as_operator(
                bridge_params,
                lambda bridge: bridge.send_text_message(
                    pub_key,
                    text,
                    txt_type=txt_type,
                    # Preserve the upstream operator contract.  A False result
                    # after successful RF injection is an ACK timeout, not
                    # proof that nothing went on air; the bridge records that
                    # send without changing this human-facing response shape.
                    wait_for_ack=True,
                ),
            )
        )
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
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"channel_idx", "text"},
        )
        bridge_params = self._resolve_bridge_params(body)
        if "channel_idx" not in body:
            raise cherrypy.HTTPError(400, "channel_idx required")
        channel_idx = self._legacy_integer(
            body["channel_idx"],
            "channel_idx",
            low=0,
            high=0xFF,
        )
        text = text_field(
            body,
            "text",
            required=True,
            max_bytes=MAX_TEXT_LEN,
        )
        if "\x00" in text:
            raise cherrypy.HTTPError(400, "text must not contain NUL")

        async def send(bridge):
            max_channels = getattr(
                getattr(bridge, "channels", None),
                "max_channels",
                40,
            )
            if channel_idx >= max_channels:
                raise cherrypy.HTTPError(400, "channel_idx out of range")
            sender_prefix = f"{bridge.prefs.node_name}: ".encode("utf-8")
            text_budget = max(0, MAX_TEXT_LEN - len(sender_prefix))
            if len(text.encode("utf-8")) > text_budget:
                raise cherrypy.HTTPError(
                    400,
                    f"text exceeds {text_budget} UTF-8 bytes for this channel sender name",
                )
            return await bridge.send_channel_message(channel_idx, text)

        try:
            success = self._run_async(
                self._call_bridge_as_operator(
                    bridge_params,
                    send,
                )
            )
        except ChannelTextCapacityError as exc:
            raise cherrypy.HTTPError(
                400,
                f"text exceeds {exc.max_bytes} UTF-8 bytes for this channel sender name",
            ) from exc
        return self._success({"sent": success})

    # ----- Login -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def login(self, **kwargs):
        """POST /api/companion/login  {pub_key, password?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"pub_key", "password"},
        )
        bridge_params = self._resolve_bridge_params(body)
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        password = text_field(
            body,
            "password",
            default="",
            max_bytes=15,
        )
        if "\x00" in password:
            raise cherrypy.HTTPError(400, "password must not contain NUL")
        result = self._run_async(
            self._call_bridge(
                bridge_params,
                lambda bridge: bridge.send_login(pub_key, password),
            ),
            timeout=15.0,
        )
        return self._success(_to_json_safe(result))

    # ----- Status / Telemetry Requests -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def request_status(self, **kwargs):
        """POST /api/companion/request_status  {pub_key, timeout?, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"pub_key", "timeout"},
        )
        bridge_params = self._resolve_bridge_params(body)
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        timeout = self._legacy_timeout(body.get("timeout"), 15.0)
        result = self._run_async(
            self._call_bridge(
                bridge_params,
                lambda bridge: bridge.send_status_request(
                    pub_key,
                    timeout=timeout,
                ),
            ),
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
            reject_unknown_fields(
                body,
                _COMPANION_SELECTOR_FIELDS
                | {
                    "pub_key",
                    "want_base",
                    "want_location",
                    "want_environment",
                    "timeout",
                },
            )
            bridge_params = self._resolve_bridge_params(body)
            pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
            timeout = self._legacy_timeout(body.get("timeout"), 20.0)
            result = self._run_async(
                self._call_bridge(
                    bridge_params,
                    lambda bridge: bridge.send_telemetry_request(
                        pub_key,
                        want_base=boolean_field(body, "want_base", default=True),
                        want_location=boolean_field(
                            body,
                            "want_location",
                            default=True,
                        ),
                        want_environment=boolean_field(
                            body,
                            "want_environment",
                            default=True,
                        ),
                        timeout=timeout,
                    ),
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
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"pub_key", "command", "parameters"},
        )
        bridge_params = self._resolve_bridge_params(body)
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        command = text_field(
            body,
            "command",
            required=True,
            max_bytes=MAX_TEXT_LEN,
        )
        parameters = text_field(
            body,
            "parameters",
            max_bytes=MAX_TEXT_LEN,
        )
        if "\x00" in command:
            raise cherrypy.HTTPError(400, "command must not contain NUL")
        if parameters is not None and "\x00" in parameters:
            raise cherrypy.HTTPError(400, "parameters must not contain NUL")
        full_command = command if not parameters else f"{command} {parameters}"
        if len(full_command.encode("utf-8")) > MAX_TEXT_LEN:
            raise cherrypy.HTTPError(
                400,
                f"command and parameters exceed {MAX_TEXT_LEN} UTF-8 bytes",
            )
        result = self._run_async(
            self._call_bridge(
                bridge_params,
                lambda bridge: bridge.send_repeater_command(
                    pub_key,
                    command,
                    parameters,
                ),
            ),
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
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"pub_key"},
        )
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        ok = self._run_async(
            self._reset_path_durable(
                self._resolve_bridge_params(body),
                pub_key,
            )
        )
        return self._success({"reset": ok})

    async def _reset_path_durable(
        self,
        bridge_params: dict,
        public_key: bytes,
    ) -> bool:
        """Reset one path and commit its journal event before exposing it."""
        bridge = self._get_bridge(**bridge_params)
        committed_changes = []
        async with bridge.state_mutation_lock:
            before_contacts = copy.deepcopy(bridge.contacts.get_all())
            if not bridge.reset_path(public_key):
                return False
            committed_changes = bridge._contact_changes(
                before_contacts,
                bridge.contacts.get_all(),
            )
            try:
                await bridge._persist_contact_changes(committed_changes)
            except Exception:
                self._replace_contacts(bridge, before_contacts)
                raise

        await self._notify_contact_changes(bridge, committed_changes)
        return True

    # ----- Device Configuration -----

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def set_advert_name(self, **kwargs):
        """POST /api/companion/set_advert_name  {advert_name, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"advert_name", "name"},
        )
        name = body.get("advert_name", body.get("name", ""))
        if not name:
            raise cherrypy.HTTPError(400, "name required")
        try:
            validated_name = validate_companion_node_name(name)
        except ValueError as e:
            raise cherrypy.HTTPError(400, str(e)) from e
        persisted_name = self._run_async(
            self._set_advert_name(
                self._resolve_bridge_params(body),
                validated_name,
            )
        )
        return self._success({"name": persisted_name})

    async def _set_advert_name(
        self,
        bridge_params: dict,
        name: str,
    ) -> str:
        bridge = self._get_bridge(**bridge_params)
        async with bridge.state_mutation_lock:
            clear_error = getattr(bridge, "clear_prefs_save_error", None)
            if clear_error is not None:
                clear_error()
            bridge.set_advert_name(name)
            consume_error = getattr(bridge, "consume_prefs_save_error", None)
            error = consume_error() if consume_error is not None else None
            if error is not None:
                raise RuntimeError("Failed to persist companion preferences") from error
            persisted_name = bridge.prefs.node_name
        self._sync_node_name_config(
            bridge,
            persisted_name,
            bridge_params.get("name"),
        )
        return persisted_name

    def _sync_node_name_config(
        self,
        bridge,
        node_name: str,
        companion_name: Optional[str],
    ) -> None:
        """Keep the human-readable config aligned after prefs are durable."""
        config_manager = getattr(self, "config_manager", None)
        if not config_manager:
            return
        if companion_name is None:
            identity_manager = getattr(
                self.daemon_instance,
                "identity_manager",
                None,
            )
            if identity_manager:
                public_key = bridge.get_public_key()
                for name, identity, _config in identity_manager.get_identities_by_type(
                    "companion"
                ):
                    if identity.get_public_key() == public_key:
                        companion_name = name
                        break
        if companion_name is None:
            return

        save_node_name = getattr(
            config_manager,
            "save_companion_node_name",
            None,
        )
        if callable(save_node_name):
            try:
                if not save_node_name(companion_name, node_name):
                    logger.warning(
                        "Failed to save config after set_advert_name for %s",
                        companion_name,
                    )
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "Invalid config after set_advert_name for %s: %s",
                    companion_name,
                    exc,
                )
            return

        # Compatibility for small embedded callers that provide only the old
        # save_to_file surface.
        config = getattr(self, "config", {})
        companions = (config.get("identities") or {}).get("companions") or []
        for entry in companions:
            if entry.get("name") != companion_name:
                continue
            settings = entry.setdefault("settings", {})
            if settings.get("node_name") == node_name:
                return
            settings["node_name"] = node_name
            try:
                if not config_manager.save_to_file():
                    logger.warning(
                        "Failed to save config after set_advert_name for %s",
                        companion_name,
                    )
            except Exception as exc:
                logger.warning(
                    "Error saving config after set_advert_name for %s: %s",
                    companion_name,
                    exc,
                )
            return

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def set_advert_location(self, **kwargs):
        """POST /api/companion/set_advert_location  {latitude, longitude, companion_name?}"""
        self._require_post()
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            _COMPANION_SELECTOR_FIELDS | {"latitude", "longitude"},
        )
        if "latitude" not in body or "longitude" not in body:
            raise cherrypy.HTTPError(400, "latitude and longitude required")
        latitude = self._legacy_float(
            body["latitude"],
            "latitude",
            low=-90.0,
            high=90.0,
        )
        longitude = self._legacy_float(
            body["longitude"],
            "longitude",
            low=-180.0,
            high=180.0,
        )
        try:
            location = self._run_async(
                self._set_advert_location(
                    self._resolve_bridge_params(body),
                    latitude,
                    longitude,
                )
            )
        except ValueError as exc:
            raise cherrypy.HTTPError(400, str(exc)) from exc
        return self._success(location)

    async def _set_advert_location(
        self,
        bridge_params: dict,
        latitude: float,
        longitude: float,
    ) -> dict:
        bridge = self._get_bridge(**bridge_params)
        async with bridge.state_mutation_lock:
            clear_error = getattr(bridge, "clear_prefs_save_error", None)
            if clear_error is not None:
                clear_error()
            bridge.set_advert_latlon(latitude, longitude)
            consume_error = getattr(bridge, "consume_prefs_save_error", None)
            error = consume_error() if consume_error is not None else None
            if error is not None:
                raise RuntimeError("Failed to persist companion preferences") from error
            return {
                "latitude": bridge.prefs.latitude,
                "longitude": bridge.prefs.longitude,
            }

    # ==================================================================
    # SSE Event Stream
    # ==================================================================

    @cherrypy.expose
    def events(self, **kwargs):
        """GET /api/companion/events — Server-Sent Events stream for push callbacks.

        This legacy operator route uses admin authentication and can select a
        bridge with ``companion_name`` or ``companion_hash``. Because browser
        EventSource cannot set an Authorization header, exact GET requests to
        this route may use a short-lived operator JWT in ``token``. Device/API
        tokens are not accepted in the query string. New clients should prefer
        the scoped v1 companion event route.
        """
        if cherrypy.request.method != "GET":
            cherrypy.response.headers["Allow"] = "GET"
            raise cherrypy.HTTPError(405, "Method not allowed. Use GET.")

        stream_user = getattr(cherrypy.request, "user", None)
        if (
            not isinstance(stream_user, dict)
            or not is_admin_scope(stream_user.get("scope"))
        ):
            raise cherrypy.HTTPError(403, "Admin scope required")
        try:
            authorization = AuthorizationLease.from_request(
                cherrypy.request,
                cherrypy.config.get("token_manager"),
            )
        except ValueError as exc:
            raise cherrypy.HTTPError(
                401,
                "Authenticated stream credential is incomplete",
            ) from exc
        try:
            if not authorization.is_active(force=True):
                raise cherrypy.HTTPError(401, "Stream credential is no longer valid")
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.error("Legacy SSE authorization recheck is unavailable")
            raise cherrypy.HTTPError(503, "Authentication unavailable") from exc

        bridge_key = self._ensure_callbacks(self._resolve_bridge_params(kwargs))
        selected_bridge = getattr(self, "_callback_bridges", {}).get(bridge_key)

        client_queue: queue.Queue = queue.Queue(maxsize=self._sse_queue_maxsize)
        client = (bridge_key, client_queue)
        stream_principal = (self._sse_principal(), bridge_key)
        if not self._begin_sse(stream_principal):
            cherrypy.response.headers["Retry-After"] = "5"
            raise cherrypy.HTTPError(
                429,
                "Only one legacy event stream per companion is allowed",
            )
        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = (
            "no-store, no-cache, no-transform"
        )
        cherrypy.response.headers["Connection"] = "keep-alive"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"
        with self._sse_lock:
            self._sse_clients.append(client)

        def _authorization_is_active() -> bool:
            try:
                return authorization.is_active()
            except Exception:
                logger.error("Legacy SSE authorization recheck failed")
                return False

        def generate():
            try:
                if not _authorization_is_active():
                    return
                payload = {"event": "connected", "timestamp": int(time.time())}
                yield f"data: {json.dumps(payload, allow_nan=False)}\n\n"

                while True:
                    if (
                        not self._is_active_stream_bridge(selected_bridge)
                        or not _authorization_is_active()
                    ):
                        return
                    keepalive_deadline = (
                        time.monotonic() + float(self._sse_keepalive_sec)
                    )
                    item = None
                    while True:
                        if (
                            not self._is_active_stream_bridge(selected_bridge)
                            or not _authorization_is_active()
                        ):
                            return
                        keepalive_remaining = (
                            keepalive_deadline - time.monotonic()
                        )
                        if keepalive_remaining <= 0:
                            break
                        wait_for = authorization.check_in(keepalive_remaining)
                        try:
                            item = client_queue.get(timeout=wait_for)
                        except queue.Empty:
                            continue
                        break
                    if item is _SSE_OVERFLOW:
                        logger.warning(
                            "Closing slow legacy companion event stream after "
                            "its bounded queue overflowed"
                        )
                        return
                    if item is not None:
                        if not _authorization_is_active():
                            return
                        yield f"data: {json.dumps(item, allow_nan=False)}\n\n"
                        continue
                    # Keep-alive comment frame keeps EventSource connected
                    # without allocating additional JSON payload objects.
                    if not _authorization_is_active():
                        return
                    yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            except Exception as exc:
                logger.debug(f"SSE stream ended: {exc}")

        return _LegacyClosingIterator(
            generate(),
            lambda: self._end_sse(stream_principal, client),
        )

    events._cp_config = {"response.stream": True}


# ======================================================================
# Utility: make arbitrary objects JSON-serialisable for SSE events
# ======================================================================


def _to_json_safe(obj):
    """Convert common companion objects to JSON-safe dicts/values."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            # Radios commonly use NaN/Infinity for an unavailable sensor.
            # JSON clients agree on null; they do not agree on Python's
            # non-standard NaN/Infinity tokens.
            return None
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
