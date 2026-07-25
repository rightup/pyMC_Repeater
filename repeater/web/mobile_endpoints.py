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
- ``POST /api/v1/companions/{name}/advert`` — send the selected companion's advert
- ``POST /api/v1/companions/{name}/anonymous_request`` — public v13 node query
- ``GET /api/v1/companions/{name}/events`` — resumable SSE live stream (§8)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/login`` — room login (§7.3)
- ``GET /api/v1/companions/{name}/contacts/{pubkey}/connection`` — login state
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/logout`` — remote logout
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/status_request`` (§7.3)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/telemetry_request`` (§7.3)
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/ping`` — direct TRACE
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/path_discovery`` — route query
- ``POST /api/v1/companions/{name}/contacts/{pubkey}/reset_path`` (§7.3)

Cursor semantics (§5.3): clients hold an opaque ``epoch:sequence`` cursor;
the server keeps no per-client read state. ``journal_epoch`` detects
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
import copy
import hashlib
import json
import logging
import math
import queue
import secrets
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Awaitable, Callable, Optional, Tuple

import cherrypy
from openhop_core.companion.constants import (
    ADV_TYPE_CHAT,
    ADV_TYPE_NONE,
    ADV_TYPE_REPEATER,
    ANON_REQ_TYPE_BASIC,
    ANON_REQ_TYPE_OWNER,
    ANON_REQ_TYPE_REGIONS,
    CHANNEL_NAME_SIZE,
    CONTACT_NAME_SIZE,
)
from openhop_core.protocol.constants import MAX_TEXT_LEN

from repeater.companion.bridge import (
    ChannelTextCapacityError,
    PUBLIC_PREF_FIELDS,
    channel_text_capacity,
    outbound_message_id,
    outbound_message_source,
)
from repeater.companion.correlation import outbound_send_capture
from repeater.companion.path_resolution import resolve_path
from repeater.companion.rf_window import observations_pruned, parse_window_seconds
from repeater.companion.utils import (
    CONTACT_FLAG_FAVOURITE,
    companion_device_principal_id,
    parse_companion_send_response,
    validate_companion_registration_name,
)
from repeater.data_acquisition.sqlite_handler import CompanionStorageError

from .api_validation import (
    SQLITE_ROW_ID_MAX,
    finite_float_field,
    integer_field,
    positive_sqlite_row_id,
    read_json_object,
    reject_control_characters,
    reject_unknown_fields,
    text_field,
)
from .auth.lease import AuthorizationLease
from .auth.middleware import require_auth
from .auth.policy import is_admin_scope, is_companion_scope
from .companion_endpoints import _to_json_safe
from .rate_limit import (
    PrincipalTokenBucket,
    SSEAdmission,
    consume_all,
    sse_stream_settings,
    validate_sse_connection_capacity,
)

logger = logging.getLogger("MobileAPI")

_MAX_RF_BURST = 10_000
_MAX_RF_PER_MINUTE = 60_000.0
_MAX_SSE_CONNECTIONS = 256
_UINT32_MAX = (1 << 32) - 1
_EVENT_TYPE_MAX_BYTES = 64

# Public journal payload vocabularies.  Known event types are projected
# explicitly so a future internal/storage-only field cannot silently become
# part of the mobile wire contract. Unknown safe event types keep their
# envelope for forward compatibility, but their unreviewed payload is
# deliberately replaced with an empty object.
_MESSAGE_EVENT_FIELDS = (
    "id",
    "companion_hash",
    "sender_key",
    "recipient_key",
    "sender_prefix",
    "txt_type",
    "timestamp",
    "text",
    "is_channel",
    "channel_idx",
    "path_len",
    "snr",
    "rssi",
    "channel_data_type",
    "channel_data_payload",
    "packet_hash",
    "created_at",
    "observation_count",
    "unique_path_count",
    "direction",
    "state",
    "expected_ack",
    "source",
)
_CONTACT_EVENT_FIELDS = (
    "name",
    "adv_type",
    "flags",
    "out_path_len",
    "last_advert_timestamp",
    "lastmod",
    "gps_lat",
    "gps_lon",
    "change",
)
_CHANNEL_EVENT_FIELDS = ("index", "name", "change")
_MESSAGE_RECEPTION_EVENT_FIELDS = (
    "message_id",
    "packet_hash",
    "path",
    "rssi",
    "snr",
    "observed_at",
    "observation_count",
    "unique_path_count",
)
_MESSAGE_SEND_STATE_EVENT_FIELDS = (
    "message_id",
    "state",
    "packet_hash",
    "expected_ack",
    "path",
    "terminal_repeater_hash",
    "rssi",
    "snr",
    "observed_at",
    "heard_repeat_count",
    "unique_repeater_count",
)
_RF_RECEPTION_EVENT_FIELDS = (
    "packet_hash",
    "rssi",
    "snr",
    "path",
    "observed_at",
)
_PACKET_HASH_EVENT_TYPES = frozenset(
    {
        "message",
        "message_reception",
        "message_send_state",
        "rf_reception",
    }
)
_PUBLIC_PREF_STRING_FIELDS = frozenset({"node_name", "default_scope_name"})
_PUBLIC_PREF_INTEGER_FIELDS = frozenset(PUBLIC_PREF_FIELDS).difference(
    _PUBLIC_PREF_STRING_FIELDS,
    {"latitude", "longitude", "rx_delay_base", "airtime_factor"},
)
_PUBLIC_PREF_NUMBER_FIELDS = frozenset({"latitude", "longitude", "rx_delay_base", "airtime_factor"})
_MESSAGE_STATES = frozenset(
    {
        "received",
        "pending",
        "transmitted",
        "heard_repeated",
        "confirmed",
        "failed",
        "indeterminate",
    }
)

_REPEATER_VERSION: Optional[str]
try:
    from repeater._version import __version__ as _generated_version
except Exception:  # pragma: no cover - version metadata is best-effort
    _REPEATER_VERSION = None
else:
    _REPEATER_VERSION = _generated_version


def _normalize_hash16(value) -> Optional[str]:
    """Return the API's canonical 16-character uppercase packet hash."""

    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.hex()
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) < 16 or not _is_hex(text):
        logger.warning("Omitting malformed legacy packet hash from mobile response")
        return None
    return text[:16].upper()


def _is_hex(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _utf8_size(value: str, field: str) -> int:
    """Return encoded size or reject a JSON/URL string with lone surrogates."""

    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise cherrypy.HTTPError(400, f"{field} must be valid UTF-8") from None


def _device_id_text(value: object) -> str:
    """Validate one device identifier from either JSON or a URL path."""

    if not isinstance(value, str) or not value.strip():
        raise cherrypy.HTTPError(400, "device_id must be a nonblank string")
    if value != value.strip():
        raise cherrypy.HTTPError(
            400,
            "device_id must not have leading or trailing whitespace",
        )
    reject_control_characters(value, "device_id")
    if "/" in value or "\\" in value:
        raise cherrypy.HTTPError(400, "device_id must be one URL path segment")
    if value in (".", ".."):
        raise cherrypy.HTTPError(400, "device_id must not be a URL dot segment")
    if _utf8_size(value, "device_id") > _DEVICE_ID_MAX_BYTES:
        raise cherrypy.HTTPError(
            400,
            f"device_id exceeds {_DEVICE_ID_MAX_BYTES} UTF-8 bytes",
        )
    return value


def _format_cursor(epoch: str, seq: int) -> str:
    """Return the opaque, reset-safe v1 cursor."""

    return f"{epoch}:{int(seq)}"


def _parse_cursor(value: object) -> Tuple[Optional[str], int]:
    """Parse ``epoch:seq``; bare integers are recognized as legacy cursors."""

    text = str(value or "")
    if not text or text != text.strip() or _utf8_size(text, "cursor") > 128:
        raise cherrypy.HTTPError(400, "Invalid cursor")
    raw_epoch, separator, raw_seq = text.rpartition(":")
    if separator:
        if not raw_epoch or any(character not in "0123456789abcdef" for character in raw_epoch):
            raise cherrypy.HTTPError(400, "Invalid cursor")
        epoch: Optional[str] = raw_epoch
    else:
        epoch, raw_seq = None, text
    if not raw_seq or any(character not in "0123456789" for character in raw_seq):
        raise cherrypy.HTTPError(400, "Invalid cursor") from None
    seq = int(raw_seq)
    if seq > SQLITE_ROW_ID_MAX:
        raise cherrypy.HTTPError(400, "Invalid cursor")
    return epoch or None, seq


# Opt-in uncorrelated RF-reception firehose event type (design doc §9
# "Correlated vs. uncorrelated receptions"). Excluded from sync/SSE output
# unless the request's ``include`` param names it — see
# ``_include_rf_receptions``.
_RF_RECEPTION_EVENT_TYPE = "rf_reception"
# Public ``include=`` selector, not an authentication credential.
_INCLUDE_RF_RECEPTIONS_TOKEN = "rf_receptions"  # nosec B105

_IDEMPOTENCY_KEY_MAX_BYTES = 128
_DEVICE_ID_MAX_BYTES = 128
_DEVICE_NAME_MAX_BYTES = 96
_PLATFORM_MAX_BYTES = 32
_PUSH_TOKEN_MAX_BYTES = 4096
_MENTION_KEYWORD_MAX_BYTES = 64
_MAX_MENTION_KEYWORDS = 32
_RF_OBSERVATION_LIMIT = 500
_IDEMPOTENCY_LOCK_STRIPES = 64
_UNPERSISTED_INDETERMINATE_MAX = 1024


def _success_response(data, **kwargs) -> dict:
    """Build one RFC-JSON-safe v1 success envelope."""

    cherrypy.response.headers.setdefault("Cache-Control", "no-store")
    try:
        result = {"success": True, "data": _to_json_safe(data)}
        result.update(_to_json_safe(kwargs))
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        logger.error("Companion response contains invalid JSON state: %s", exc)
        raise cherrypy.HTTPError(
            503,
            "Companion state is temporarily unavailable",
        ) from exc


def _positive_config_integer(
    config: dict,
    name: str,
    default: int,
    *,
    section: str,
    maximum: Optional[int] = None,
) -> int:
    """Read one explicit positive integer from operator configuration."""

    value = config.get(name, default)
    if type(value) is not int or value < 1:
        raise ValueError(f"{section}.{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{section}.{name} must be no greater than {maximum}")
    return value


def _positive_config_number(
    config: dict,
    name: str,
    default: float,
    *,
    section: str,
    maximum: Optional[float] = None,
) -> float:
    """Read one explicit finite positive number from operator configuration."""

    value = config.get(name, default)
    if type(value) not in (int, float):
        raise ValueError(f"{section}.{name} must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{section}.{name} must be a finite positive number") from None
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ValueError(f"{section}.{name} must be a finite positive number")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{section}.{name} must be no greater than {maximum:g}")
    return parsed


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DispatchUnavailable(cherrypy.HTTPError):
    """The bridge coroutine was not handed to the daemon event loop."""

    def __init__(self):
        super().__init__(503, "Event loop not available")


class _ClosingIterator:
    """Run one cleanup callback even when a stream closes before first pull."""

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


def _include_rf_receptions(include) -> bool:
    """Parse the strict, comma-separated ``?include=`` selector."""
    if include is None:
        return False
    if not isinstance(include, str):
        raise cherrypy.HTTPError(400, "include must be a string")
    include_text = include
    if _utf8_size(include_text, "include") > 128:
        raise cherrypy.HTTPError(400, "include exceeds 128 UTF-8 bytes")
    tokens_list = [token.strip() for token in include_text.split(",")]
    if not tokens_list or any(not token for token in tokens_list):
        raise cherrypy.HTTPError(400, "include contains an empty token")
    unknown = sorted(set(tokens_list) - {_INCLUDE_RF_RECEPTIONS_TOKEN})
    if unknown:
        raise cherrypy.HTTPError(
            400,
            f"Unknown include token(s): {', '.join(unknown)}",
        )
    tokens = set(tokens_list)
    return _INCLUDE_RF_RECEPTIONS_TOKEN in tokens


async def _send_and_capture(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    capture: dict,
    message_id: Optional[int] = None,
):
    """Create and await a bridge coroutine with fresh send context in scope.

    ``RepeaterCompanionBridge._send_packet`` (awaited somewhere inside
    the returned coroutine, transitively) publishes the packet hash and
    accepted/failed/indeterminate RF outcome into whatever holder is set in
    the *current* context (design doc §10.4). The request owns that holder so
    published correlation remains available if later post-transmit work raises.
    Setting the ContextVar here — inside the coroutine actually scheduled
    onto the event loop — rather than in the request thread, is what scopes
    the holder to this one send: contextvars
    propagate down an await chain within the same task, so two concurrent
    sends (two separate calls to this function, two separate tasks) never
    see each other's holder. The factory also avoids creating a bridge
    coroutine when the daemon event loop cannot accept work.
    """
    capture_token = outbound_send_capture.set(capture)
    source_token = outbound_message_source.set("rest")
    message_token = outbound_message_id.set(message_id)
    try:
        result = await coro_factory()
    finally:
        outbound_message_id.reset(message_token)
        outbound_message_source.reset(source_token)
        outbound_send_capture.reset(capture_token)
    return result


class MobileAPIEndpoints:
    """Root of the ``/api/v1/`` tree (attach as ``APIEndpoints.v1``)."""

    def __init__(
        self,
        daemon_instance=None,
        config=None,
        event_loop=None,
        sse_admission=None,
    ):
        self.daemon_instance = daemon_instance
        self.config = config if config is not None else {}
        self.event_loop = event_loop
        self.companions = CompanionsV1(
            daemon_instance,
            self.config,
            event_loop=event_loop,
            sse_admission=sse_admission,
        )
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
        scheme = str(getattr(cherrypy.request, "scheme", "http") or "http").lower()
        return _success_response(
            {
                "site_name": str(site_name),
                "api_versions": ["v1"],
                "auth_modes": ["jwt", "api_token"],
                "transport": {
                    "scheme": scheme,
                    "secure": scheme == "https",
                    "trusted_network_required": scheme != "https",
                },
                "server": {"version": _REPEATER_VERSION, "time": time.time()},
            }
        )


class CompanionsV1:
    """``/api/v1/companions`` collection and per-companion sync/action resources."""

    _ACTIONS = (
        "snapshot",
        "sync",
        "messages",
        "advert",
        "anonymous_request",
        "events",
    )
    # Sub-resource actions under /companions/{name}/contacts/{pubkey}/{action}
    # (§7.3). POST actions have no idempotency requirement.
    # Login/status/telemetry transmit RF and synchronously return their
    # request result; logout is one best-effort RF send. A timeout is an
    # unknown outcome and callers choose whether to issue a fresh request.
    _CONTACT_ACTIONS = (
        "login",
        "logout",
        "status_request",
        "telemetry_request",
        "ping",
        "path_discovery",
        "reset_path",
    )
    # GET-only sub-resource actions on the same /contacts/{pubkey}/{action}
    # URL shape (§10): route identically to _CONTACT_ACTIONS, just a
    # different HTTP method and handler set.
    _CONTACT_GET_ACTIONS = ("paths", "connection")
    # Sub-resource actions under /companions/{name}/messages/{id}/{action}
    # and /companions/{name}/transmissions/{packet_hash}/{action} (§10): the
    # RF observation surface's other two URL shapes.
    _MESSAGE_SUB_ACTIONS = ("receptions",)
    _TRANSMISSION_SUB_ACTIONS = ("repeats",)

    def __init__(
        self,
        daemon_instance=None,
        config=None,
        event_loop=None,
        sse_admission=None,
    ):
        self.daemon_instance = daemon_instance
        self.config = config if config is not None else {}
        self.event_loop = event_loop
        api_cfg = self.config.get("mobile_api", {}) if isinstance(self.config, dict) else {}
        if not isinstance(api_cfg, dict):
            raise ValueError("mobile_api must be an object")
        burst = _positive_config_integer(
            api_cfg,
            "rf_burst",
            6,
            section="mobile_api",
            maximum=_MAX_RF_BURST,
        )
        per_minute = _positive_config_number(
            api_cfg,
            "rf_per_minute",
            12,
            section="mobile_api",
            maximum=_MAX_RF_PER_MINUTE,
        )
        self._rf_limiter = PrincipalTokenBucket(
            capacity=burst,
            refill_per_second=per_minute / 60.0,
        )
        global_burst = _positive_config_integer(
            api_cfg,
            "rf_global_burst",
            12,
            section="mobile_api",
            maximum=_MAX_RF_BURST,
        )
        global_per_minute = _positive_config_number(
            api_cfg,
            "rf_global_per_minute",
            30,
            section="mobile_api",
            maximum=_MAX_RF_PER_MINUTE,
        )
        self._rf_global_limiter = PrincipalTokenBucket(
            capacity=global_burst,
            refill_per_second=global_per_minute / 60.0,
        )
        self._idempotency_locks = tuple(threading.Lock() for _ in range(_IDEMPOTENCY_LOCK_STRIPES))
        # If RF may have happened while storage is unavailable, retain one
        # bounded process-local fail-closed marker.  Same-key retries can then
        # report the truth and repair SQLite when it returns, while a process
        # restart delegates the same recovery to SQLiteHandler startup.
        self._unpersisted_indeterminate_lock = threading.Lock()
        self._unpersisted_indeterminate: dict[tuple[str, str, str], dict] = {}
        self._unpersisted_indeterminate_safety_lost = False
        sse_max_connections = _positive_config_integer(
            api_cfg,
            "sse_max_connections",
            8,
            section="mobile_api",
            maximum=_MAX_SSE_CONNECTIONS,
        )
        validate_sse_connection_capacity(self.config, sse_max_connections)
        self._sse_admission = sse_admission or SSEAdmission(
            sse_max_connections,
        )
        if self._sse_admission.max_connections != sse_max_connections:
            raise ValueError(
                "shared SSE admission limit does not match mobile_api.sse_max_connections"
            )
        self._sse_queue_maxsize, self._sse_keepalive_sec = sse_stream_settings(self.config)

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
        elif len(vpath) == 3:
            # /companions/{name}/contacts/{pubkey} and
            # /companions/{name}/channels/{index} — collection *members*,
            # not the {pubkey}/{action} sub-resources handled below.
            collection = vpath[1]
            if collection == "contacts":
                name = vpath.pop(0)
                vpath.pop(0)
                pubkey = vpath.pop(0)
                cherrypy.request.params["companion_name"] = name
                cherrypy.request.params["contact_pubkey"] = pubkey
                return self.contact
            if collection == "channels":
                name = vpath.pop(0)
                vpath.pop(0)
                index = vpath.pop(0)
                cherrypy.request.params["companion_name"] = name
                cherrypy.request.params["channel_index"] = index
                return self.channel
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
        return _success_response(data, **kwargs)

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
        """Read one bounded application/json object."""

        return read_json_object(
            require_json_content_type=True,
            allow_empty_without_content_type=True,
        )

    def _pub_key_from_hex(self, hex_str: str) -> bytes:
        """Decode a hex public key, raising 400 on error (mirrors
        companion_endpoints._pub_key_from_hex)."""
        try:
            if (
                not isinstance(hex_str, str)
                or len(hex_str) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in hex_str)
            ):
                raise ValueError("Expected 64 hexadecimal characters")
            key = bytes.fromhex(hex_str)
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
            coro.close()
            raise _DispatchUnavailable()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.event_loop)
        except Exception as exc:
            coro.close()
            logger.warning("Companion event loop unavailable: %s", exc)
            raise _DispatchUnavailable() from exc
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise cherrypy.HTTPError(504, "Timed out waiting for radio response")
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.exception("Companion operation failed")
            raise cherrypy.HTTPError(500, "Companion operation failed") from exc

    def _principal(self) -> Tuple[str, str]:
        """Return a typed, server-owned principal for limits and idempotency."""

        user = getattr(cherrypy.request, "user", None) or {}
        if user.get("auth_type") == "api_token" and user.get("token_id") is not None:
            device = self._paired_device_by_token(user["token_id"])
            if device and device.get("device_id"):
                # A device row's integer id changes after revoke/re-pair. The
                # identity-qualified client id remains stable for retries on
                # the same companion without crossing identity rotations.
                return "device", companion_device_principal_id(
                    device.get("companion_identity"),
                    device.get("companion_hash"),
                    device["device_id"],
                )
            return "token", str(user["token_id"])
        if user.get("auth_type") in ("jwt", "jwt_query"):
            username = str(user.get("username") or "unknown")
            client_id = str(user.get("client_id") or "unknown")
            return "jwt", f"{username}:{client_id}"
        raise cherrypy.HTTPError(401, "Authenticated principal required")

    def _admit_rf(self, *, cost: float = 1.0) -> None:
        """Apply the same small per-principal budget to every REST RF action."""

        principal_type, principal_id = self._principal()
        retry_after = consume_all(
            (
                (
                    self._rf_limiter,
                    f"{principal_type}:{principal_id}",
                    cost,
                ),
                (self._rf_global_limiter, "mobile-api", cost),
            )
        )
        if retry_after is None:
            return
        retry_seconds = max(1, int(retry_after + 0.999))
        cherrypy.response.headers["Retry-After"] = str(retry_seconds)
        raise cherrypy.HTTPError(429, "RF request rate exceeded")

    def _idempotency_lock(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
    ):
        """Return one bounded stripe for the immutable idempotency key."""

        index = hash((principal_type, principal_id, idempotency_key)) % len(self._idempotency_locks)
        return self._idempotency_locks[index]

    def _lookup_or_reserve_outbound(
        self,
        handler,
        journal,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        outbound: dict,
    ) -> dict:
        """Atomically gate RF quota and the durable send reservation."""

        lock = self._idempotency_lock(
            principal_type,
            principal_id,
            idempotency_key,
        )
        with lock:
            try:
                reservation = handler.companion_idempotency_lookup(
                    principal_type,
                    principal_id,
                    idempotency_key,
                    request_hash,
                )
            except CompanionStorageError as exc:
                logger.error("Idempotency lookup failed: %s", exc)
                raise cherrypy.HTTPError(
                    503,
                    "Companion storage unavailable",
                ) from exc
            local_indeterminate = self._get_unpersisted_indeterminate(
                principal_type,
                principal_id,
                idempotency_key,
            )
            if local_indeterminate is not None:
                if local_indeterminate.get("request_hash") != request_hash:
                    if reservation is not None:
                        return reservation
                    local_conflict = dict(local_indeterminate)
                    local_conflict["result"] = "conflict"
                    return local_conflict
                if reservation is None:
                    # The process observed an RF outcome that it could not
                    # persist. A live DB replacement or row loss must not turn
                    # that remembered ambiguity into permission to send again.
                    local_result = dict(local_indeterminate)
                    local_result["state"] = "indeterminate"
                    local_result["result"] = "indeterminate"
                    return local_result
                if reservation is not None:
                    if reservation.get("state") in {
                        "complete",
                        "failed",
                        "indeterminate",
                    }:
                        self._forget_unpersisted_indeterminate(
                            principal_type,
                            principal_id,
                            idempotency_key,
                        )
                    else:
                        # Storage is readable again. Repair both durable rows
                        # before reporting the locally remembered outcome; a
                        # failed repair remains fail-closed in this map.
                        repaired = self._mark_send_indeterminate(
                            journal,
                            principal_type,
                            principal_id,
                            idempotency_key,
                            request_hash,
                            local_indeterminate["message_id"],
                            local_indeterminate.get("packet_hash"),
                            local_indeterminate.get("expected_ack"),
                        )
                        if repaired is not None:
                            self._forget_unpersisted_indeterminate(
                                principal_type,
                                principal_id,
                                idempotency_key,
                            )
                        if repaired is not None and repaired.get("state") in {
                            "complete",
                            "failed",
                        }:
                            reservation = dict(repaired)
                            reservation["result"] = "replay"
                        else:
                            reservation = dict(reservation)
                            reservation.update(local_indeterminate)
                            reservation["state"] = "indeterminate"
                            reservation["result"] = "indeterminate"
            if reservation is not None:
                return reservation
            with self._unpersisted_indeterminate_lock:
                safety_lost = self._unpersisted_indeterminate_safety_lost
            if safety_lost:
                # At least one process-local marker was evicted, so an absent
                # SQLite row is no longer proof that this key never reached
                # RF. Block all absent-key sends until restart rather than
                # risk transmitting an evicted key twice.
                return {
                    "result": "indeterminate",
                    "state": "indeterminate",
                    "message_id": None,
                    "packet_hash": None,
                    "expected_ack": None,
                }

            # Replaying an existing response is not RF work. Consume quota
            # only while creating the one durable reservation that may send.
            self._admit_rf()
            try:
                return journal.reserve_outbound_send(
                    principal_type,
                    principal_id,
                    idempotency_key,
                    request_hash,
                    outbound,
                )
            except CompanionStorageError as exc:
                logger.error("Outbound reservation failed: %s", exc)
                raise cherrypy.HTTPError(
                    503,
                    "Companion storage unavailable",
                ) from exc

    @staticmethod
    def _unpersisted_indeterminate_key(
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
    ) -> tuple[str, str, str]:
        return principal_type, principal_id, idempotency_key

    def _remember_unpersisted_indeterminate(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        packet_hash: Optional[str],
        expected_ack: Optional[int],
    ) -> None:
        key = self._unpersisted_indeterminate_key(
            principal_type,
            principal_id,
            idempotency_key,
        )
        record = {
            "request_hash": request_hash,
            "message_id": message_id,
            "packet_hash": packet_hash,
            "expected_ack": expected_ack,
        }
        with self._unpersisted_indeterminate_lock:
            self._unpersisted_indeterminate.pop(key, None)
            self._unpersisted_indeterminate[key] = record
            if len(self._unpersisted_indeterminate) > _UNPERSISTED_INDETERMINATE_MAX:
                oldest = next(iter(self._unpersisted_indeterminate))
                self._unpersisted_indeterminate.pop(oldest, None)
                self._unpersisted_indeterminate_safety_lost = True
                logger.error(
                    "Process-local idempotency safety coverage was exhausted; "
                    "blocking absent-key RF sends until process restart"
                )

    def _get_unpersisted_indeterminate(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
    ) -> Optional[dict]:
        key = self._unpersisted_indeterminate_key(
            principal_type,
            principal_id,
            idempotency_key,
        )
        with self._unpersisted_indeterminate_lock:
            record = self._unpersisted_indeterminate.get(key)
            return dict(record) if record is not None else None

    def _forget_unpersisted_indeterminate(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
    ) -> None:
        key = self._unpersisted_indeterminate_key(
            principal_type,
            principal_id,
            idempotency_key,
        )
        with self._unpersisted_indeterminate_lock:
            self._unpersisted_indeterminate.pop(key, None)

    @staticmethod
    def _sse_principal() -> str:
        """Return the same non-secret principal key used by the legacy route."""

        user = getattr(cherrypy.request, "user", None) or {}
        auth_type = str(user.get("auth_type") or "unknown")
        if auth_type in ("jwt", "jwt_query"):
            username = str(user.get("username") or "unknown")
            client_id = str(user.get("client_id") or "unknown")
            return f"jwt:{username}:{client_id}"
        if auth_type == "api_token" and user.get("token_id") is not None:
            return f"api_token:{user['token_id']}"
        raise cherrypy.HTTPError(401, "Authenticated principal required")

    def _begin_sse(self, principal: str, companion_identity: str) -> bool:
        return self._sse_admission.acquire(principal, companion_identity)

    def _replace_sse(self, principal: str, companion_identity: str):
        return self._sse_admission.replace(principal, companion_identity)

    def _end_sse(
        self,
        principal: str,
        companion_identity: str,
        lease=None,
    ) -> None:
        self._sse_admission.release(principal, companion_identity, lease)

    @property
    def _sse_total(self) -> int:
        """Compatibility/readability view used by diagnostics and tests."""

        return self._sse_admission.active_count

    @staticmethod
    def _state_guard(bridge):
        """Serialize snapshot reads with snapshot-visible bridge mutations."""
        return getattr(bridge, "state_mutation_lock", _AsyncNullContext())

    @staticmethod
    async def _commit_blocking(bridge, function, *args):
        """Finish a started local commit before propagating cancellation."""
        commit = getattr(bridge, "_await_blocking_commit", None)
        if callable(commit):
            return await commit(function, *args)
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            try:
                await task
            except BaseException:
                raise
            raise cancellation

    @staticmethod
    async def _notify_contact_committed(
        bridge,
        change: str,
        contact: dict,
    ) -> None:
        """Notify transport observers only after state and journal agree."""
        notify = getattr(bridge, "notify_observers", None)
        if callable(notify):
            await notify("contact_committed", change, contact)

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
            get_by_name = getattr(identity_manager, "get_identity_by_name", None)
            registered = get_by_name(name) if callable(get_by_name) else None
            if registered is None and not callable(get_by_name):
                registered = next(
                    (
                        (identity, config, "companion")
                        for reg_name, identity, config in identity_manager.get_identities_by_type(
                            "companion"
                        )
                        if reg_name == name
                    ),
                    None,
                )
            if registered is not None:
                identity, _config, identity_type = registered
                if identity_type == "companion":
                    hash_byte = identity.get_public_key()[0]
                    bridge = bridges.get(hash_byte)
                    if bridge:
                        self._check_scope(name, identity.get_public_key().hex())
                        return bridge, f"0x{hash_byte:02x}"  # noqa: E231
        raise cherrypy.HTTPError(404, f"Companion '{name}' not found")

    def _scope_allows_companion(self, name: str, identity_hex: str) -> bool:
        user = getattr(cherrypy.request, "user", None)
        if not isinstance(user, dict):
            return False
        scope = user.get("scope")
        if scope in ("admin", "companion:*"):
            return True
        if user.get("auth_type") != "api_token":
            return scope == f"companion:{name}"

        # Exact-scope API tokens are device credentials. Their mutable display
        # name is not an authorization binding: require exactly one paired
        # device row and compare its complete immutable public identity.
        token_id = user.get("token_id")
        if token_id is None:
            return False
        device = self._paired_device_by_token(token_id)
        bound_identity = str((device or {}).get("companion_identity") or "").strip().lower()
        requested_identity = str(identity_hex or "").strip().lower()
        if len(bound_identity) != 64 or len(requested_identity) != 64:
            return False
        return secrets.compare_digest(bound_identity, requested_identity)

    def _paired_device_by_token(self, token_id: int) -> Optional[dict]:
        """Resolve an API token's device binding without failing open."""

        try:
            return self._get_sqlite_handler().companion_device_get_by_token_strict(token_id)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion device storage unavailable") from exc

    def _check_scope(self, name: str, identity_hex: str) -> None:
        """Enforce the caller's token scope against companion ``name``
        (design doc §11.1): ``admin``, ``companion:*`` (all companions), or
        ``companion:{name}`` (exact resolved name) are allowed.

        Out-of-scope access raises 404 with the SAME message ``_resolve``
        uses for unknown names, not 403 — a 403 would confirm to a scoped
        device token that some other companion name exists (the
        ``/companions`` listing is filtered for the same reason). Keep the
        message in sync with ``_resolve``'s not-found error.

        The authentication boundary always supplies an explicit normalized
        scope. Legacy API-token rows with a NULL scope are normalized there
        to ``admin``; a missing scope here is therefore an incomplete auth
        decision and fails closed.
        """
        if self._scope_allows_companion(name, identity_hex):
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
        journals = getattr(self.daemon_instance, "companion_journals", None) or {}
        journal = journals.get(companion_hash)
        if journal is not None:
            return journal
        servers = getattr(self.daemon_instance, "companion_frame_servers", None) or []
        for server in servers:
            if getattr(server, "companion_hash", None) == companion_hash:
                return getattr(server, "journal", None)
        return None

    def _sse_settings(self) -> Tuple[int, int]:
        """Return the constructor-validated shared SSE settings."""

        return self._sse_queue_maxsize, self._sse_keepalive_sec

    @staticmethod
    def _etag_not_modified(etag: str) -> bool:
        """Set the ETag header; return True (and set 304) on If-None-Match hit."""
        cherrypy.response.headers["Cache-Control"] = "private, no-store, no-cache, no-transform"
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
    def _message_to_wire(message: dict) -> dict:
        """Remove Frame-delivery bookkeeping from the public message model.

        A Frame client can consume a pending queue entry without appending a
        journal event. Exposing that internal state would therefore make a
        snapshot ETag lie: the representation could change while its cursor
        and ETag remained stable.
        """
        wire = {field: message[field] for field in _MESSAGE_EVENT_FIELDS if field in message}
        if "packet_hash" in wire:
            wire["packet_hash"] = _normalize_hash16(wire.get("packet_hash"))
        return wire

    @staticmethod
    def _contact_event_to_wire(contact: dict) -> dict:
        """Normalize journal storage fields to the public contact vocabulary.

        Contact rows use ``pubkey`` and retain raw advert/path bytes for Frame
        compatibility. Mobile snapshots use ``public_key`` and deliberately
        omit that internal material. Sync and SSE must expose the same shape
        so clients need only one contact parser.
        """
        public_key = contact.get("public_key", contact.get("pubkey"))
        if isinstance(public_key, (bytes, bytearray)):
            public_key = bytes(public_key).hex()
        wire: dict[str, object] = {}
        if public_key is not None:
            wire["public_key"] = str(public_key)
        for field in _CONTACT_EVENT_FIELDS:
            if field in contact:
                wire[field] = contact[field]
        if "flags" in wire:
            wire["favorite"] = bool(int(contact.get("flags") or 0) & CONTACT_FLAG_FAVOURITE)
        return wire

    @staticmethod
    def _event_type_to_wire(value: object) -> str:
        """Return one SSE-safe, human-readable event token."""

        if not isinstance(value, str) or not value:
            raise ValueError("event type must be a non-empty string")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("event type must be ASCII") from None
        if len(encoded) > _EVENT_TYPE_MAX_BYTES or any(
            not (character.isalnum() or character in {"_", ".", "-"}) for character in value
        ):
            raise ValueError("event type must use 1 to 64 ASCII letters, digits, '_', '.', or '-'")
        if value == "snapshot_required":
            raise ValueError("snapshot_required is reserved for the SSE reset control event")
        return value

    @staticmethod
    def _project_event_fields(payload: dict, fields: tuple[str, ...]) -> dict:
        """Copy only the documented public fields of a known event."""

        return {field: payload[field] for field in fields if field in payload}

    @staticmethod
    def _wire_integer(
        value: object,
        field: str,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        if (
            type(value) is not int
            or (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)
        ):
            raise ValueError(f"{field} is not a valid integer")
        return value

    @staticmethod
    def _wire_number(
        value: object,
        field: str,
        *,
        nullable: bool = False,
    ) -> Optional[float]:
        if value is None and nullable:
            return None
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"{field} is not a finite number")
        return float(value)

    @staticmethod
    def _wire_packet_hash(
        value: object,
        field: str,
        *,
        nullable: bool = False,
    ) -> Optional[str]:
        if value is None and nullable:
            return None
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).hex()
        if not isinstance(value, str) or value != value.strip():
            raise ValueError(f"{field} is not a packet hash")
        text = value[2:] if value.lower().startswith("0x") else value
        if len(text) not in {16, 64} or not _is_hex(text):
            raise ValueError(f"{field} is not a packet hash")
        return text[:16].upper()

    @staticmethod
    def _validate_wire_path(value: object, field: str) -> None:
        if not isinstance(value, list) or not all(
            isinstance(hop, str) and bool(hop) for hop in value
        ):
            raise ValueError(f"{field} is not a path array")

    @classmethod
    def _validate_message_event(cls, wire: dict) -> None:
        if set(wire) != set(_MESSAGE_EVENT_FIELDS):
            raise ValueError("message event is incomplete or has unknown fields")
        integer_ranges = {
            "id": (1, SQLITE_ROW_ID_MAX),
            "txt_type": (0, 0x3F),
            "timestamp": (0, _UINT32_MAX),
            "channel_idx": (0, 0xFF),
            "path_len": (0, 0xFF),
            "channel_data_type": (0, 0xFFFF),
            "observation_count": (0, SQLITE_ROW_ID_MAX),
            "unique_path_count": (0, SQLITE_ROW_ID_MAX),
        }
        for field, (minimum, maximum) in integer_ranges.items():
            if field in wire:
                cls._wire_integer(
                    wire[field],
                    f"message.{field}",
                    minimum=minimum,
                    maximum=maximum,
                )
        if "rssi" in wire:
            cls._wire_integer(wire["rssi"], "message.rssi")
        if "is_channel" in wire and type(wire["is_channel"]) is not bool:
            raise ValueError("message.is_channel is not a boolean")
        if "text" in wire and not isinstance(wire["text"], str):
            raise ValueError("message.text is not a string")
        if "companion_hash" in wire and (
            not isinstance(wire["companion_hash"], str)
            or len(wire["companion_hash"]) != 4
            or not wire["companion_hash"].startswith("0x")
            or wire["companion_hash"] != wire["companion_hash"].lower()
            or not _is_hex(wire["companion_hash"][2:])
        ):
            raise ValueError("message.companion_hash is invalid")
        for field in ("sender_key", "recipient_key"):
            if field in wire and (
                not isinstance(wire[field], str)
                or (wire[field] != "" and (len(wire[field]) != 64 or not _is_hex(wire[field])))
            ):
                raise ValueError(f"message.{field} is invalid")
            if field in wire:
                wire[field] = wire[field].lower()
        for field in ("sender_prefix", "channel_data_payload"):
            if field in wire and (
                not isinstance(wire[field], str) or (wire[field] and not _is_hex(wire[field]))
            ):
                raise ValueError(f"message.{field} is invalid")
            if field in wire:
                wire[field] = wire[field].lower()
        for field in ("snr", "created_at"):
            if field in wire:
                cls._wire_number(wire[field], f"message.{field}")
        if "packet_hash" in wire:
            wire["packet_hash"] = cls._wire_packet_hash(
                wire["packet_hash"],
                "message.packet_hash",
                nullable=True,
            )
        if "direction" in wire and wire["direction"] not in {"in", "out"}:
            raise ValueError("message.direction is invalid")
        if "state" in wire and wire["state"] not in _MESSAGE_STATES:
            raise ValueError("message.state is invalid")
        if "expected_ack" in wire and wire["expected_ack"] is not None:
            cls._wire_integer(
                wire["expected_ack"],
                "message.expected_ack",
                minimum=0,
                maximum=_UINT32_MAX,
            )
        if "source" in wire and wire["source"] not in {
            None,
            "radio",
            "rest",
            "frame",
            "operator",
        }:
            raise ValueError("message.source is invalid")
        if (
            "observation_count" in wire
            and "unique_path_count" in wire
            and wire["unique_path_count"] > wire["observation_count"]
        ):
            raise ValueError("message observation counters are invalid")

    @classmethod
    def _validate_rf_event(
        cls,
        wire: dict,
        *,
        event_type: str,
        require_message_id: bool = False,
        counter_fields: tuple[str, str] = (),
    ) -> None:
        if require_message_id:
            cls._wire_integer(
                wire.get("message_id"),
                f"{event_type}.message_id",
                minimum=1,
                maximum=SQLITE_ROW_ID_MAX,
            )
        wire["packet_hash"] = cls._wire_packet_hash(
            wire.get("packet_hash"),
            f"{event_type}.packet_hash",
        )
        cls._validate_wire_path(wire.get("path"), f"{event_type}.path")
        cls._wire_number(
            wire.get("rssi"),
            f"{event_type}.rssi",
            nullable=True,
        )
        cls._wire_number(
            wire.get("snr"),
            f"{event_type}.snr",
            nullable=True,
        )
        cls._wire_number(
            wire.get("observed_at"),
            f"{event_type}.observed_at",
        )
        if counter_fields:
            first, second = counter_fields
            cls._wire_integer(wire.get(first), f"{event_type}.{first}", minimum=0)
            cls._wire_integer(wire.get(second), f"{event_type}.{second}", minimum=0)
            if wire[second] > wire[first]:
                raise ValueError(f"{event_type} counters are invalid")

    @classmethod
    def _known_event_payload_to_wire(
        cls,
        event_type: str,
        payload: dict,
    ) -> dict:
        """Project one documented event payload onto its public vocabulary."""

        if event_type == "message":
            if "packet_hash" in payload:
                cls._wire_packet_hash(
                    payload["packet_hash"],
                    "message.packet_hash",
                    nullable=True,
                )
            wire = cls._message_to_wire(payload)
            cls._validate_message_event(wire)
            return wire
        if event_type == "contact":
            wire = cls._contact_event_to_wire(payload)
            expected_fields = {"public_key", "favorite", *_CONTACT_EVENT_FIELDS}
            if set(wire) != expected_fields:
                raise ValueError("contact event is incomplete or has unknown fields")
            public_key = wire.get("public_key")
            if not isinstance(public_key, str) or len(public_key) != 64 or not _is_hex(public_key):
                raise ValueError("contact.public_key is invalid")
            wire["public_key"] = public_key.lower()
            if not isinstance(wire.get("name"), str):
                raise ValueError("contact.name is invalid")
            for field in (
                "adv_type",
                "flags",
                "out_path_len",
                "last_advert_timestamp",
                "lastmod",
            ):
                cls._wire_integer(wire.get(field), f"contact.{field}")
            if type(wire.get("favorite")) is not bool:
                raise ValueError("contact.favorite is invalid")
            if wire["favorite"] != bool(wire["flags"] & CONTACT_FLAG_FAVOURITE):
                raise ValueError("contact.favorite does not match contact.flags")
            for field in ("gps_lat", "gps_lon"):
                cls._wire_number(wire.get(field), f"contact.{field}", nullable=True)
            if wire.get("change") not in {"new", "update", "remove", "path"}:
                raise ValueError("contact.change is invalid")
            return wire
        if event_type == "channel":
            wire = cls._project_event_fields(payload, _CHANNEL_EVENT_FIELDS)
            if (
                set(wire) != set(_CHANNEL_EVENT_FIELDS)
                or type(wire.get("index")) is not int
                or wire["index"] < 0
                or wire.get("change") not in {"update", "remove"}
                or (wire["change"] == "update" and not isinstance(wire.get("name"), str))
                or (wire["change"] == "remove" and wire.get("name") is not None)
            ):
                raise ValueError("channel event has invalid public fields")
            return wire
        if event_type == "prefs":
            wire = cls._project_event_fields(payload, PUBLIC_PREF_FIELDS)
            if not wire:
                raise ValueError("prefs event has no public fields")
            for field in _PUBLIC_PREF_STRING_FIELDS:
                if field in wire and not isinstance(wire[field], str):
                    raise ValueError(f"prefs.{field} is invalid")
            for field in _PUBLIC_PREF_INTEGER_FIELDS:
                if field in wire:
                    cls._wire_integer(wire[field], f"prefs.{field}")
            for field in _PUBLIC_PREF_NUMBER_FIELDS:
                if field in wire:
                    cls._wire_number(wire[field], f"prefs.{field}")
            return wire
        if event_type == "message_reception":
            wire = cls._project_event_fields(
                payload,
                _MESSAGE_RECEPTION_EVENT_FIELDS,
            )
            if set(wire) != set(_MESSAGE_RECEPTION_EVENT_FIELDS):
                raise ValueError("message_reception event is incomplete")
            cls._validate_rf_event(
                wire,
                event_type=event_type,
                require_message_id=True,
                counter_fields=("observation_count", "unique_path_count"),
            )
            return wire
        if event_type == "message_send_state":
            wire = cls._project_event_fields(
                payload,
                _MESSAGE_SEND_STATE_EVENT_FIELDS,
            )
            lifecycle_fields = {
                "message_id",
                "state",
                "packet_hash",
                "expected_ack",
            }
            heard_repeat_fields = {
                "message_id",
                "state",
                "packet_hash",
                "path",
                "terminal_repeater_hash",
                "rssi",
                "snr",
                "observed_at",
                "heard_repeat_count",
                "unique_repeater_count",
            }
            if frozenset(wire) not in {
                frozenset(lifecycle_fields),
                frozenset(heard_repeat_fields),
            }:
                raise ValueError("message_send_state event has an invalid public shape")
            cls._wire_integer(
                wire.get("message_id"),
                "message_send_state.message_id",
                minimum=1,
                maximum=SQLITE_ROW_ID_MAX,
            )
            if wire.get("state") not in _MESSAGE_STATES.difference({"received"}):
                raise ValueError("message_send_state.state is invalid")
            wire["packet_hash"] = cls._wire_packet_hash(
                wire.get("packet_hash"),
                "message_send_state.packet_hash",
                nullable=True,
            )
            if "expected_ack" in wire and wire["expected_ack"] is not None:
                cls._wire_integer(
                    wire["expected_ack"],
                    "message_send_state.expected_ack",
                    minimum=0,
                    maximum=_UINT32_MAX,
                )
            if set(wire) == heard_repeat_fields:
                cls._validate_rf_event(
                    wire,
                    event_type=event_type,
                    counter_fields=("heard_repeat_count", "unique_repeater_count"),
                )
                terminal = wire["terminal_repeater_hash"]
                if terminal is not None and (not isinstance(terminal, str) or not terminal):
                    raise ValueError("message_send_state.terminal_repeater_hash is invalid")
            return wire
        if event_type == _RF_RECEPTION_EVENT_TYPE:
            wire = cls._project_event_fields(
                payload,
                _RF_RECEPTION_EVENT_FIELDS,
            )
            if set(wire) != set(_RF_RECEPTION_EVENT_FIELDS):
                raise ValueError("rf_reception event is incomplete")
            cls._validate_rf_event(wire, event_type=event_type)
            return wire
        # Preserve the safe envelope so older clients can advance their
        # cursor across newer event types. Do not publish an internal payload
        # until its public vocabulary has been reviewed and defined above.
        return {}

    @classmethod
    def _event_to_wire(cls, row: dict) -> dict:
        """Return the one event representation shared by sync and SSE."""
        seq = cls._wire_integer(
            row["seq"],
            "event.seq",
            minimum=1,
            maximum=SQLITE_ROW_ID_MAX,
        )
        event_type = cls._event_type_to_wire(row["event_type"])
        created_at = cls._wire_number(row["created_at"], "event.created_at")
        packet_hash = cls._wire_packet_hash(
            row.get("packet_hash"),
            "event.packet_hash",
            nullable=True,
        )
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        payload = cls._known_event_payload_to_wire(event_type, payload)
        if event_type in _PACKET_HASH_EVENT_TYPES and payload["packet_hash"] != packet_hash:
            raise ValueError("event packet_hash does not match data.packet_hash")
        return _to_json_safe(
            {
                "seq": seq,
                "type": event_type,
                "ts": created_at,
                "packet_hash": packet_hash,
                "data": payload,
            }
        )

    @classmethod
    def _event_page_to_wire(
        cls,
        rows: list[dict],
        *,
        include_rf_receptions: bool,
    ) -> list[dict]:
        """Convert one durable page or classify malformed rows as storage."""

        try:
            return [
                cls._event_to_wire(row)
                for row in rows
                if (include_rf_receptions or row["event_type"] != _RF_RECEPTION_EVENT_TYPE)
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CompanionStorageError("Companion event has invalid public wire fields") from exc

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
        and public keys. ``admin``/``companion:*`` callers see everything.
        """
        self._require_get()
        if not self.daemon_instance:
            raise cherrypy.HTTPError(503, "Daemon not initialized")
        identity_manager = getattr(self.daemon_instance, "identity_manager", None)
        registrations = (
            identity_manager.get_identities_by_type("companion") if identity_manager else []
        )
        allowed = []
        for name, identity, _config in registrations:
            identity_hex = identity.get_public_key().hex()
            if not self._scope_allows_companion(name, identity_hex):
                continue
            allowed.append((name, identity.get_public_key()[0], identity_hex))

        async def snapshot():
            bridges = getattr(self.daemon_instance, "companion_bridges", {})
            items = []
            for name, hash_byte, identity_hex in allowed:
                bridge = bridges.get(hash_byte)
                if bridge is None:
                    continue
                async with self._state_guard(bridge):
                    node_name = bridge.prefs.node_name
                items.append(
                    {
                        "name": name,
                        "companion_hash": f"0x{hash_byte:02x}",  # noqa: E231
                        "node_name": node_name,
                        "public_key": identity_hex,
                    }
                )
            return items

        items = self._run_async(snapshot())
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

        try:
            sync_state = handler.companion_sync_state(companion_hash)
        except CompanionStorageError as exc:
            logger.error("Snapshot sync-state read failed for %s: %s", companion_hash, exc)
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        head = sync_state["head"]
        epoch = sync_state["epoch"]
        version_tag = hashlib.sha256(
            str(_REPEATER_VERSION or "unknown").encode("utf-8")
        ).hexdigest()[:12]
        etag = f'"snapshot:{companion_hash}:{epoch}:{head}:{limit}:{version_tag}"'
        if self._etag_not_modified(etag):
            return None

        async def read_bridge_state():
            async with self._state_guard(bridge):
                prefs = bridge.get_self_info()
                self_info = {"public_key": bridge.get_public_key().hex()}
                self_info.update(
                    {field: _to_json_safe(getattr(prefs, field)) for field in PUBLIC_PREF_FIELDS}
                )
                contacts = [self._contact_to_json(contact) for contact in bridge.get_contacts()]
                channels = []
                for idx in range(bridge.channels.max_channels):
                    channel = bridge.channels.get(idx)
                    if channel:
                        channels.append({"index": idx, "name": channel.name})
                return self_info, contacts, channels

        self_info, contacts, channels = self._run_async(read_bridge_state(), timeout=5.0)

        # Stored newest-first; snapshot delivers oldest-first for direct
        # client-side append order.
        try:
            messages = [
                self._message_to_wire(message)
                for message in reversed(
                    handler.companion_get_messages_strict(
                        companion_hash,
                        limit=limit,
                    )
                )
            ]
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc

        return self._success(
            {
                "journal_epoch": epoch,
                "cursor": sync_state["cursor"],
                "self": self_info,
                "contacts": contacts,
                "channels": channels,
                "messages": messages,
                "server": {"version": _REPEATER_VERSION},
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
        (comma-separated; unknown or empty tokens are rejected). The filter is applied
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
        cursor_epoch, cursor_seq = _parse_cursor(cursor)
        limit_n = self._clamp(limit, default=100, low=1, high=500)

        try:
            page = handler.companion_sync_page(
                companion_hash,
                cursor_epoch,
                cursor_seq,
                limit_n,
            )
        except CompanionStorageError as exc:
            logger.error("Sync page read failed for %s: %s", companion_hash, exc)
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        if not page["valid"]:
            if page["reason"] == "invalid_cursor":
                raise cherrypy.HTTPError(400, "Invalid cursor")
            return self._success(
                {
                    "journal_epoch": page["epoch"],
                    "events": [],
                    "next_cursor": page["cursor"],
                    "has_more": False,
                    "snapshot_required": True,
                    "reset_reason": page["reason"],
                }
            )

        rows = page["events"]
        want_rf_receptions = _include_rf_receptions(include)
        try:
            events = self._event_page_to_wire(
                rows,
                include_rf_receptions=want_rf_receptions,
            )
        except CompanionStorageError as exc:
            logger.error("Sync event conversion failed for %s: %s", companion_hash, exc)
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        # Cursor tracks the last row the query scanned, not the last row
        # returned to the client — a filtered-out rf_reception row still
        # advances it, so a client that opts in later doesn't re-scan rows
        # it already passed.
        return self._success(
            {
                "journal_epoch": page["epoch"],
                "events": events,
                "next_cursor": page["next_cursor"],
                "has_more": page["has_more"],
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

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def advert(self, companion_name=None, **kwargs):
        """POST .../advert — send this companion's own advert.

        ``mode: flood`` uses the persisted flood scope; ``mode: local`` sends
        the same advert as a direct zero-hop packet. This is the API equivalent
        of the legacy Frame ``CMD_SEND_SELF_ADVERT`` command.
        """
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        body = self._get_json_body()
        reject_unknown_fields(body, {"mode"})
        mode = body.get("mode")
        if mode not in ("flood", "local"):
            raise cherrypy.HTTPError(400, "mode must be 'flood' or 'local'")
        self._admit_rf()
        sent = self._run_async(bridge.advertise(flood=mode == "flood"))
        return self._success({"sent": bool(sent), "mode": mode})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def anonymous_request(self, companion_name=None, **kwargs):
        """POST .../anonymous_request — query public metadata by full key."""

        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        body = self._get_json_body()
        reject_unknown_fields(body, {"public_key", "request"})
        pub_key = self._pub_key_from_hex(body.get("public_key"))
        request_name = body.get("request")
        request_types = {
            "regions": ANON_REQ_TYPE_REGIONS,
            "owner": ANON_REQ_TYPE_OWNER,
            "basic": ANON_REQ_TYPE_BASIC,
        }
        if not isinstance(request_name, str) or request_name not in request_types:
            raise cherrypy.HTTPError(
                400,
                "request must be 'regions', 'owner', or 'basic'",
            )

        self._admit_rf()
        timeout = 15.0
        result = self._run_async(
            bridge.request_anonymous(
                pub_key,
                bytes([request_types[request_name]]),
                timeout=timeout,
            ),
            timeout=timeout + 5.0,
        )
        if result.get("success"):
            result = {
                **result,
                "public_key": pub_key.hex(),
                "request": request_name,
            }
        return self._success(_to_json_safe(result))

    def _message_history(self, companion_name, before_id, limit):
        _bridge, companion_hash = self._resolve(companion_name)
        handler = self._get_sqlite_handler()

        limit_n = self._clamp(limit, default=100, low=1, high=200)
        before = None
        if before_id is not None:
            before = positive_sqlite_row_id(before_id, "before_id")

        try:
            rows = handler.companion_get_messages_strict(
                companion_hash,
                before_id=before,
                limit=limit_n,
            )
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        next_before_id = rows[-1]["id"] if rows else None
        messages = [self._message_to_wire(message) for message in rows]
        return self._success({"messages": messages, "next_before_id": next_before_id})

    def _send_message(self, companion_name):
        """POST /api/v1/companions/{name}/messages — send a DM or channel
        message (§7.3): bridge calls mirror companion_endpoints.send_text /
        send_channel_message exactly. Wrapped with the mandatory
        Idempotency-Key contract (§6): a retry with the same key and the
        same body replays the stored response without touching the radio;
        the same key against a different body — or a different companion —
        is a 409. Both successful and failed terminal responses are replayed;
        an outcome that may have reached RF is retained as ``indeterminate``
        so a retry cannot silently double-send it.

        A successful response also carries ``packet_hash``: the 16-char
        correlation key (§10.2) for the packet that was just transmitted,
        captured off ``RepeaterCompanionBridge._send_packet`` via the
        ``outbound_send_capture`` contextvar (§10.4). Clients can use it to
        match a send against later ``message_send_state`` heard-repeat
        events without waiting on a round trip through the journal. Absent
        on failure — a send that never reached the radio has no packet_hash.
        """
        bridge, companion_hash = self._resolve(companion_name)

        idempotency_key = cherrypy.request.headers.get("Idempotency-Key")
        if not idempotency_key:
            raise cherrypy.HTTPError(400, "Idempotency-Key header required")
        if not isinstance(idempotency_key, str):
            raise cherrypy.HTTPError(400, "Idempotency-Key must be a string")
        if len(idempotency_key) > _IDEMPOTENCY_KEY_MAX_BYTES or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in idempotency_key
        ):
            raise cherrypy.HTTPError(
                400,
                "Idempotency-Key must contain 1 to 128 visible ASCII characters without whitespace",
            )

        body = self._get_json_body()
        reject_unknown_fields(body, {"to", "channel_idx", "text", "txt_type"})
        to_hex = body.get("to")
        channel_idx = body.get("channel_idx")
        has_to = to_hex is not None
        has_channel = channel_idx is not None  # 0 is a valid channel index
        if has_to == has_channel:
            raise cherrypy.HTTPError(400, "Exactly one of 'to' or 'channel_idx' required")
        text = text_field(body, "text", required=True, max_bytes=MAX_TEXT_LEN)
        if "\x00" in text:
            # MeshCore text payloads are C strings. Accepting an embedded NUL
            # would persist one value while peers display only its prefix (and
            # direct-message ACK correlation would use a different hash).
            raise cherrypy.HTTPError(400, "text must not contain NUL")

        handler = self._get_sqlite_handler()
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")
        principal_type, principal_id = self._principal()

        if has_to:
            pub_key = self._pub_key_from_hex(to_hex)
            txt_type = integer_field(body, "txt_type", default=0, low=0, high=0)
            target = {"to": pub_key.hex(), "txt_type": txt_type}
        else:
            if "txt_type" in body:
                raise cherrypy.HTTPError(400, "txt_type is only valid for direct messages")
            idx = integer_field(body, "channel_idx", low=0)
            max_channels = getattr(getattr(bridge, "channels", None), "max_channels", 40)
            if idx is None or idx >= max_channels:
                raise cherrypy.HTTPError(400, "channel_idx out of range")
            channel_text_max = channel_text_capacity(bridge.prefs.node_name)
            if _utf8_size(text, "text") > channel_text_max:
                raise cherrypy.HTTPError(
                    400,
                    f"text exceeds {channel_text_max} UTF-8 bytes for this channel sender name",
                )
            target = {"channel_idx": idx}

        canonical_request = {
            "companion_identity": bridge.get_public_key().hex(),
            "text": text,
            **target,
        }
        canonical = json.dumps(
            canonical_request,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        outbound = {
            "sender_key": bridge.get_public_key(),
            "recipient_key": pub_key if has_to else None,
            "text": text,
            "timestamp": int(time.time()),
            "is_channel": not has_to,
            "channel_idx": idx if not has_to else 0,
            "txt_type": txt_type if has_to else 0,
        }

        reservation = self._lookup_or_reserve_outbound(
            handler,
            journal,
            principal_type,
            principal_id,
            idempotency_key,
            request_hash,
            outbound,
        )

        reservation_result = reservation["result"]
        if reservation_result == "conflict":
            raise cherrypy.HTTPError(409, "Idempotency-Key reuse with different request")
        if reservation_result == "replay":
            cherrypy.response.headers["Idempotency-Replayed"] = "true"
            try:
                return parse_companion_send_response(reservation["response_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                logger.error("Stored idempotency response is invalid: %s", exc)
                raise cherrypy.HTTPError(503, "Stored send response is unavailable") from exc
        if reservation_result == "in_progress":
            cherrypy.response.headers["Retry-After"] = "1"
            raise cherrypy.HTTPError(409, "A send with this Idempotency-Key is in progress")
        if reservation_result == "indeterminate":
            cherrypy.response.status = 409
            return {
                "success": False,
                "error": "The prior send outcome is indeterminate; do not retry with a new key",
                "data": {
                    "state": "indeterminate",
                    "message_id": reservation.get("message_id"),
                    "packet_hash": reservation.get("packet_hash"),
                    "expected_ack": reservation.get("expected_ack"),
                },
            }

        try:
            message_id = int(reservation["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise cherrypy.HTTPError(
                503,
                "Outbound reservation is missing its message",
            ) from exc

        packet_hash = None
        expected_ack = None
        send_capture = {}
        try:
            if has_to:
                bridge_result = self._run_async(
                    _send_and_capture(
                        lambda: bridge.send_text_message(
                            pub_key, text, txt_type=txt_type, wait_for_ack=False
                        ),
                        capture=send_capture,
                        message_id=message_id,
                    )
                )
            else:
                bridge_result = self._run_async(
                    _send_and_capture(
                        lambda: bridge.send_channel_message(idx, text),
                        capture=send_capture,
                        message_id=message_id,
                    )
                )
            packet_hash = send_capture.get("hash")
            expected_ack = send_capture.get("expected_ack")
        except ChannelTextCapacityError as exc:
            # The name changed after the request-thread validation but before
            # Core built a packet. Nothing reached RF, so this is a definite,
            # terminal result and is safe to replay with the same key.
            sent_ok = False
            data = {
                "message_id": message_id,
                "sent": False,
                "state": "failed",
                "reason": str(exc),
            }
            normalized_hash = None
            final_state = "failed"
            response = self._success(data)
            response_json = json.dumps(
                response,
                separators=(",", ":"),
                allow_nan=False,
            )
        except _DispatchUnavailable:
            sent_ok = False
            data = {
                "message_id": message_id,
                "sent": False,
                "state": "failed",
                "reason": "Radio dispatch is unavailable",
            }
            normalized_hash = None
            final_state = "failed"
            response = self._success(data)
            response_json = json.dumps(
                response,
                separators=(",", ":"),
                allow_nan=False,
            )
        except Exception as exc:
            # The bridge may have published RF acceptance before later
            # post-transmit work timed out, was cancelled, or failed. Preserve
            # that already-known correlation while keeping the outcome
            # conservatively indeterminate.
            packet_hash = send_capture.get("hash")
            expected_ack = send_capture.get("expected_ack")
            logger.warning(
                "Radio send did not return a definite result for message %s: %s",
                message_id,
                exc,
            )
            return self._indeterminate_send_response(
                journal,
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                "The radio send outcome is indeterminate; retry only with the same key",
                packet_hash=packet_hash,
                expected_ack=expected_ack,
            )
        else:
            try:
                initial_state = send_capture.get("initial_state")
                if initial_state not in {
                    None,
                    "transmitted",
                    "failed",
                    "indeterminate",
                }:
                    raise ValueError("radio send capture has invalid initial_state")
                if expected_ack is not None and (
                    type(expected_ack) is not int or not 0 <= expected_ack <= _UINT32_MAX
                ):
                    raise ValueError("radio send capture has invalid expected_ack")
                if initial_state == "indeterminate":
                    return self._indeterminate_send_response(
                        journal,
                        principal_type,
                        principal_id,
                        idempotency_key,
                        request_hash,
                        message_id,
                        "The radio send outcome is indeterminate; retry only with the same key",
                        packet_hash=packet_hash,
                        expected_ack=expected_ack,
                    )
                if has_to:
                    success = bridge_result.success
                    is_flood = bridge_result.is_flood
                    result_expected_ack = bridge_result.expected_ack
                    error_code = bridge_result.error
                    if type(success) is not bool or type(is_flood) is not bool:
                        raise ValueError("direct-message send result has invalid flags")
                    if result_expected_ack is not None and (
                        type(result_expected_ack) is not int
                        or not 0 <= result_expected_ack <= _UINT32_MAX
                    ):
                        raise ValueError("direct-message send result has invalid expected_ack")
                    if error_code is not None and not isinstance(error_code, str):
                        raise ValueError("direct-message send result has invalid error")
                    if result_expected_ack is not None:
                        expected_ack = result_expected_ack
                    if initial_state == "transmitted":
                        sent_ok = True
                    elif initial_state == "failed":
                        if success:
                            raise ValueError(
                                "direct-message result conflicts with failed radio capture"
                            )
                        sent_ok = False
                    else:
                        sent_ok = success
                    data = {
                        "message_id": message_id,
                        "sent": sent_ok,
                        "state": "transmitted" if sent_ok else "failed",
                        "is_flood": is_flood,
                        "expected_ack": expected_ack,
                    }
                    if not sent_ok:
                        if error_code == "not_found":
                            data["reason"] = "Contact not found"
                        elif initial_state == "failed":
                            data["reason"] = "Radio rejected the direct-message send"
                        else:
                            data["reason"] = "Direct-message send failed before radio dispatch"
                else:
                    if type(bridge_result) is not bool:
                        raise ValueError("channel send result must be a boolean")
                    if initial_state == "transmitted":
                        sent_ok = True
                    elif initial_state == "failed":
                        if bridge_result:
                            raise ValueError("channel result conflicts with failed radio capture")
                        sent_ok = False
                    else:
                        sent_ok = bridge_result
                    data = {
                        "message_id": message_id,
                        "sent": sent_ok,
                        "state": "transmitted" if sent_ok else "failed",
                    }
                    if not sent_ok:
                        data["reason"] = (
                            "Radio rejected the channel send"
                            if initial_state == "failed"
                            else "Channel send failed before radio dispatch"
                        )

                normalized_hash = _normalize_hash16(packet_hash)
                if normalized_hash:
                    data["packet_hash"] = normalized_hash
                final_state = "transmitted" if sent_ok else "failed"
                response = self._success(data)
                response_json = json.dumps(
                    response,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except Exception as exc:
                logger.error(
                    "Invalid post-dispatch result for message %s: %s",
                    message_id,
                    exc,
                )
                return self._indeterminate_send_response(
                    journal,
                    principal_type,
                    principal_id,
                    idempotency_key,
                    request_hash,
                    message_id,
                    "The radio send outcome is indeterminate; retry only with the same key",
                    packet_hash=packet_hash,
                    expected_ack=expected_ack,
                )

        try:
            completion = journal.complete_outbound_send(
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                final_state,
                response_json,
                normalized_hash,
                expected_ack,
            )
            if not isinstance(completion, dict):
                raise CompanionStorageError("Outbound completion returned an invalid result")
            completion_state = completion.get("state")
            if completion_state == "indeterminate":
                return self._indeterminate_send_response(
                    journal,
                    principal_type,
                    principal_id,
                    idempotency_key,
                    request_hash,
                    message_id,
                    "The radio send outcome is indeterminate; retry only with the same key",
                    packet_hash=normalized_hash,
                    expected_ack=expected_ack,
                )
            if completion_state not in {"complete", "failed"}:
                raise CompanionStorageError(
                    "Outbound completion did not reach a durable terminal state"
                )
        except Exception as exc:
            logger.error("Post-send state persistence failed for message %s: %s", message_id, exc)
            return self._indeterminate_send_response(
                journal,
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                "The message may have been sent, but confirmation storage failed",
                packet_hash=normalized_hash,
                expected_ack=expected_ack,
            )
        return response

    def _indeterminate_send_response(
        self,
        journal,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        error: str,
        *,
        packet_hash=None,
        expected_ack=None,
    ) -> dict:
        """Fail closed after dispatch while preserving any safe correlation."""

        try:
            normalized_hash = _normalize_hash16(packet_hash)
        except Exception as exc:
            logger.warning(
                "Could not normalize packet hash for indeterminate message %s: %s",
                message_id,
                exc,
            )
            normalized_hash = None
        safe_expected_ack = (
            expected_ack if type(expected_ack) is int and 0 <= expected_ack <= _UINT32_MAX else None
        )
        persisted = self._mark_send_indeterminate(
            journal,
            principal_type,
            principal_id,
            idempotency_key,
            request_hash,
            message_id,
            normalized_hash,
            safe_expected_ack,
        )
        if persisted is not None and persisted.get("state") in {
            "complete",
            "failed",
        }:
            try:
                recovered_response = parse_companion_send_response(persisted["response_json"])
            except (KeyError, TypeError, ValueError, RecursionError) as exc:
                logger.error(
                    "Committed send response is unavailable for message %s: %s",
                    message_id,
                    exc,
                )
            else:
                cherrypy.response.status = 200
                cherrypy.response.headers["Idempotency-Replayed"] = "true"
                return recovered_response
        if persisted is None:
            self._remember_unpersisted_indeterminate(
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                normalized_hash,
                safe_expected_ack,
            )
        data = {"message_id": message_id, "state": "indeterminate"}
        if normalized_hash is not None:
            data["packet_hash"] = normalized_hash
        if safe_expected_ack is not None:
            data["expected_ack"] = safe_expected_ack
        cherrypy.response.status = 503
        return {"success": False, "error": error, "data": data}

    @staticmethod
    def _mark_send_indeterminate(
        journal,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> Optional[dict]:
        """Return the durable terminal record after a fail-closed transition."""

        try:
            result = journal.mark_outbound_send_indeterminate(
                principal_type,
                principal_id,
                idempotency_key,
                request_hash,
                message_id,
                packet_hash,
                expected_ack,
            )
            if not isinstance(result, dict) or result.get("state") not in {
                "indeterminate",
                "complete",
                "failed",
            }:
                raise CompanionStorageError(
                    "Indeterminate transition did not reach a durable terminal state"
                )
            return result
        except Exception:
            logger.exception(
                "Could not atomically mark outbound send %s indeterminate",
                message_id,
            )
            return None

    # ------------------------------------------------------------------
    # GET /api/v1/companions/{name}/events  (SSE, design doc §8)
    # ------------------------------------------------------------------

    @classmethod
    def _sse_frame(cls, row: dict, epoch: str) -> str:
        """Format one journal row (or listener event — same shape) as an SSE
        frame: ``id:`` = seq, ``event:`` = event type, ``data:`` = the same
        JSON object ``sync`` returns for this row (§8, one schema/two
        transports)."""
        return cls._sse_wire_frame(cls._event_to_wire(row), epoch)

    @staticmethod
    def _sse_wire_frame(event: dict, epoch: str) -> str:
        seq = event["seq"]
        event_type = event["type"]
        data = json.dumps(event, separators=(",", ":"), allow_nan=False)
        return f"id: {_format_cursor(epoch, seq)}\nevent: {event_type}\ndata: {data}\n\n"

    @cherrypy.expose
    def events(self, companion_name=None, cursor=None, include=None, **kwargs):
        """GET /api/v1/companions/{name}/events — resumable SSE live stream.

        Use an Authorization header. Browser-native ``EventSource`` cannot
        supply one, so browser clients should use a streaming ``fetch``;
        query-string credentials are deliberately limited to short-lived
        operator JWTs by the auth boundary and are not a mobile-token
        transport.

        Listener notifications are wake signals only. Every event is read
        back from SQLite in sequence order, so concurrent writers cannot
        reorder a stream and a one-item queue can safely coalesce bursts.
        """
        self._require_get()
        bridge, companion_hash = self._resolve(companion_name)
        companion_identity = bridge.get_public_key().hex().lower()
        want_rf_receptions = _include_rf_receptions(include)
        handler = self._get_sqlite_handler()
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        stream_user = getattr(cherrypy.request, "user", None)
        stream_scope = stream_user.get("scope") if isinstance(stream_user, dict) else None
        stream_auth_type = stream_user.get("auth_type") if isinstance(stream_user, dict) else None
        stream_token_id = stream_user.get("token_id") if isinstance(stream_user, dict) else None

        def _token_still_bound(_token_info: dict) -> bool:
            if stream_auth_type != "api_token" or stream_scope in ("admin", "companion:*"):
                return True
            device = handler.companion_device_get_by_token_strict(stream_token_id)
            bound_identity = str((device or {}).get("companion_identity") or "").strip().lower()
            return (
                len(bound_identity) == 64
                and len(companion_identity) == 64
                and secrets.compare_digest(bound_identity, companion_identity)
            )

        try:
            authorization = AuthorizationLease.from_request(
                cherrypy.request,
                cherrypy.config.get("token_manager"),
                token_check=_token_still_bound,
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
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Authentication storage unavailable") from exc
        except Exception as exc:
            logger.error("Mobile SSE authorization recheck is unavailable")
            raise cherrypy.HTTPError(503, "Authentication unavailable") from exc

        last_event_id = cherrypy.request.headers.get("Last-Event-ID")
        cursor_param = last_event_id if last_event_id is not None else cursor
        if cursor_param is None:
            try:
                state = handler.companion_sync_state(companion_hash)
            except CompanionStorageError as exc:
                raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
            cursor_epoch = state["epoch"]
            cursor_seq = state["head"]
        else:
            cursor_epoch, cursor_seq = _parse_cursor(cursor_param)

        try:
            status = handler.companion_cursor_status(
                companion_hash,
                cursor_epoch,
                cursor_seq,
            )
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        _queue_maxsize, keepalive_sec = self._sse_settings()

        first_page = None
        first_page_wire = None
        if status["valid"]:
            try:
                # Validate and retain the first page before returning a
                # streaming response. A corrupt durable row therefore yields
                # the normal JSON 503 instead of a silent stream disconnect.
                first_page = handler.companion_sync_page(
                    companion_hash,
                    status["epoch"],
                    cursor_seq,
                    500,
                )
                first_page_wire = self._event_page_to_wire(
                    first_page["events"],
                    include_rf_receptions=want_rf_receptions,
                )
            except CompanionStorageError as exc:
                raise cherrypy.HTTPError(
                    503,
                    "Companion storage unavailable",
                ) from exc

        stream_principal = self._sse_principal()
        stream_lease = self._replace_sse(stream_principal, companion_identity)
        if stream_lease is None:
            cherrypy.response.headers["Retry-After"] = "5"
            raise cherrypy.HTTPError(
                429,
                "Only one event stream per credential and companion is allowed",
            )
        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-store, no-cache, no-transform"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"

        if not status["valid"]:

            def _snapshot_required_stream():
                if not self._sse_admission.is_current(
                    stream_principal,
                    companion_identity,
                    stream_lease,
                ):
                    return
                try:
                    if not authorization.is_active():
                        return
                except Exception:
                    logger.warning(
                        "Mobile SSE authorization ended before reset notice for %s",
                        companion_hash,
                    )
                    return
                payload = json.dumps(
                    {
                        "journal_epoch": status["epoch"],
                        "cursor": status["cursor"],
                        "snapshot_required": True,
                        "reset_reason": status["reason"],
                    },
                    separators=(",", ":"),
                    allow_nan=False,
                )
                yield f"event: snapshot_required\ndata: {payload}\n\n"

            return _ClosingIterator(
                _snapshot_required_stream(),
                lambda: self._end_sse(
                    stream_principal,
                    companion_identity,
                    stream_lease,
                ),
            )

        wake_queue: queue.Queue = queue.Queue(maxsize=1)

        def _on_event(_event: dict) -> None:
            try:
                wake_queue.put_nowait(None)
            except queue.Full:
                pass

        def _bridge_is_active() -> bool:
            try:
                hash_byte = bridge.get_public_key()[0]
            except (AttributeError, IndexError, TypeError):
                return False
            bridges = getattr(self.daemon_instance, "companion_bridges", {})
            return bridges.get(hash_byte) is bridge

        def _authorization_is_active() -> bool:
            try:
                return authorization.is_active()
            except CompanionStorageError:
                logger.warning(
                    "Mobile SSE authorization storage became unavailable for %s",
                    companion_hash,
                )
                return False
            except Exception:
                logger.error(
                    "Mobile SSE authorization recheck failed for %s",
                    companion_hash,
                )
                return False

        def _stream_is_current() -> bool:
            return self._sse_admission.is_current(
                stream_principal,
                companion_identity,
                stream_lease,
            )

        def generate():
            last_sent_seq = cursor_seq
            stream_epoch = status["epoch"]
            page = first_page
            page_wire = first_page_wire
            page_was_prefetched = page is not None
            listener_registered = False
            try:
                if (
                    not _stream_is_current()
                    or not _bridge_is_active()
                    or not _authorization_is_active()
                ):
                    return
                journal.register_listener(_on_event)
                listener_registered = True
                stream_started = False
                while True:
                    if (
                        not _stream_is_current()
                        or not _bridge_is_active()
                        or not _authorization_is_active()
                    ):
                        return
                    if page is None:
                        page = handler.companion_sync_page(
                            companion_hash,
                            stream_epoch,
                            last_sent_seq,
                            500,
                        )
                        page_wire = None
                        page_was_prefetched = False
                    if not page["valid"]:
                        payload = json.dumps(
                            {
                                "journal_epoch": page["epoch"],
                                "cursor": page["cursor"],
                                "snapshot_required": True,
                                "reset_reason": page["reason"],
                            },
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        yield f"event: snapshot_required\ndata: {payload}\n\n"
                        return
                    rows = page["events"]
                    if rows:
                        last_sent_seq = rows[-1]["seq"]
                    if page_wire is None:
                        page_wire = self._event_page_to_wire(
                            rows,
                            include_rf_receptions=want_rf_receptions,
                        )
                    for event in page_wire:
                        if not _stream_is_current() or not _authorization_is_active():
                            return
                        yield self._sse_wire_frame(event, stream_epoch)
                        stream_started = True
                    has_more = page["has_more"]
                    refresh_before_wait = page_was_prefetched
                    page = None
                    page_wire = None
                    if has_more:
                        continue
                    if refresh_before_wait:
                        # An event can land after the handler's eager first
                        # read but before this lazy generator registers its
                        # listener. One immediate indexed refresh closes that
                        # gap without turning the stream into a polling loop.
                        continue
                    if not stream_started:
                        # SSE comments are ignored by clients but flush the
                        # authenticated response through proxies immediately
                        # when there is no replay event to send.
                        yield ": connected\n\n"
                        stream_started = True
                    keepalive_deadline = time.monotonic() + keepalive_sec
                    woke_for_event = False
                    while True:
                        if (
                            not _stream_is_current()
                            or not _bridge_is_active()
                            or not _authorization_is_active()
                        ):
                            return
                        keepalive_remaining = keepalive_deadline - time.monotonic()
                        if keepalive_remaining <= 0:
                            break
                        wait_for = authorization.check_in(keepalive_remaining)
                        try:
                            wake_queue.get(timeout=wait_for)
                        except queue.Empty:
                            continue
                        woke_for_event = True
                        break
                    if woke_for_event:
                        continue
                    # Purging/restoring companion state rotates the cursor
                    # epoch but does not append a journal event. Revalidate
                    # before emitting an idle keepalive so a quiet stream
                    # cannot continue advertising a stale cursor lineage.
                    idle_status = handler.companion_cursor_status(
                        companion_hash,
                        stream_epoch,
                        last_sent_seq,
                    )
                    if not idle_status["valid"]:
                        payload = json.dumps(
                            {
                                "journal_epoch": idle_status["epoch"],
                                "cursor": idle_status["cursor"],
                                "snapshot_required": True,
                                "reset_reason": idle_status["reason"],
                            },
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        yield f"event: snapshot_required\ndata: {payload}\n\n"
                        return
                    if not _stream_is_current() or not _authorization_is_active():
                        return
                    yield ": ka\n\n"
            except GeneratorExit:
                pass
            except CompanionStorageError as exc:
                logger.warning("Mobile SSE storage failed for %s: %s", companion_hash, exc)
            except Exception as exc:
                logger.debug("Mobile SSE stream ended for %s: %s", companion_hash, exc)
            finally:
                if listener_registered:
                    journal.unregister_listener(_on_event)

        return _ClosingIterator(
            generate(),
            lambda: self._end_sse(
                stream_principal,
                companion_identity,
                stream_lease,
            ),
        )

    events._cp_config = {"response.stream": True}

    # ------------------------------------------------------------------
    # POST /api/v1/companions/{name}/contacts/{pubkey}/{action}  (§7.3)
    #
    # Thin wrappers over the same CompanionBridge coroutines
    # companion_endpoints.py calls, routed here via _cp_dispatch. No
    # idempotency handling — these request/response actions do transmit RF,
    # but repeating one is a fresh query/login attempt rather than a duplicate
    # durable chat message. Their timeout means "outcome unknown", not "nothing
    # was sent"; callers decide whether and when to try again.
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
        reject_unknown_fields(body, {"password"})
        password = text_field(body, "password", default="", max_bytes=15)
        if "\x00" in password:
            # Core's login credential is a C string. Its response state must
            # retain the exact same password the peer receives.
            raise cherrypy.HTTPError(400, "password must not contain NUL")
        self._admit_rf()
        result = self._run_async(bridge.send_login(pub_key, password), timeout=15.0)
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def connection(self, companion_name=None, contact_pubkey=None, **kwargs):
        """GET .../contacts/{pubkey}/connection — current login-session state."""

        self._require_get()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)

        async def read_connection():
            return bridge.has_login_connection(pub_key)

        connected = self._run_async(read_connection())
        return self._success({"connected": bool(connected)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def logout(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/logout — end a remote login session."""

        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        reject_unknown_fields(self._get_json_body(), set())
        self._admit_rf()
        sent = self._run_async(bridge.send_logout(pub_key))
        return self._success({"logged_out": True, "sent": bool(sent)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def status_request(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/status_request  (empty body) — remote
        status query."""
        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        reject_unknown_fields(self._get_json_body(), set())
        self._admit_rf()
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
        reject_unknown_fields(self._get_json_body(), set())
        self._admit_rf()
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
    def ping(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/ping — direct TRACE from this companion."""

        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        reject_unknown_fields(self._get_json_body(), set())
        contact = bridge.contacts.get_by_key(pub_key)
        if contact is None:
            raise cherrypy.HTTPError(404, "Contact not found")
        if (int(getattr(contact, "adv_type", ADV_TYPE_NONE)) & 0x0F) != ADV_TYPE_REPEATER:
            raise cherrypy.HTTPError(400, "Ping is only available for repeaters")

        self._admit_rf()
        timeout = 15.0
        result = self._run_async(
            bridge.ping_contact(pub_key, timeout=timeout),
            timeout=timeout + 5.0,
        )
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def path_discovery(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/path_discovery — actively query its route."""

        self._require_post()
        bridge, _companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        reject_unknown_fields(self._get_json_body(), set())
        contact = bridge.contacts.get_by_key(pub_key)
        if contact is None or getattr(contact, "adv_type", ADV_TYPE_NONE) == ADV_TYPE_NONE:
            raise cherrypy.HTTPError(404, "Contact not found")

        self._admit_rf()
        timeout = 15.0
        result = self._run_async(
            bridge.discover_path(pub_key, timeout=timeout),
            timeout=timeout + 5.0,
        )
        if result.get("success"):
            result = {**result, "public_key": pub_key.hex()}
        return self._success(_to_json_safe(result))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def reset_path(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST .../contacts/{pubkey}/reset_path  (empty body) — reset the
        outbound routing path for a contact."""
        self._require_post()
        bridge, companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        reject_unknown_fields(self._get_json_body(), set())
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        async def mutate_locked():
            existing = bridge.contacts.get_by_key(pub_key)
            if existing is None or getattr(existing, "adv_type", ADV_TYPE_NONE) == ADV_TYPE_NONE:
                raise cherrypy.HTTPError(404, "Contact not found")
            before = copy.deepcopy(existing)
            if not bridge.reset_path(pub_key):
                return False
            updated = bridge.contacts.get_by_key(pub_key)
            stored = self._contact_to_storage_dict(updated)
            try:
                await self._commit_blocking(
                    bridge,
                    journal.store_contact,
                    stored,
                    "path",
                )
            except Exception as exc:
                bridge.add_update_contact(before)
                raise cherrypy.HTTPError(503, "Contact storage unavailable") from exc
            return stored

        async def mutate():
            async with self._state_guard(bridge):
                stored = await mutate_locked()
            if stored:
                await self._notify_contact_committed(
                    bridge,
                    "path",
                    stored,
                )
            return bool(stored)

        return self._success({"reset": self._run_async(mutate())})

    # ------------------------------------------------------------------
    # Contact and channel management (routed via _cp_dispatch as collection
    # members). These close the gaps inventoried in
    # docs/architecture/companion-frame-vs-rest.md: before them a client's
    # contact list was read-only and a channel could not be joined at all.
    # ------------------------------------------------------------------

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def contact(self, companion_name=None, contact_pubkey=None, **kwargs):
        """POST|DELETE .../contacts/{pubkey} — add/update or remove a contact.

        POST body: ``{name?, adv_type?, favorite?, gps_lat?, gps_lon?}``.
        Learned routing fields and raw flags are server-owned.

        Adverts already auto-add contacts (``autoadd_config`` /
        ``autoadd_max_hops``), so POST is mainly for the ones auto-add filtered
        out — wrong type, or too many hops away.
        """
        method = cherrypy.request.method
        if method == "POST":
            return self._upsert_contact(companion_name, contact_pubkey)
        if method == "DELETE":
            return self._delete_contact(companion_name, contact_pubkey)
        cherrypy.response.headers["Allow"] = "POST, DELETE"
        raise cherrypy.HTTPError(405, "Method not allowed. Use POST or DELETE.")

    def _upsert_contact(self, companion_name, contact_pubkey):
        from openhop_core.companion.models import Contact

        bridge, companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        body = self._get_json_body()
        reject_unknown_fields(
            body,
            {"name", "adv_type", "favorite", "gps_lat", "gps_lon"},
        )
        name = text_field(
            body,
            "name",
            max_bytes=CONTACT_NAME_SIZE,
            strip=True,
        )
        if name is not None:
            reject_control_characters(name, "name")
            if not name:
                raise cherrypy.HTTPError(400, "name must not be empty")
        # Core reserves zero for ADV_TYPE_NONE, a transient/non-contact value
        # that is deliberately absent from contact snapshots.
        adv_type = integer_field(
            body,
            "adv_type",
            low=ADV_TYPE_CHAT,
            high=255,
        )
        gps_lat = finite_float_field(body, "gps_lat", low=-90.0, high=90.0)
        gps_lon = finite_float_field(body, "gps_lon", low=-180.0, high=180.0)
        favorite = body.get("favorite")
        if favorite is not None and not isinstance(favorite, bool):
            raise cherrypy.HTTPError(400, "favorite must be a boolean")
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        async def mutate_locked():
            previous = copy.deepcopy(bridge.contacts.get_by_key(pub_key))
            existing = (
                previous
                if previous is not None
                and getattr(previous, "adv_type", ADV_TYPE_NONE) != ADV_TYPE_NONE
                else None
            )
            if existing is None and name is None:
                raise cherrypy.HTTPError(400, "name is required for a new contact")
            # A transient anon-request route is not a public contact. An
            # explicit upsert promotes it to a real contact while retaining
            # useful learned routing state.
            source = previous
            flags = int(getattr(source, "flags", 0))
            if favorite is not None:
                if favorite:
                    flags |= CONTACT_FLAG_FAVOURITE
                else:
                    flags &= ~CONTACT_FLAG_FAVOURITE
            contact = Contact(
                public_key=pub_key,
                name=name if name is not None else getattr(existing, "name", ""),
                adv_type=(
                    adv_type
                    if adv_type is not None
                    else getattr(existing, "adv_type", ADV_TYPE_CHAT)
                ),
                flags=flags,
                out_path_len=getattr(source, "out_path_len", -1),
                out_path=getattr(source, "out_path", b""),
                last_advert_timestamp=getattr(source, "last_advert_timestamp", 0),
                lastmod=int(time.time()),
                gps_lat=(gps_lat if gps_lat is not None else getattr(source, "gps_lat", 0.0)),
                gps_lon=(gps_lon if gps_lon is not None else getattr(source, "gps_lon", 0.0)),
                sync_since=getattr(source, "sync_since", 0),
                last_advert_packet=getattr(source, "last_advert_packet", None),
            )
            if not bridge.add_update_contact(contact):
                raise cherrypy.HTTPError(507, "Contact store is full")
            change = "new" if existing is None else "update"
            try:
                stored = self._contact_to_storage_dict(contact)
                await self._commit_blocking(
                    bridge,
                    journal.store_contact,
                    stored,
                    change,
                )
            except Exception as exc:
                if previous is None:
                    bridge.remove_contact(pub_key)
                else:
                    bridge.add_update_contact(previous)
                raise cherrypy.HTTPError(503, "Contact storage unavailable") from exc
            return (
                self._contact_to_json(contact),
                change,
                stored,
            )

        async def mutate():
            async with self._state_guard(bridge):
                result, change, stored = await mutate_locked()
            await self._notify_contact_committed(bridge, change, stored)
            return result

        return self._success({"contact": self._run_async(mutate())})

    def _delete_contact(self, companion_name, contact_pubkey):
        bridge, companion_hash = self._resolve(companion_name)
        pub_key = self._pub_key_from_hex(contact_pubkey)
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        async def mutate_locked():
            current = copy.deepcopy(bridge.contacts.get_by_key(pub_key))
            if current is None or getattr(current, "adv_type", ADV_TYPE_NONE) == ADV_TYPE_NONE:
                raise cherrypy.HTTPError(404, "Contact not found")
            if not bridge.remove_contact(pub_key):
                return False
            try:
                await self._commit_blocking(
                    bridge,
                    journal.remove_contact,
                    pub_key,
                )
            except Exception as exc:
                bridge.add_update_contact(current)
                raise cherrypy.HTTPError(503, "Contact storage unavailable") from exc
            return self._contact_to_storage_dict(current)

        async def mutate():
            async with self._state_guard(bridge):
                stored = await mutate_locked()
            if stored:
                await self._notify_contact_committed(
                    bridge,
                    "remove",
                    stored,
                )
            return bool(stored)

        return self._success({"removed": self._run_async(mutate())})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def channel(self, companion_name=None, channel_index=None, **kwargs):
        """PUT|DELETE .../channels/{index} — join/rename or clear a channel.

        PUT body: ``{name, secret}``. ``secret`` is the channel PSK as hex
        (32 or 64 chars) and is **write-only** — it is accepted here but never
        returned by any v1 endpoint, so the surface still never hands out a
        secret (§11.4). Clients learn the PSK out of band (QR code, invite),
        exactly as MeshCore clients do.

        Without this a REST-only client could not join a channel at all: the
        snapshot withholds secrets and there was no way to supply one.
        """
        method = cherrypy.request.method
        if method == "PUT":
            return self._set_channel(companion_name, channel_index)
        if method == "DELETE":
            return self._clear_channel(companion_name, channel_index)
        cherrypy.response.headers["Allow"] = "PUT, DELETE"
        raise cherrypy.HTTPError(405, "Method not allowed. Use PUT or DELETE.")

    @staticmethod
    def _channel_index(raw) -> int:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            raise cherrypy.HTTPError(400, "Channel index must be an integer") from None
        if index < 0:
            raise cherrypy.HTTPError(400, "Channel index must not be negative")
        return index

    def _set_channel(self, companion_name, channel_index):
        bridge, companion_hash = self._resolve(companion_name)
        index = self._channel_index(channel_index)
        max_channels = getattr(getattr(bridge, "channels", None), "max_channels", 40)
        if index >= max_channels:
            raise cherrypy.HTTPError(404, f"Channel index out of range (max {max_channels - 1})")

        body = self._get_json_body()
        reject_unknown_fields(body, {"name", "secret"})
        name = text_field(
            body,
            "name",
            required=True,
            max_bytes=CHANNEL_NAME_SIZE,
            strip=True,
        )
        reject_control_characters(name, "name")
        secret_hex = text_field(body, "secret", required=True, max_bytes=64)
        if len(secret_hex) not in (32, 64) or not _is_hex(secret_hex):
            raise cherrypy.HTTPError(
                400,
                "secret must be exactly 32 or 64 hexadecimal characters",
            )
        secret = bytes.fromhex(secret_hex)

        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        async def mutate_locked():
            existing = copy.deepcopy(bridge.get_channel(index))
            if not bridge.set_channel(index, name, secret):
                raise cherrypy.HTTPError(400, "Channel could not be set")
            try:
                await self._commit_blocking(
                    bridge,
                    journal.store_channel,
                    index,
                    name,
                    secret,
                )
            except Exception as exc:
                if existing is None:
                    bridge.remove_channel(index)
                else:
                    bridge.set_channel(index, existing.name, existing.secret)
                raise cherrypy.HTTPError(503, "Channel storage unavailable") from exc
            return {"index": index, "name": name}

        async def mutate():
            async with self._state_guard(bridge):
                return await mutate_locked()

        return self._success({"channel": self._run_async(mutate())})

    def _clear_channel(self, companion_name, channel_index):
        bridge, companion_hash = self._resolve(companion_name)
        index = self._channel_index(channel_index)
        max_channels = getattr(getattr(bridge, "channels", None), "max_channels", 40)
        if index >= max_channels:
            raise cherrypy.HTTPError(
                404,
                f"Channel index out of range (max {max_channels - 1})",
            )
        journal = self._get_journal(companion_hash)
        if journal is None:
            raise cherrypy.HTTPError(503, "Companion event journal not available")

        async def mutate_locked():
            existing = copy.deepcopy(bridge.get_channel(index))
            if existing is None:
                raise cherrypy.HTTPError(404, "Channel not configured")
            if not bridge.remove_channel(index):
                raise cherrypy.HTTPError(400, "Channel could not be cleared")
            try:
                await self._commit_blocking(
                    bridge,
                    journal.store_channel,
                    index,
                    None,
                    None,
                )
            except Exception as exc:
                bridge.set_channel(index, existing.name, existing.secret)
                raise cherrypy.HTTPError(503, "Channel storage unavailable") from exc
            return True

        async def mutate():
            async with self._state_guard(bridge):
                return await mutate_locked()

        return self._success({"removed": self._run_async(mutate())})

    @staticmethod
    def _contact_to_storage_dict(contact) -> dict:
        """Storage shape (``pubkey`` key, raw bytes), mirroring
        ``frame_server._contact_to_dict``."""
        return {
            "pubkey": contact.public_key,
            "name": contact.name,
            "adv_type": contact.adv_type,
            "flags": contact.flags,
            "out_path_len": contact.out_path_len,
            "out_path": contact.out_path,
            "last_advert_timestamp": contact.last_advert_timestamp,
            "last_advert_packet": getattr(contact, "last_advert_packet", None),
            "lastmod": contact.lastmod,
            "gps_lat": contact.gps_lat,
            "gps_lon": contact.gps_lon,
            "sync_since": getattr(contact, "sync_since", 0),
        }

    @staticmethod
    def _contact_to_json(contact) -> dict:
        """Wire shape, matching ``snapshot.contacts`` (hex ``public_key``)."""
        return {
            "public_key": contact.public_key.hex(),
            "name": contact.name,
            "adv_type": contact.adv_type,
            "flags": contact.flags,
            "favorite": bool(int(contact.flags or 0) & CONTACT_FLAG_FAVOURITE),
            "out_path_len": contact.out_path_len,
            "last_advert_timestamp": contact.last_advert_timestamp,
            "lastmod": contact.lastmod,
            "gps_lat": contact.gps_lat,
            "gps_lon": contact.gps_lon,
        }

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

        msg_id = positive_sqlite_row_id(message_id, "message_id")

        try:
            msg = handler.companion_message_get_by_id_strict(companion_hash, msg_id)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
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
                    "truncated": False,
                    "observations_pruned": False,
                }
            )

        packet_hash_16 = _normalize_hash16(packet_hash)
        try:
            rows, truncated = handler.packets_receptions_strict(
                packet_hash_16,
                since,
                now,
                limit=_RF_OBSERVATION_LIMIT,
            )
            contacts = handler.companion_load_contacts_strict(companion_hash)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc

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
                # approximate -- design doc §10.4) journal counters. When
                # truncated=true, these counts describe the returned rows.
                "observation_count": len(receptions_out),
                "unique_path_count": len(unique_paths),
                "truncated": truncated,
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

        try:
            msg_rows, truncated = handler.companion_messages_by_sender_strict(
                companion_hash,
                sender_key,
                since,
                now,
                limit=self._CONTACT_PATHS_MESSAGE_LIMIT,
            )
            contacts = handler.companion_load_contacts_strict(companion_hash)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        packet_hashes = []
        seen_hashes = set()
        for row in msg_rows:
            ph = _normalize_hash16(row.get("packet_hash"))
            if ph and ph not in seen_hashes:
                seen_hashes.add(ph)
                packet_hashes.append(ph)

        # path tuple -> running aggregate
        aggregates: dict = {}
        total_observations = 0
        remaining = _RF_OBSERVATION_LIMIT
        for packet_index, ph in enumerate(packet_hashes):
            if remaining == 0:
                truncated = True
                break
            try:
                rows, page_truncated = handler.packets_receptions_strict(
                    ph,
                    since,
                    now,
                    limit=remaining,
                )
            except CompanionStorageError as exc:
                raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
            remaining -= len(rows)
            if page_truncated or (remaining == 0 and packet_index + 1 < len(packet_hashes)):
                truncated = True
            for row in rows:
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
                    "rssi_avg": (sum(rssi_values) / len(rssi_values) if rssi_values else None),
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
                "observation_limit": _RF_OBSERVATION_LIMIT,
                "truncated": truncated,
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

        raw_packet_hash = packet_hash.strip() if isinstance(packet_hash, str) else ""
        if raw_packet_hash.lower().startswith("0x"):
            raw_packet_hash = raw_packet_hash[2:]
        packet_hash_16 = _normalize_hash16(raw_packet_hash)
        if (
            len(raw_packet_hash) not in (16, 64)
            or not _is_hex(raw_packet_hash)
            or not packet_hash_16
        ):
            raise cherrypy.HTTPError(400, "Invalid packet_hash")

        window_seconds = self._parse_window(window)
        now = time.time()
        since = now - window_seconds

        try:
            owned_message = handler.companion_outbound_message_get_by_hash(
                companion_hash,
                packet_hash_16,
            )
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        if owned_message is None:
            raise cherrypy.HTTPError(404, "Transmission not found")

        try:
            # Only the earliest local transmission anchors the repeat query.
            tx_rows, _more_transmissions = handler.packets_transmissions_strict(
                packet_hash_16,
                since,
                now,
                limit=1,
            )
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc
        if not tx_rows:
            raise cherrypy.HTTPError(404, "Transmission not found")
        transmitted_at = min(row["timestamp"] for row in tx_rows)

        try:
            repeat_rows, truncated = handler.packets_heard_repeats_strict(
                packet_hash_16,
                transmitted_at,
                now,
                limit=_RF_OBSERVATION_LIMIT,
            )
            contacts = handler.companion_load_contacts_strict(companion_hash)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Companion storage unavailable") from exc

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
                "truncated": truncated,
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
    _MAX_ACTIVE_CODES = 128
    _CODE_GENERATION_ATTEMPTS = 8

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config if config is not None else {}
        self.event_loop = event_loop
        self._lock = threading.Lock()
        self._codes: dict = {}  # code -> {companion_name, companion_hash, issued_at}
        self._attempts = PrincipalTokenBucket(
            capacity=self._RATE_LIMIT_MAX,
            refill_per_second=self._RATE_LIMIT_MAX / self._RATE_LIMIT_WINDOW_SEC,
        )

    # ------------------------------------------------------------------
    # Small helpers (deliberately not shared with CompanionsV1 — pairing's
    # auth/resolve shape differs enough that reuse would be more confusing
    # than a few duplicated lines; see class docstring).
    # ------------------------------------------------------------------

    @staticmethod
    def _success(data, **kwargs):
        return _success_response(data, **kwargs)

    @staticmethod
    def _require_post():
        if cherrypy.request.method != "POST":
            cherrypy.response.headers["Allow"] = "POST"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST.")

    def _get_json_body(self) -> dict:
        return read_json_object(require_json_content_type=True)

    @staticmethod
    def _check_admin_scope() -> None:
        """Require the explicit normalized admin scope for ``pair/start``."""
        user = getattr(cherrypy.request, "user", None)
        if not isinstance(user, dict) or not is_admin_scope(user.get("scope")):
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
            for reg_name, identity, _cfg in identity_manager.get_identities_by_type("companion"):
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

    def _sweep_expired_locked(self, now: float | None = None) -> None:
        """Drop expired codes. Caller must hold ``self._lock``."""
        if now is None:
            now = time.monotonic()
        expired = [
            code for code, entry in self._codes.items() if now - entry["issued_at"] > self._TTL_SEC
        ]
        for code in expired:
            del self._codes[code]

    def _check_rate_limit(self) -> None:
        """Bound pairing guesses with a small per-source token bucket.

        The check runs before code lookup so known, unknown, and expired codes
        consume the same budget and expose no validity oracle.
        """
        remote = getattr(getattr(cherrypy.request, "remote", None), "ip", None) or "unknown"
        retry_after = self._attempts.consume(str(remote))
        if retry_after is not None:
            cherrypy.response.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
            raise cherrypy.HTTPError(429, "Too many pairing attempts, try again later")

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
        companion identity's public key; the app compares it with the value
        transferred through the trusted pairing channel. It detects an
        unexpected identity but is not a substitute for TLS. Assembling the
        QR code / pairing URL from these ingredients is the web UI's job.
        """
        self._require_post()
        self._check_admin_scope()
        body = self._get_json_body()
        reject_unknown_fields(body, {"companion_name"})
        companion_name = text_field(
            body,
            "companion_name",
            required=True,
            max_bytes=64,
        )
        try:
            companion_name = validate_companion_registration_name(companion_name)
        except ValueError as exc:
            raise cherrypy.HTTPError(400, str(exc)) from None
        companion_hash, pub_key = self._resolve_companion(companion_name)

        fingerprint = hashlib.sha256(pub_key).hexdigest()
        with self._lock:
            now = time.monotonic()
            self._sweep_expired_locked(now)
            if len(self._codes) >= self._MAX_ACTIVE_CODES:
                oldest = min(entry["issued_at"] for entry in self._codes.values())
                retry_after = max(
                    1,
                    int(self._TTL_SEC - (now - oldest) + 0.999),
                )
                cherrypy.response.headers["Retry-After"] = str(retry_after)
                raise cherrypy.HTTPError(
                    429,
                    "Too many active pairing codes; wait for one to expire",
                )
            for _attempt in range(self._CODE_GENERATION_ATTEMPTS):
                code = secrets.token_hex(16)  # 128-bit pairing code (§11.3)
                if code not in self._codes:
                    break
            else:
                logger.error("Could not generate a unique pairing code")
                raise cherrypy.HTTPError(
                    503,
                    "Pairing code generation is temporarily unavailable",
                )
            self._codes[code] = {
                "companion_name": companion_name,
                "companion_hash": companion_hash,
                "companion_identity": pub_key.hex(),
                "issued_at": now,
            }

        return self._success(
            {
                "code": code,
                "expires_in": int(self._TTL_SEC),
                "companion_name": companion_name,
                "companion_identity": pub_key.hex(),
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
        body = self._get_json_body()
        self._check_rate_limit()
        reject_unknown_fields(body, {"code", "device_id", "name", "platform"})
        code = text_field(body, "code", required=True, max_bytes=32)
        if len(code) != 32 or not _is_hex(code):
            raise cherrypy.HTTPError(400, "code must be 32 hexadecimal characters")
        code = code.lower()
        device_id = _device_id_text(body.get("device_id"))
        name = text_field(
            body,
            "name",
            required=True,
            max_bytes=_DEVICE_NAME_MAX_BYTES,
            strip=True,
        )
        reject_control_characters(name, "name")
        platform = text_field(
            body,
            "platform",
            max_bytes=_PLATFORM_MAX_BYTES,
            strip=True,
        )
        if platform is not None:
            reject_control_characters(platform, "platform")

        token_manager = cherrypy.config.get("token_manager")
        if not token_manager:
            raise cherrypy.HTTPError(500, "Authentication not configured")
        handler = self._get_sqlite_handler()

        with self._lock:
            self._sweep_expired_locked()
            entry = self._codes.pop(code, None)
        if entry is None:
            raise cherrypy.HTTPError(404, "Invalid or expired pairing code")

        companion_name = entry["companion_name"]
        companion_hash = entry["companion_hash"]
        companion_identity = entry["companion_identity"]
        try:
            current_hash, current_public_key = self._resolve_companion(companion_name)
        except cherrypy.HTTPError as exc:
            if exc.status == 404:
                raise cherrypy.HTTPError(
                    404,
                    "Invalid or expired pairing code",
                ) from exc
            raise
        current_identity = current_public_key.hex()
        if current_hash != companion_hash or not secrets.compare_digest(
            current_identity,
            companion_identity,
        ):
            raise cherrypy.HTTPError(404, "Invalid or expired pairing code")

        scope = f"companion:{companion_name}"  # noqa: E231
        plaintext = token_manager.generate_api_token()
        token_hash = token_manager.hash_token(plaintext)
        try:
            handler.companion_pair_device(
                companion_hash,
                companion_identity,
                device_id,
                name,
                f"device:{name}",
                token_hash,
                scope,
                platform,
            )
        except CompanionStorageError as exc:
            try:
                existing = handler.companion_device_get_strict(device_id)
            except CompanionStorageError:
                existing = None
            if existing is not None:
                raise cherrypy.HTTPError(409, "device_id already registered") from exc
            # The DB transaction did not commit; make a still-live code usable
            # again instead of turning a transient disk error into a new QR.
            if time.monotonic() - entry["issued_at"] <= self._TTL_SEC:
                with self._lock:
                    self._codes.setdefault(code, entry)
            raise cherrypy.HTTPError(503, "Pairing storage unavailable") from exc

        fingerprint = hashlib.sha256(bytes.fromhex(companion_identity)).hexdigest()
        return self._success(
            {
                "token": plaintext,
                "device_id": device_id,
                "companion_name": companion_name,
                "companion_identity": companion_identity,
                "fingerprint": fingerprint,
                "scope": scope,
            }
        )


class DevicesV1:
    """``/api/v1/devices`` — operator registry plus device self-service."""

    def __init__(self, daemon_instance=None, config=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.config = config if config is not None else {}
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
        return _success_response(data, **kwargs)

    @staticmethod
    def _check_admin_scope() -> None:
        user = getattr(cherrypy.request, "user", None)
        if not isinstance(user, dict) or not is_admin_scope(user.get("scope")):
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

        try:
            last_used_by_token = {
                token["id"]: token.get("last_used") for token in handler.list_api_tokens_strict()
            }
            devices = handler.companion_device_list_strict()
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Device storage unavailable") from exc

        items = []
        for device in devices:
            token_last_used = last_used_by_token.get(device["token_id"])
            last_seen = device.get("last_seen")
            if token_last_used is not None and (last_seen is None or token_last_used > last_seen):
                last_seen = token_last_used
            item = {
                "device_id": device["device_id"],
                "name": device["name"],
                "platform": device.get("platform"),
                "companion_hash": device["companion_hash"],
                "companion_identity": device.get("companion_identity"),
                "created_at": device["created_at"],
                "last_seen": last_seen,
                "push_registered": bool(device.get("push_token")),
                "push_detail": device.get("push_detail") or "none",
                "mention_push": bool(device.get("mention_push")),
            }
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
        row. Admins may revoke any device; a paired token may revoke itself."""
        if cherrypy.request.method != "DELETE":
            cherrypy.response.headers["Allow"] = "DELETE"
            raise cherrypy.HTTPError(405, "Method not allowed. Use DELETE.")
        device_id = _device_id_text(device_id)
        handler = self._get_sqlite_handler()
        self._check_device_or_admin(handler, device_id)
        user = getattr(cherrypy.request, "user", None) or {}
        expected_token_id = None if is_admin_scope(user.get("scope")) else user.get("token_id")
        try:
            result = handler.companion_revoke_device(
                device_id=device_id,
                expected_token_id=expected_token_id,
            )
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Device storage unavailable") from exc
        if not result["devices_deleted"]:
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
        return self._success({"revoked": True, "device_id": device_id})

    # ------------------------------------------------------------------
    # POST | DELETE /api/v1/devices/{device_id}/push  (routed via _cp_dispatch)
    # ------------------------------------------------------------------

    _VALID_PUSH_DETAIL = ("none", "count", "preview")

    def _get_json_body(self) -> dict:
        return read_json_object(
            require_json_content_type=True,
            allow_empty_without_content_type=True,
        )

    def _check_device_or_admin(self, handler, device_id: str) -> None:
        """A device manages its own push registration; admins manage any.

        Admin scope (web UI / admin token) passes unconditionally. Otherwise
        the caller must be authenticating with the very device-token paired to
        ``device_id`` — a scoped device token can register push only for
        itself, never for another device (mirrors the 404-folding choke point
        the rest of /api/v1 uses so a token can't probe other device ids).
        """
        user = getattr(cherrypy.request, "user", None)
        if not isinstance(user, dict):
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
        scope = user.get("scope")
        if is_admin_scope(scope):
            return
        if not is_companion_scope(scope):
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
        token_id = user.get("token_id")
        if token_id is not None:
            try:
                own = handler.companion_device_get_by_token_strict(token_id)
            except CompanionStorageError as exc:
                raise cherrypy.HTTPError(503, "Companion device storage unavailable") from exc
            if own is not None and own["device_id"] == device_id:
                return
        # Indistinguishable from "device not found" for a non-owning caller
        # (no cross-device existence leak).
        raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def push(self, device_id=None, **kwargs):
        """Register (POST) or clear (DELETE) a device's push credentials
        (design doc §12.2). POST body: ``{push_token, push_detail?}`` —
        ``push_token`` required; the relay URL is operator configuration, never
        device input. Auth: the device's own token, or admin."""
        method = cherrypy.request.method
        if method not in ("POST", "DELETE"):
            cherrypy.response.headers["Allow"] = "POST, DELETE"
            raise cherrypy.HTTPError(405, "Method not allowed. Use POST or DELETE.")
        device_id = _device_id_text(device_id)
        handler = self._get_sqlite_handler()
        self._check_device_or_admin(handler, device_id)
        user = getattr(cherrypy.request, "user", None) or {}
        expected_token_id = None if is_admin_scope(user.get("scope")) else int(user["token_id"])

        if method == "DELETE":
            try:
                if expected_token_id is None:
                    cleared = handler.companion_device_clear_push_strict(device_id)
                else:
                    cleared = handler.companion_device_clear_push_strict(
                        device_id,
                        expected_token_id=expected_token_id,
                    )
            except CompanionStorageError as exc:
                raise cherrypy.HTTPError(503, "Device storage unavailable") from exc
            if not cleared:
                raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")
            return self._success({"unregistered": True, "device_id": device_id})

        body = self._get_json_body()
        reject_unknown_fields(
            body,
            {"push_token", "push_detail", "mention_push", "mention_keywords"},
        )
        push_token = text_field(
            body,
            "push_token",
            required=True,
            max_bytes=_PUSH_TOKEN_MAX_BYTES,
        )
        if push_token != push_token.strip():
            raise cherrypy.HTTPError(
                400,
                "push_token must not have leading or trailing whitespace",
            )
        reject_control_characters(push_token, "push_token")

        push_detail = body.get("push_detail")
        if push_detail is not None and push_detail not in self._VALID_PUSH_DETAIL:
            raise cherrypy.HTTPError(
                400, f"push_detail must be one of {', '.join(self._VALID_PUSH_DETAIL)}"
            )

        mention_push = body.get("mention_push")
        if mention_push is not None and not isinstance(mention_push, bool):
            raise cherrypy.HTTPError(400, "mention_push must be a boolean")

        mention_keywords = body.get("mention_keywords")
        if mention_keywords is not None:
            if not isinstance(mention_keywords, list) or not all(
                isinstance(k, str) for k in mention_keywords
            ):
                raise cherrypy.HTTPError(400, "mention_keywords must be an array of strings")
            if len(mention_keywords) > _MAX_MENTION_KEYWORDS:
                raise cherrypy.HTTPError(
                    400,
                    f"mention_keywords may contain at most {_MAX_MENTION_KEYWORDS} entries",
                )
            normalized_keywords = []
            for keyword in mention_keywords:
                value = keyword.strip()
                if not value:
                    continue
                reject_control_characters(value, "mention keyword")
                if _utf8_size(value, "mention keyword") > _MENTION_KEYWORD_MAX_BYTES:
                    raise cherrypy.HTTPError(
                        400,
                        f"mention keyword exceeds {_MENTION_KEYWORD_MAX_BYTES} UTF-8 bytes",
                    )
                if value not in normalized_keywords:
                    normalized_keywords.append(value)
            mention_keywords = normalized_keywords

        try:
            push_kwargs = {
                # Clear legacy client-selected relay values. The notifier uses only
                # companion.push.relay_url from operator configuration.
                "push_relay_url": "",
                "push_detail": push_detail,
                "mention_push": mention_push,
                "mention_keywords": mention_keywords,
            }
            if expected_token_id is not None:
                push_kwargs["expected_token_id"] = expected_token_id
            updated = handler.companion_device_set_push_strict(
                device_id,
                push_token,
                **push_kwargs,
            )
            device = handler.companion_device_get_strict(device_id)
        except CompanionStorageError as exc:
            raise cherrypy.HTTPError(503, "Device storage unavailable") from exc
        if (
            not updated
            or device is None
            or (expected_token_id is not None and int(device["token_id"]) != expected_token_id)
        ):
            raise cherrypy.HTTPError(404, f"Device '{device_id}' not found")

        return self._success(
            {
                "device_id": device_id,
                "push_detail": device["push_detail"],
                "mention_push": device["mention_push"],
                "registered": True,
            }
        )
