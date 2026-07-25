"""Client for the Mobile Companion API v1 (``/api/v1/*``).

This is the surface a phone app lives on: pair once to get a device token,
fetch a snapshot to bootstrap, then follow the journal with ``sync``. The TCP
frame protocol (:mod:`companion_client.client`) is a different, lower-level
interface -- the two are not equivalent, and this one is the newer of the pair.

Stdlib ``urllib`` only, matching the repeater's own outbound HTTP (see
``push_notifier._default_poster``), so the client adds no dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

logger = logging.getLogger("companion_client.rest")

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_SSE_DATA_LINES = 4096
_CURSOR_MAX_BYTES = 128
_SQLITE_ROW_ID_MAX = (1 << 63) - 1
_UINT32_MAX = (1 << 32) - 1
_HASH16_RE = re.compile(r"^[0-9A-F]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PAIR_CODE_RE = re.compile(r"^[0-9a-f]{32}$")
_BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-._~+/]+={0,}$")
_BEARER_TOKEN_MAX_CHARS = 4096
_RESET_REASONS = {
    "missing_epoch",
    "epoch_mismatch",
    "future_cursor",
    "pruned_cursor",
}
_PUBLIC_PREF_STRING_FIELDS = frozenset({"node_name", "default_scope_name"})
_PUBLIC_PREF_INTEGER_FIELDS = frozenset(
    {
        "adv_type",
        "autoadd_config",
        "autoadd_max_hops",
        "path_hash_mode",
        "client_repeat",
        "manual_add_contacts",
        "telemetry_mode_base",
        "telemetry_mode_location",
        "telemetry_mode_environment",
        "advert_loc_policy",
        "multi_acks",
    }
)
_PUBLIC_PREF_NUMBER_FIELDS = frozenset({"latitude", "longitude", "rx_delay_base", "airtime_factor"})
_PUBLIC_PREF_FIELDS = (
    _PUBLIC_PREF_STRING_FIELDS | _PUBLIC_PREF_INTEGER_FIELDS | _PUBLIC_PREF_NUMBER_FIELDS
)
_MESSAGE_EVENT_FIELDS = frozenset(
    {
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
    }
)
_CONTACT_EVENT_FIELDS = frozenset(
    {
        "public_key",
        "name",
        "adv_type",
        "flags",
        "favorite",
        "out_path_len",
        "last_advert_timestamp",
        "lastmod",
        "gps_lat",
        "gps_lon",
        "change",
    }
)
_SNAPSHOT_CONTACT_FIELDS = _CONTACT_EVENT_FIELDS.difference({"change"})
_CHANNEL_EVENT_FIELDS = frozenset({"index", "name", "change"})
_SNAPSHOT_CHANNEL_FIELDS = frozenset({"index", "name"})
_MESSAGE_RECEPTION_EVENT_FIELDS = frozenset(
    {
        "message_id",
        "packet_hash",
        "path",
        "rssi",
        "snr",
        "observed_at",
        "observation_count",
        "unique_path_count",
    }
)
_MESSAGE_SEND_STATE_EVENT_FIELDS = frozenset(
    {
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
    }
)
_RF_RECEPTION_EVENT_FIELDS = frozenset({"packet_hash", "rssi", "snr", "path", "observed_at"})
_PACKET_HASH_EVENT_TYPES = frozenset(
    {
        "message",
        "message_reception",
        "message_send_state",
        "rf_reception",
    }
)
_KNOWN_EVENT_FIELDS = {
    "message": _MESSAGE_EVENT_FIELDS,
    "contact": _CONTACT_EVENT_FIELDS,
    "channel": _CHANNEL_EVENT_FIELDS,
    "prefs": _PUBLIC_PREF_FIELDS,
    "message_reception": _MESSAGE_RECEPTION_EVENT_FIELDS,
    "message_send_state": _MESSAGE_SEND_STATE_EVENT_FIELDS,
    "rf_reception": _RF_RECEPTION_EVENT_FIELDS,
}


def _journal_epoch(value: object) -> Optional[str]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _CURSOR_MAX_BYTES
        or any(character not in "0123456789abcdef" for character in value)
    ):
        return None
    return value


def _parse_cursor(
    value: object,
    *,
    allow_legacy: bool = False,
) -> Optional[tuple[Optional[str], int]]:
    """Parse one bounded server cursor without unbounded integer conversion."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _CURSOR_MAX_BYTES:
        return None

    raw_epoch, separator, raw_seq = value.rpartition(":")
    if separator:
        epoch = _journal_epoch(raw_epoch)
        if epoch is None:
            return None
    elif allow_legacy:
        epoch, raw_seq = None, value
    else:
        return None

    if not raw_seq or any(character not in "0123456789" for character in raw_seq):
        return None
    sequence = int(raw_seq)
    if sequence > _SQLITE_ROW_ID_MAX:
        return None
    return epoch, sequence


def _validate_include(value: Optional[str]) -> None:
    """Reject mistyped event selectors before issuing a request."""

    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("include must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("include must be valid UTF-8") from None
    if len(encoded) > 128:
        raise ValueError("include must not exceed 128 UTF-8 bytes")
    tokens = [token.strip() for token in value.split(",")]
    if (
        not tokens
        or any(not token for token in tokens)
        or any(token != "rf_receptions" for token in tokens)
    ):
        raise ValueError("include may contain only rf_receptions")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer token or request body to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


# Companion/admin bearer tokens go directly to the configured repeater. Do
# not let ambient HTTP_PROXY settings silently route them through another
# process; callers that intentionally need a proxy should put an authenticated
# reverse proxy in ``base_url``.
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler,
)


def _path_segment(value: object) -> str:
    """Encode one value as exactly one URL path segment."""

    raw = str(value)
    if raw in (".", ".."):
        # urllib intentionally leaves RFC-unreserved dots alone. Encode an
        # all-dot identifier explicitly so it cannot be normalized as a path
        # traversal segment before application routing.
        return "".join("%2E" for _character in raw)
    return urllib.parse.quote(raw, safe="")


def _read_bounded(response, limit: int = _MAX_RESPONSE_BYTES) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        headers = _Headers(dict(getattr(response, "headers", {}) or {}))
        raise RestError(
            502,
            {"error": "response exceeds client limit"},
            response.geturl(),
            headers=headers,
        )
    return raw


def _bearer_header(token: Optional[str]) -> Optional[str]:
    if token is None or token == "":
        return None
    if (
        not isinstance(token, str)
        or len(token) > _BEARER_TOKEN_MAX_CHARS
        or token != token.strip()
        or _BEARER_TOKEN_RE.fullmatch(token) is None
    ):
        raise ValueError(
            "token must contain only RFC 6750 bearer-token characters and no whitespace or controls"
        )
    return f"Bearer {token}"


def strict_json_loads(raw: Any) -> Any:
    """Decode one strict JSON value for reference-client network boundaries."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    def reject_duplicate_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON field {key}")
            value[key] = item
        return value

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number {value}")
        return parsed

    return json.loads(
        raw,
        object_pairs_hook=reject_duplicate_fields,
        parse_constant=reject_constant,
        parse_float=parse_finite_float,
    )


_json_loads = strict_json_loads


def _is_finite_wire_number(value: Any) -> bool:
    """Return whether one decoded JSON value is a finite, non-boolean number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


class _Headers(dict):
    """Case-insensitive response headers.

    HTTP header names are case-insensitive (RFC 7230 §3.2), and servers differ
    on casing: the spec documents ``ETag`` while CherryPy puts ``Etag`` on the
    wire. A case-sensitive lookup silently returns None and the caller
    quietly loses conditional-request support.
    """

    def __init__(self, raw) -> None:
        super().__init__(raw)
        self._lower = {k.lower(): v for k, v in raw.items()}

    def get(self, key, default=None):
        return self._lower.get(key.lower(), default)

    def __getitem__(self, key):
        return self._lower[key.lower()]

    def __contains__(self, key) -> bool:
        return key.lower() in self._lower


class RestError(Exception):
    """Non-2xx response. Carries the v1 error envelope when there is one."""

    def __init__(
        self,
        status: int,
        body: Any,
        url: str,
        *,
        headers: Optional[dict] = None,
    ) -> None:
        detail = body
        if isinstance(body, dict):
            detail = body.get("error") or body.get("message") or body
        super().__init__(f"{status} from {url}: {detail}")
        self.status = status
        self.body = body
        self.url = url
        self.headers = _Headers(headers or {})

    @property
    def data(self) -> Any:
        """Structured v1 error data, including an indeterminate send record."""

        return self.body.get("data") if isinstance(self.body, dict) else None


class NotModified(Exception):
    """304 -- the ETag matched, so there is no body to read."""


class PairingIdentityMismatch(Exception):
    """The out-of-band companion fingerprint did not match the pair result."""

    def __init__(self, expected: str, presented: str) -> None:
        super().__init__("paired companion fingerprint differs from the trusted pairing value")
        self.expected = expected
        self.presented = presented


@dataclass
class SyncResult:
    events: list
    next_cursor: str
    has_more: bool
    snapshot_required: bool = False
    journal_epoch: Optional[str] = None
    reset_reason: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SSEEvent:
    """One event from ``GET .../events``."""

    event: str
    data: dict
    event_id: Optional[str] = None


class CompanionRestClient:
    """Talks to one repeater's ``/api/v1`` tree.

    ``token`` is a device token from :meth:`pair` (or an admin API token for
    operator-level calls). It is sent as ``Authorization: Bearer``.
    """

    def __init__(self, base_url: str, token: Optional[str] = None, *, timeout: float = 30.0):
        parsed = urllib.parse.urlsplit(base_url)
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("base_url has an invalid port") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or not parsed.hostname
            or parsed_port is not None
            and not 1 <= parsed_port <= 65_535
        ):
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        self.base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self.token = token
        if isinstance(timeout, bool):
            raise ValueError("timeout must be a finite number greater than zero")
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout must be a finite number greater than zero") from None
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be a finite number greater than zero")

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        headers: Optional[dict] = None,
        auth: bool = True,
        auth_token: Optional[str] = None,
    ) -> tuple[int, Any, dict]:
        url = f"{self.base_url}/api/v1{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        data = (
            json.dumps(
                body,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if body is not None
            else None
        )
        request_headers = {"Accept": "application/json"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        token = auth_token if auth_token is not None else self.token
        authorization = _bearer_header(token) if auth else None
        if authorization is not None:
            request_headers["Authorization"] = authorization
        request_headers.update(headers or {})

        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                response_headers = _Headers(dict(response.headers))
                content_type = str(response_headers.get("Content-Type", "") or "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise RestError(
                        502,
                        {"error": (f"server returned unexpected Content-Type {content_type!r}")},
                        url,
                        headers=response_headers,
                    )
                raw = _read_bounded(response)
                try:
                    parsed = _json_loads(raw) if raw else None
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    raise RestError(
                        502,
                        {"error": "server returned invalid JSON"},
                        url,
                        headers=response_headers,
                    ) from exc
                return response.status, parsed, response_headers
        except urllib.error.HTTPError as exc:
            with exc:
                raw = _read_bounded(exc)
                if exc.code == 304:
                    raise NotModified() from exc
                response_headers = _Headers(dict(exc.headers or {}))
                try:
                    parsed = _json_loads(raw) if raw else None
                except (UnicodeDecodeError, ValueError, RecursionError):
                    parsed = raw.decode("utf-8", errors="replace")
                raise RestError(
                    exc.code,
                    parsed,
                    url,
                    headers=response_headers,
                ) from exc

    def _data(self, method: str, path: str, **kwargs) -> Any:
        """Unwrap the ``{success, data}`` envelope the v1 tree returns."""
        _status, payload, _headers = self._request(method, path, **kwargs)
        return self._success_data(payload, path)

    def _success_data(self, payload: Any, path: str) -> Any:
        """Return data only from an explicit v1 success envelope."""
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or "data" not in payload
        ):
            raise RestError(
                502,
                {"error": "server returned an invalid success envelope"},
                f"{self.base_url}/api/v1{path}",
            )
        return payload["data"]

    def _wire_error(self, path: str, message: str) -> RestError:
        return RestError(
            502,
            {"error": message},
            f"{self.base_url}/api/v1{path}",
        )

    def _object_data(self, method: str, path: str, **kwargs) -> dict:
        data = self._data(method, path, **kwargs)
        if not isinstance(data, dict):
            raise self._wire_error(path, "response data must be an object")
        return data

    def _list_data(self, method: str, path: str, **kwargs) -> list:
        data = self._data(method, path, **kwargs)
        if not isinstance(data, list):
            raise self._wire_error(path, "response data must be an array")
        return data

    def _validate_identity_fingerprint(self, data: dict, path: str) -> str:
        identity = data.get("companion_identity")
        fingerprint = data.get("fingerprint")
        if not isinstance(identity, str) or _HEX64_RE.fullmatch(identity) is None:
            raise self._wire_error(
                path,
                "response has invalid companion_identity",
            )
        if not isinstance(fingerprint, str) or _HEX64_RE.fullmatch(fingerprint) is None:
            raise self._wire_error(
                path,
                "response has invalid identity fingerprint",
            )
        computed = hashlib.sha256(bytes.fromhex(identity)).hexdigest()
        if not hmac.compare_digest(computed, fingerprint):
            raise self._wire_error(
                path,
                "response identity and fingerprint disagree",
            )
        return computed

    def _validate_journal_event(self, value: Any, path: str) -> dict:
        if not isinstance(value, dict):
            raise self._wire_error(path, "journal event must be an object")
        if any(key not in value for key in ("seq", "type", "ts", "packet_hash", "data")):
            raise self._wire_error(path, "journal event is incomplete")
        seq = value.get("seq")
        event_type = value.get("type")
        timestamp = value.get("ts")
        packet_hash = value.get("packet_hash")
        data = value.get("data")
        if type(seq) is not int or not 1 <= seq <= _SQLITE_ROW_ID_MAX:
            raise self._wire_error(path, "journal event has invalid seq")
        if not isinstance(event_type, str) or _EVENT_TYPE_RE.fullmatch(event_type) is None:
            raise self._wire_error(path, "journal event has invalid type")
        if event_type == "snapshot_required":
            raise self._wire_error(path, "snapshot_required is reserved for SSE reset control")
        if not _is_finite_wire_number(timestamp):
            raise self._wire_error(path, "journal event has invalid ts")
        if packet_hash is not None and (
            not isinstance(packet_hash, str) or _HASH16_RE.fullmatch(packet_hash) is None
        ):
            raise self._wire_error(path, "journal event has invalid packet_hash")
        if not isinstance(data, dict):
            raise self._wire_error(path, "journal event data must be an object")
        allowed_fields = _KNOWN_EVENT_FIELDS.get(event_type)
        if allowed_fields is not None and not set(data).issubset(allowed_fields):
            raise self._wire_error(
                path,
                f"{event_type} event contains unknown fields",
            )
        if event_type == "channel":
            if (
                set(data) != _CHANNEL_EVENT_FIELDS
                or type(data.get("index")) is not int
                or data["index"] < 0
                or data.get("change") not in ("update", "remove")
                or (data.get("change") == "remove" and data.get("name") is not None)
                or (data.get("change") == "update" and not isinstance(data.get("name"), str))
            ):
                raise self._wire_error(path, "channel event is invalid")
        elif event_type == "contact":
            self._validate_contact(data, path, event=True)
        elif event_type == "prefs":
            if not data:
                raise self._wire_error(path, "prefs event is empty")
            self._validate_public_prefs(data, path, complete=False)
        elif event_type == "message":
            self._validate_message_event(data, path, complete=True)
        elif event_type == "message_reception":
            self._validate_reception_event(data, path)
        elif event_type == "message_send_state":
            self._validate_send_state_event(data, path)
        elif event_type == "rf_reception":
            self._validate_rf_reception_event(data, path)
        if event_type in _PACKET_HASH_EVENT_TYPES and data["packet_hash"] != packet_hash:
            raise self._wire_error(
                path,
                "journal event packet_hash does not match data.packet_hash",
            )
        return value

    def _validate_contact(
        self,
        data: dict,
        path: str,
        *,
        event: bool,
    ) -> None:
        expected_fields = _CONTACT_EVENT_FIELDS if event else _SNAPSHOT_CONTACT_FIELDS
        if set(data) != expected_fields:
            raise self._wire_error(path, "contact is incomplete or contains unknown fields")
        public_key = data["public_key"]
        if not isinstance(public_key, str) or _HEX64_RE.fullmatch(public_key) is None:
            raise self._wire_error(path, "contact public_key is invalid")
        if not isinstance(data["name"], str):
            raise self._wire_error(path, "contact name is invalid")
        for field_name in (
            "adv_type",
            "flags",
            "out_path_len",
            "last_advert_timestamp",
            "lastmod",
        ):
            if type(data[field_name]) is not int:
                raise self._wire_error(path, f"contact {field_name} is invalid")
        if type(data["favorite"]) is not bool:
            raise self._wire_error(path, "contact favorite is invalid")
        if data["favorite"] != bool(data["flags"] & 0x01):
            raise self._wire_error(path, "contact favorite does not match flags")
        for field_name in ("gps_lat", "gps_lon"):
            if data[field_name] is not None and not _is_finite_wire_number(data[field_name]):
                raise self._wire_error(path, f"contact {field_name} is invalid")
        if event and data["change"] not in ("new", "update", "remove", "path"):
            raise self._wire_error(path, "contact change is invalid")

    def _validate_message_event(
        self,
        data: dict,
        path: str,
        *,
        complete: bool = False,
    ) -> None:
        """Validate fields present in a projected message journal payload."""

        if complete and set(data) != _MESSAGE_EVENT_FIELDS:
            raise self._wire_error(path, "message is incomplete or contains unknown fields")
        integer_ranges = {
            "id": (1, _SQLITE_ROW_ID_MAX),
            "txt_type": (0, 0x3F),
            "timestamp": (0, _UINT32_MAX),
            "channel_idx": (0, 0xFF),
            "path_len": (0, 0xFF),
            "channel_data_type": (0, 0xFFFF),
            "observation_count": (0, _SQLITE_ROW_ID_MAX),
            "unique_path_count": (0, _SQLITE_ROW_ID_MAX),
            "rssi": (-_SQLITE_ROW_ID_MAX, _SQLITE_ROW_ID_MAX),
        }
        for field_name, (minimum, maximum) in integer_ranges.items():
            if field_name in data and (
                type(data[field_name]) is not int or not minimum <= data[field_name] <= maximum
            ):
                raise self._wire_error(
                    path,
                    f"message event {field_name} is invalid",
                )
        if "is_channel" in data and type(data["is_channel"]) is not bool:
            raise self._wire_error(path, "message event is_channel is invalid")
        for field_name in ("text",):
            if field_name in data and not isinstance(data[field_name], str):
                raise self._wire_error(
                    path,
                    f"message event {field_name} is invalid",
                )
        if "companion_hash" in data and (
            not isinstance(data["companion_hash"], str)
            or re.fullmatch(r"0x[0-9a-f]{2}", data["companion_hash"]) is None
        ):
            raise self._wire_error(path, "message event companion_hash is invalid")
        for field_name in ("sender_key", "recipient_key"):
            field_value = data.get(field_name)
            if field_name in data and (
                not isinstance(field_value, str)
                or (field_value != "" and _HEX64_RE.fullmatch(field_value) is None)
            ):
                raise self._wire_error(
                    path,
                    f"message event {field_name} is invalid",
                )
        for field_name in ("sender_prefix", "channel_data_payload"):
            field_value = data.get(field_name)
            if field_name in data and (
                not isinstance(field_value, str) or re.fullmatch(r"[0-9a-f]*", field_value) is None
            ):
                raise self._wire_error(
                    path,
                    f"message event {field_name} is invalid",
                )
        if "sender_prefix" in data and len(data["sender_prefix"]) not in {0, 8}:
            raise self._wire_error(path, "message event sender_prefix is invalid")
        for field_name in ("snr", "created_at"):
            if field_name in data and not _is_finite_wire_number(data[field_name]):
                raise self._wire_error(
                    path,
                    f"message event {field_name} is invalid",
                )
        if (
            "packet_hash" in data
            and data["packet_hash"] is not None
            and (
                not isinstance(data["packet_hash"], str)
                or _HASH16_RE.fullmatch(data["packet_hash"]) is None
            )
        ):
            raise self._wire_error(path, "message event packet_hash is invalid")
        if "direction" in data and data["direction"] not in {"in", "out"}:
            raise self._wire_error(path, "message event direction is invalid")
        if "state" in data and data["state"] not in {
            "received",
            "pending",
            "transmitted",
            "heard_repeated",
            "confirmed",
            "failed",
            "indeterminate",
        }:
            raise self._wire_error(path, "message event state is invalid")
        if (
            "expected_ack" in data
            and data["expected_ack"] is not None
            and (
                type(data["expected_ack"]) is not int
                or not 0 <= data["expected_ack"] <= _UINT32_MAX
            )
        ):
            raise self._wire_error(path, "message event expected_ack is invalid")
        if "source" in data and data["source"] not in {
            None,
            "radio",
            "rest",
            "frame",
            "operator",
        }:
            raise self._wire_error(path, "message event source is invalid")
        if (
            "observation_count" in data
            and "unique_path_count" in data
            and data["unique_path_count"] > data["observation_count"]
        ):
            raise self._wire_error(path, "message event counters are invalid")
        direction = data.get("direction")
        state = data.get("state")
        source = data.get("source")
        if direction == "in" and "state" in data and state != "received":
            raise self._wire_error(path, "inbound message event state is invalid")
        if direction == "out" and state == "received":
            raise self._wire_error(path, "outbound message event state is invalid")
        if direction == "in" and "source" in data and source not in {None, "radio"}:
            raise self._wire_error(path, "inbound message event source is invalid")
        if direction == "out" and "source" in data and source not in {"rest", "frame", "operator"}:
            raise self._wire_error(path, "outbound message event source is invalid")

    def _validate_reception_event(self, data: dict, path: str) -> None:
        if set(data) != _MESSAGE_RECEPTION_EVENT_FIELDS:
            raise self._wire_error(path, "message_reception event is incomplete")
        self._validate_rf_fields(data, path, require_counters=True)

    def _validate_send_state_event(self, data: dict, path: str) -> None:
        lifecycle_fields = frozenset(
            {
                "message_id",
                "state",
                "packet_hash",
                "expected_ack",
            }
        )
        heard_repeat_fields = frozenset(
            {
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
        )
        fields = frozenset(data)
        if fields not in {lifecycle_fields, heard_repeat_fields}:
            raise self._wire_error(path, "message_send_state event has an invalid shape")
        message_id = data["message_id"]
        if type(message_id) is not int or not 1 <= message_id <= _SQLITE_ROW_ID_MAX:
            raise self._wire_error(path, "message_send_state message_id is invalid")
        if data["state"] not in {
            "pending",
            "transmitted",
            "heard_repeated",
            "confirmed",
            "failed",
            "indeterminate",
        }:
            raise self._wire_error(path, "message_send_state state is invalid")
        payload_hash = data["packet_hash"]
        if payload_hash is not None and (
            not isinstance(payload_hash, str) or _HASH16_RE.fullmatch(payload_hash) is None
        ):
            raise self._wire_error(path, "message_send_state packet_hash is invalid")
        if (
            "expected_ack" in data
            and data["expected_ack"] is not None
            and (
                type(data["expected_ack"]) is not int
                or not 0 <= data["expected_ack"] <= _UINT32_MAX
            )
        ):
            raise self._wire_error(path, "message_send_state expected_ack is invalid")
        if fields == heard_repeat_fields:
            self._validate_rf_fields(data, path, send_state=True)

    def _validate_rf_reception_event(self, data: dict, path: str) -> None:
        if set(data) != _RF_RECEPTION_EVENT_FIELDS:
            raise self._wire_error(path, "rf_reception event is incomplete")
        self._validate_rf_fields(data, path)

    def _validate_rf_fields(
        self,
        data: dict,
        path: str,
        *,
        require_counters: bool = False,
        send_state: bool = False,
    ) -> None:
        message_id = data.get("message_id")
        if require_counters and (
            type(message_id) is not int or not 1 <= message_id <= _SQLITE_ROW_ID_MAX
        ):
            raise self._wire_error(path, "RF event message_id is invalid")
        payload_hash = data.get("packet_hash")
        if not isinstance(payload_hash, str) or _HASH16_RE.fullmatch(payload_hash) is None:
            raise self._wire_error(path, "RF event packet_hash is invalid")
        raw_path = data.get("path")
        if not isinstance(raw_path, list) or not all(
            isinstance(hop, str) and hop for hop in raw_path
        ):
            raise self._wire_error(path, "RF event path is invalid")
        for field_name in ("rssi", "snr"):
            if data.get(field_name) is not None and not _is_finite_wire_number(data[field_name]):
                raise self._wire_error(
                    path,
                    f"RF event {field_name} is invalid",
                )
        if not _is_finite_wire_number(data.get("observed_at")):
            raise self._wire_error(path, "RF event observed_at is invalid")
        counter_fields = (
            ("heard_repeat_count", "unique_repeater_count")
            if send_state
            else ("observation_count", "unique_path_count")
        )
        if require_counters or send_state:
            for field_name in counter_fields:
                if type(data.get(field_name)) is not int or data[field_name] < 0:
                    raise self._wire_error(
                        path,
                        f"RF event {field_name} is invalid",
                    )
            if data[counter_fields[1]] > data[counter_fields[0]]:
                raise self._wire_error(path, "RF event counters are invalid")
        if send_state:
            terminal = data.get("terminal_repeater_hash")
            if terminal is not None and (not isinstance(terminal, str) or not terminal):
                raise self._wire_error(
                    path,
                    "message_send_state terminal_repeater_hash is invalid",
                )

    def _validate_public_prefs(
        self,
        value: dict,
        path: str,
        *,
        complete: bool,
    ) -> None:
        if complete and any(field not in value for field in _PUBLIC_PREF_FIELDS):
            raise self._wire_error(path, "public preferences are incomplete")
        for pref_field in _PUBLIC_PREF_STRING_FIELDS:
            if pref_field in value and not isinstance(value[pref_field], str):
                raise self._wire_error(
                    path,
                    f"public preference {pref_field} is invalid",
                )
        for pref_field in _PUBLIC_PREF_INTEGER_FIELDS:
            if pref_field in value and type(value[pref_field]) is not int:
                raise self._wire_error(
                    path,
                    f"public preference {pref_field} is invalid",
                )
        for pref_field in _PUBLIC_PREF_NUMBER_FIELDS:
            field_value = value.get(pref_field)
            if pref_field in value and not _is_finite_wire_number(field_value):
                raise self._wire_error(
                    path,
                    f"public preference {pref_field} is invalid",
                )

    def _validate_snapshot_required_event(self, data: dict, path: str) -> None:
        epoch = data.get("journal_epoch")
        cursor = data.get("cursor")
        reason = data.get("reset_reason")
        parsed_cursor = _parse_cursor(cursor)
        if (
            _journal_epoch(epoch) is None
            or data.get("snapshot_required") is not True
            or reason not in _RESET_REASONS
        ):
            raise self._wire_error(path, "invalid snapshot_required event")
        if parsed_cursor is None or parsed_cursor[0] != epoch:
            raise self._wire_error(path, "invalid snapshot_required cursor")

    # -- unauthenticated ---------------------------------------------------

    def server_info(self) -> dict:
        """Public bootstrap document. Deliberately excludes companion names."""
        path = "/server_info"
        data = self._object_data("GET", path, auth=False)
        transport = data.get("transport")
        server = data.get("server")
        if (
            not isinstance(data.get("site_name"), str)
            or not isinstance(data.get("api_versions"), list)
            or "v1" not in data["api_versions"]
            or not all(isinstance(version, str) for version in data["api_versions"])
            or not isinstance(data.get("auth_modes"), list)
            or not all(isinstance(mode, str) for mode in data["auth_modes"])
            or not isinstance(transport, dict)
            or transport.get("scheme") not in ("http", "https")
            or type(transport.get("secure")) is not bool
            or type(transport.get("trusted_network_required")) is not bool
            or transport["secure"] is not (transport["scheme"] == "https")
            or transport["trusted_network_required"] is not (transport["scheme"] != "https")
            or not isinstance(server, dict)
            or (server.get("version") is not None and not isinstance(server.get("version"), str))
            or not _is_finite_wire_number(server.get("time"))
        ):
            raise self._wire_error(path, "server_info response is invalid")
        return data

    # -- pairing -----------------------------------------------------------

    def pair_start(self, companion_name: str, admin_token: str) -> dict:
        """Operator-side: mint a short-lived pairing code.

        Needs admin auth -- a device cannot bootstrap itself.
        """
        if _bearer_header(admin_token) is None:
            raise ValueError("admin_token is required to start pairing")
        path = "/pair/start"
        data = self._object_data(
            "POST",
            path,
            body={"companion_name": companion_name},
            auth_token=admin_token,
        )
        if data.get("companion_name") != companion_name:
            raise self._wire_error(path, "pair response has the wrong companion_name")
        code = data.get("code")
        if not isinstance(code, str) or _PAIR_CODE_RE.fullmatch(code) is None:
            raise self._wire_error(path, "pair response has an invalid code")
        expires_in = data.get("expires_in")
        if type(expires_in) is not int or expires_in <= 0:
            raise self._wire_error(path, "pair response has an invalid expires_in")
        self._validate_identity_fingerprint(data, path)
        return data

    def pair(
        self,
        code: str,
        device_id: str,
        name: str,
        platform: Optional[str] = None,
        *,
        expected_fingerprint: str,
    ) -> dict:
        """Device-side: exchange a pairing code for a device token.

        The code is single-use. On success the returned token is adopted as
        this client's credential.

        Pass the fingerprint received through the trusted pairing channel
        (for example, the QR code) as ``expected_fingerprint``. The comparison
        happens before the token is adopted. This detects an unexpected
        companion identity; it does not authenticate a plaintext HTTP server.
        Use TLS or a trusted network for transport security.

        ``device_id`` is globally unique on one repeater. A client pairing
        with multiple companion identities must derive a distinct stable ID
        for each identity; reusing one returns HTTP 409.
        """
        if not isinstance(expected_fingerprint, str):
            raise ValueError("expected_fingerprint must be 64 hexadecimal characters")
        expected = expected_fingerprint.strip().lower()
        if len(expected) != 64:
            raise ValueError("expected_fingerprint must be 64 hexadecimal characters")
        try:
            expected_bytes = bytes.fromhex(expected)
        except ValueError as exc:
            raise ValueError("expected_fingerprint must be 64 hexadecimal characters") from exc
        if len(expected_bytes) != 32 or expected_bytes.hex() != expected:
            raise ValueError("expected_fingerprint must be 64 hexadecimal characters")

        body = {"code": code, "device_id": device_id, "name": name}
        if platform is not None:
            body["platform"] = platform
        path = "/pair"
        data = self._object_data("POST", path, body=body, auth=False)
        computed = self._validate_identity_fingerprint(data, path)
        if not hmac.compare_digest(expected, computed):
            raise PairingIdentityMismatch(expected, computed)

        token = data.get("token")
        try:
            usable_token = _bearer_header(token)
        except ValueError as exc:
            raise self._wire_error(path, "pair response has no usable token") from exc
        if usable_token is None:
            raise self._wire_error(path, "pair response has no usable token")
        returned_device_id = data.get("device_id")
        returned_name = data.get("companion_name")
        if returned_device_id != device_id:
            raise self._wire_error(path, "pair response has the wrong device_id")
        if not isinstance(returned_name, str) or not returned_name:
            raise self._wire_error(path, "pair response has invalid companion_name")
        if data.get("scope") != f"companion:{returned_name}":
            raise self._wire_error(path, "pair response has invalid scope")
        self.token = token
        return data

    # -- companions --------------------------------------------------------

    def companions(self) -> list:
        path = "/companions"
        data = self._list_data("GET", path)
        for item in data:
            if not isinstance(item, dict):
                raise self._wire_error(path, "companion entry must be an object")
            required = ("name", "companion_hash", "node_name", "public_key")
            if any(key not in item for key in required):
                raise self._wire_error(path, "companion entry is incomplete")
            if (
                not isinstance(item["name"], str)
                or not item["name"]
                or not isinstance(item["node_name"], str)
                or not isinstance(item["companion_hash"], str)
                or re.fullmatch(r"^0x[0-9a-f]{2}$", item["companion_hash"]) is None
                or not isinstance(item["public_key"], str)
                or _HEX64_RE.fullmatch(item["public_key"]) is None
            ):
                raise self._wire_error(path, "companion entry is invalid")
        return data

    def snapshot(
        self,
        companion_name: str,
        *,
        messages_limit: Optional[int] = None,
        etag: Optional[str] = None,
    ) -> tuple[dict, Optional[str]]:
        """Bootstrap document: self, contacts, channels, recent messages.

        This is the only place channels and contacts are handed out in full --
        there is no dedicated list endpoint for either. Returns
        ``(data, etag)``; pass the etag back to get :class:`NotModified`.
        """
        path = f"/companions/{_path_segment(companion_name)}/snapshot"
        headers = {"If-None-Match": etag} if etag else None
        _status, payload, response_headers = self._request(
            "GET",
            path,
            params={"messages_limit": messages_limit},
            headers=headers,
        )
        data = self._success_data(payload, path)
        if not isinstance(data, dict):
            raise RestError(
                502,
                {"error": "snapshot data must be an object"},
                f"{self.base_url}/api/v1{path}",
            )
        required = (
            "journal_epoch",
            "cursor",
            "self",
            "contacts",
            "channels",
            "messages",
            "server",
        )
        if any(key not in data for key in required):
            raise self._wire_error(path, "snapshot data is incomplete")
        epoch = data["journal_epoch"]
        cursor = data["cursor"]
        parsed_cursor = _parse_cursor(cursor)
        if _journal_epoch(epoch) is None or parsed_cursor is None or parsed_cursor[0] != epoch:
            raise self._wire_error(path, "snapshot has an invalid cursor")
        if not isinstance(data["self"], dict) or not isinstance(data["server"], dict):
            raise self._wire_error(path, "snapshot metadata must be objects")
        self_info = data["self"]
        if set(self_info) != {"public_key", *_PUBLIC_PREF_FIELDS}:
            raise self._wire_error(
                path,
                "snapshot public preferences are incomplete or contain unknown fields",
            )
        if (
            not isinstance(self_info.get("public_key"), str)
            or _HEX64_RE.fullmatch(self_info["public_key"]) is None
        ):
            raise self._wire_error(path, "snapshot self identity is invalid")
        self._validate_public_prefs(self_info, path, complete=True)
        for collection_name in ("contacts", "channels", "messages"):
            if not isinstance(data[collection_name], list) or not all(
                isinstance(item, dict) for item in data[collection_name]
            ):
                raise self._wire_error(
                    path,
                    f"snapshot {collection_name} must be an array of objects",
                )
        for contact in data["contacts"]:
            self._validate_contact(contact, path, event=False)
        for channel in data["channels"]:
            if (
                set(channel) != _SNAPSHOT_CHANNEL_FIELDS
                or type(channel.get("index")) is not int
                or channel["index"] < 0
                or not isinstance(channel.get("name"), str)
            ):
                raise self._wire_error(path, "snapshot channel is invalid")
        for message in data["messages"]:
            self._validate_message_event(message, path, complete=True)
        return data, response_headers.get("ETag")

    def sync(
        self,
        companion_name: str,
        cursor: str,
        *,
        limit: Optional[int] = None,
        include: Optional[str] = None,
    ) -> SyncResult:
        """Journal delta since ``cursor``.

        ``rf_reception`` events are omitted unless ``include='rf_receptions'``;
        every other event type -- including ``channel`` -- comes through by
        default.

        A cursor below the prune floor yields ``snapshot_required``: the delta
        would be silently incomplete, so the client must re-snapshot.
        """
        path = f"/companions/{_path_segment(companion_name)}/sync"
        _validate_include(include)
        supplied_cursor = _parse_cursor(cursor, allow_legacy=True)
        if supplied_cursor is None:
            raise ValueError(
                "cursor must be at most 128 ASCII bytes in epoch:sequence or legacy decimal form"
            )
        _status, payload, _response_headers = self._request(
            "GET",
            path,
            params={"cursor": cursor, "limit": limit, "include": include},
        )
        data = self._success_data(payload, path)
        if not isinstance(data, dict):
            raise RestError(
                502,
                {"error": "sync data must be an object"},
                f"{self.base_url}/api/v1{path}",
            )
        required = (
            "journal_epoch",
            "events",
            "next_cursor",
            "has_more",
            "snapshot_required",
        )
        if any(key not in data for key in required):
            raise self._wire_error(path, "sync data is incomplete")
        epoch = data["journal_epoch"]
        events = data["events"]
        next_cursor = data["next_cursor"]
        has_more = data["has_more"]
        snapshot_required = data["snapshot_required"]
        if _journal_epoch(epoch) is None:
            raise self._wire_error(path, "sync has an invalid journal_epoch")
        parsed_next_cursor = _parse_cursor(next_cursor)
        if parsed_next_cursor is None or parsed_next_cursor[0] != epoch:
            raise self._wire_error(path, "sync has an invalid next_cursor")
        if type(has_more) is not bool or type(snapshot_required) is not bool:
            raise self._wire_error(path, "sync flags must be booleans")
        if not isinstance(events, list):
            raise self._wire_error(path, "sync events must be an array")
        previous_seq = 0
        for event in events:
            self._validate_journal_event(event, path)
            if event["seq"] <= previous_seq:
                raise self._wire_error(path, "sync events are not strictly ordered")
            previous_seq = event["seq"]
        next_seq = parsed_next_cursor[1]
        if previous_seq > next_seq:
            raise self._wire_error(path, "sync cursor precedes its events")
        if snapshot_required:
            if events or has_more or data.get("reset_reason") not in _RESET_REASONS:
                raise self._wire_error(path, "invalid snapshot-required sync response")
        else:
            supplied_epoch, supplied_seq = supplied_cursor
            if supplied_epoch is not None and supplied_epoch != epoch:
                raise self._wire_error(path, "sync epoch changed without requiring a snapshot")
            if events and events[0]["seq"] <= supplied_seq:
                raise self._wire_error(path, "sync repeated an event at or before the cursor")
            if next_seq < supplied_seq or (has_more and next_seq == supplied_seq):
                raise self._wire_error(path, "sync cursor did not advance")
        return SyncResult(
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
            snapshot_required=snapshot_required,
            journal_epoch=epoch,
            reset_reason=data.get("reset_reason"),
            raw=data,
        )

    def messages(
        self,
        companion_name: str,
        *,
        limit: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> dict:
        path = f"/companions/{_path_segment(companion_name)}/messages"
        if before_id is not None and (
            type(before_id) is not int or not 1 <= before_id <= _SQLITE_ROW_ID_MAX
        ):
            raise ValueError(f"before_id must be between 1 and {_SQLITE_ROW_ID_MAX}")
        data = self._object_data(
            "GET",
            path,
            params={"limit": limit, "before_id": before_id},
        )
        messages = data.get("messages")
        if "next_before_id" not in data:
            raise self._wire_error(path, "message page is incomplete")
        next_before_id = data["next_before_id"]
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            raise self._wire_error(path, "message page has invalid messages")
        message_ids = [message.get("id") for message in messages]
        if any(
            type(message_id) is not int or not 1 <= message_id <= _SQLITE_ROW_ID_MAX
            for message_id in message_ids
        ):
            raise self._wire_error(path, "message page has invalid message ids")
        if any(earlier <= later for earlier, later in zip(message_ids, message_ids[1:])):
            raise self._wire_error(
                path,
                "message page ids are not strictly descending",
            )
        if before_id is not None and any(message_id >= before_id for message_id in message_ids):
            raise self._wire_error(
                path,
                "message page does not precede before_id",
            )
        expected_next_before_id = message_ids[-1] if message_ids else None
        if next_before_id != expected_next_before_id:
            raise self._wire_error(path, "message page has invalid next_before_id")
        return data

    @staticmethod
    def new_idempotency_key() -> str:
        """Return a fresh send key for the caller to persist with its draft."""

        return uuid.uuid4().hex

    def send_message(
        self,
        companion_name: str,
        text: str,
        *,
        channel_idx: Optional[int] = None,
        to: Optional[str] = None,
        txt_type: int = 0,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Send a channel message (``channel_idx``) or a DM (``to``, hex pubkey).

        ``Idempotency-Key`` is mandatory. The server reserves it before any
        durable message or RF work. A retry with the same key and request
        replays the recorded result without touching the radio; reusing it for
        another request is a 409.

        Generate a key with :meth:`new_idempotency_key`, persist it with the
        local draft, and pass it explicitly. The client intentionally refuses
        to hide an auto-generated key: if the HTTP result were lost, the
        caller could not safely replay that send. Never switch to a new key
        after an ``indeterminate`` result; the first send may already have
        reached the radio.

        Every accepted attempt has a durable ``message_id`` and ``state``.
        A transmitted result also carries ``packet_hash`` when the radio
        backend can provide one; later ``message_send_state`` events use the
        same identifiers. Completed/failed keys replay for at least 48 hours
        from first reservation. Reconcile a lost result within that window;
        indeterminate keys remain blocked rather than becoming reusable.
        """
        if (channel_idx is None) == (to is None):
            raise ValueError("exactly one of channel_idx or to is required")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key is required; generate and persist it before sending")
        if len(idempotency_key) > 128 or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in idempotency_key
        ):
            raise ValueError(
                "idempotency_key must contain 1 to 128 visible ASCII characters without whitespace"
            )

        body: dict = {"text": text}
        if channel_idx is not None:
            body["channel_idx"] = channel_idx
        else:
            body["to"] = to
            body["txt_type"] = txt_type

        path = f"/companions/{_path_segment(companion_name)}/messages"
        data = self._object_data(
            "POST",
            path,
            body=body,
            headers={"Idempotency-Key": idempotency_key},
        )
        message_id = data.get("message_id")
        sent = data.get("sent")
        state = data.get("state")
        if type(message_id) is not int or not 1 <= message_id <= _SQLITE_ROW_ID_MAX:
            raise self._wire_error(path, "send response has invalid message_id")
        if type(sent) is not bool or state not in ("transmitted", "failed"):
            raise self._wire_error(path, "send response has invalid state")
        if (state == "transmitted") is not sent:
            raise self._wire_error(path, "send response state disagrees with sent")
        reason = data.get("reason")
        if (state == "failed" and reason is None) or (
            reason is not None and (not isinstance(reason, str) or not reason)
        ):
            raise self._wire_error(path, "send response has an invalid failure reason")
        if to is not None and type(data.get("is_flood")) is not bool:
            raise self._wire_error(path, "send response has invalid is_flood")
        packet_hash = data.get("packet_hash")
        if packet_hash is not None and (
            not isinstance(packet_hash, str) or _HASH16_RE.fullmatch(packet_hash) is None
        ):
            raise self._wire_error(path, "send response has invalid packet_hash")
        expected_ack = data.get("expected_ack")
        if expected_ack is not None and (
            type(expected_ack) is not int or not 0 <= expected_ack <= _UINT32_MAX
        ):
            raise self._wire_error(path, "send response has invalid expected_ack")
        return data

    # -- contact actions and RF observations -------------------------------

    def login(self, companion_name: str, pubkey: str, password: str = "") -> dict:
        return self._object_data(
            "POST",
            f"/companions/{_path_segment(companion_name)}/contacts/{_path_segment(pubkey)}/login",
            body={"password": password},
        )

    def has_connection(self, companion_name: str, pubkey: str) -> dict:
        """Return whether this companion has a live login session with a contact."""

        path = (
            f"/companions/{_path_segment(companion_name)}/contacts/"
            f"{_path_segment(pubkey)}/connection"
        )
        data = self._object_data("GET", path)
        if type(data.get("connected")) is not bool:
            raise self._wire_error(path, "connection response has invalid connected flag")
        return data

    def logout(self, companion_name: str, pubkey: str) -> dict:
        """Clear a contact login session and send the remote logout once."""

        path = (
            f"/companions/{_path_segment(companion_name)}/contacts/{_path_segment(pubkey)}/logout"
        )
        data = self._object_data("POST", path, body={})
        if type(data.get("logged_out")) is not bool or type(data.get("sent")) is not bool:
            raise self._wire_error(path, "logout response has invalid flags")
        return data

    def status_request(self, companion_name: str, pubkey: str) -> dict:
        return self._object_data(
            "POST",
            f"/companions/{_path_segment(companion_name)}/contacts/"
            f"{_path_segment(pubkey)}/status_request",
            body={},
        )

    def telemetry_request(self, companion_name: str, pubkey: str) -> dict:
        return self._object_data(
            "POST",
            f"/companions/{_path_segment(companion_name)}/contacts/"
            f"{_path_segment(pubkey)}/telemetry_request",
            body={},
        )

    def reset_path(self, companion_name: str, pubkey: str) -> dict:
        path = (
            f"/companions/{_path_segment(companion_name)}/contacts/"
            f"{_path_segment(pubkey)}/reset_path"
        )
        data = self._object_data(
            "POST",
            path,
            body={},
        )
        if type(data.get("reset")) is not bool:
            raise self._wire_error(path, "reset response has invalid reset flag")
        return data

    def message_receptions(
        self,
        companion_name: str,
        message_id: int,
        *,
        window: Optional[str] = None,
    ) -> dict:
        return self._object_data(
            "GET",
            f"/companions/{_path_segment(companion_name)}/messages/"
            f"{_path_segment(message_id)}/receptions",
            params={"window": window},
        )

    def contact_paths(
        self,
        companion_name: str,
        pubkey: str,
        *,
        window: Optional[str] = None,
    ) -> dict:
        return self._object_data(
            "GET",
            f"/companions/{_path_segment(companion_name)}/contacts/{_path_segment(pubkey)}/paths",
            params={"window": window},
        )

    def transmission_repeats(
        self,
        companion_name: str,
        packet_hash: str,
        *,
        window: Optional[str] = None,
    ) -> dict:
        return self._object_data(
            "GET",
            f"/companions/{_path_segment(companion_name)}/transmissions/"
            f"{_path_segment(packet_hash)}/repeats",
            params={"window": window},
        )

    def events(
        self,
        companion_name: str,
        *,
        cursor: Optional[str] = None,
        include: Optional[str] = None,
        stream_timeout: Optional[float] = None,
    ) -> Iterator[SSEEvent]:
        """Yield resumable live events until the server closes the stream.

        This method does not reconnect automatically. Store each ``event_id``
        after applying the event, then pass it back as ``cursor`` on the next
        connection. That keeps retry policy in the chat client where network
        and app-lifecycle decisions belong. ``stream_timeout`` is a socket
        inactivity timeout; by default it is at least 65 seconds so it exceeds
        the server's default 15-second keepalive interval.
        """

        requested_cursor = None
        _validate_include(include)
        if cursor is not None:
            requested_cursor = _parse_cursor(cursor, allow_legacy=True)
            if requested_cursor is None:
                raise ValueError(
                    "cursor must be at most 128 ASCII bytes in epoch:sequence "
                    "or legacy decimal form"
                )
        path = f"/companions/{_path_segment(companion_name)}/events"
        url = f"{self.base_url}/api/v1{path}"
        params = {
            key: value
            for key, value in {"cursor": cursor, "include": include}.items()
            if value is not None
        }
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "text/event-stream"}
        authorization = _bearer_header(self.token)
        if authorization is not None:
            headers["Authorization"] = authorization
        request = urllib.request.Request(url, method="GET", headers=headers)
        if stream_timeout is None:
            read_timeout = max(self.timeout, 65.0)
        else:
            if isinstance(stream_timeout, bool):
                raise ValueError("stream_timeout must be a finite number greater than zero")
            try:
                read_timeout = float(stream_timeout)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    "stream_timeout must be a finite number greater than zero"
                ) from None
        if not math.isfinite(read_timeout) or read_timeout <= 0:
            raise ValueError("stream_timeout must be a finite number greater than zero")

        def generate() -> Iterator[SSEEvent]:
            event_name = "message"
            event_id: Optional[str] = None
            data_lines: list[str] = []
            event_data_bytes = 0
            event_data_lines = 0
            stream_epoch: Optional[str] = None
            stream_seq: Optional[int] = None
            try:
                response = _opener.open(request, timeout=read_timeout)
            except urllib.error.HTTPError as exc:
                with exc:
                    raw = _read_bounded(exc)
                    response_headers = _Headers(dict(exc.headers or {}))
                    try:
                        parsed = _json_loads(raw) if raw else None
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        parsed = raw.decode("utf-8", errors="replace")
                    raise RestError(
                        exc.code,
                        parsed,
                        url,
                        headers=response_headers,
                    ) from exc
            try:
                with response:
                    content_type = str(response.headers.get("Content-Type", "") or "")
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if media_type != "text/event-stream":
                        raise RestError(
                            502,
                            {
                                "error": (
                                    "event stream returned unexpected Content-Type "
                                    f"{content_type!r}"
                                )
                            },
                            url,
                        )
                    first_line = True
                    while True:
                        raw_line = response.readline(64 * 1024 + 1)
                        if not raw_line:
                            return
                        if len(raw_line) > 64 * 1024:
                            raise RestError(502, {"error": "SSE line too large"}, url)
                        try:
                            line = raw_line.decode("utf-8").rstrip("\r\n")
                        except UnicodeDecodeError as exc:
                            raise RestError(
                                502,
                                {"error": "SSE stream is not valid UTF-8"},
                                url,
                            ) from exc
                        if first_line:
                            line = line.removeprefix("\ufeff")
                            first_line = False
                        if not line:
                            if data_lines:
                                raw_data = "\n".join(data_lines)
                                try:
                                    data = _json_loads(raw_data)
                                except (ValueError, RecursionError) as exc:
                                    raise RestError(
                                        502,
                                        {"error": "invalid JSON in SSE event"},
                                        url,
                                    ) from exc
                                if not isinstance(data, dict):
                                    raise RestError(
                                        502,
                                        {"error": "SSE event data must be an object"},
                                        url,
                                    )
                                if event_name == "snapshot_required":
                                    if event_id is not None:
                                        raise self._wire_error(
                                            path,
                                            "snapshot_required event must not have an id",
                                        )
                                    self._validate_snapshot_required_event(data, path)
                                    yield SSEEvent(event_name, data, event_id)
                                    return
                                else:
                                    self._validate_journal_event(data, path)
                                    if data["type"] != event_name:
                                        raise self._wire_error(
                                            path,
                                            "SSE event name disagrees with its data",
                                        )
                                    parsed_event_id = _parse_cursor(event_id)
                                    if parsed_event_id is None or parsed_event_id[1] != data["seq"]:
                                        raise self._wire_error(
                                            path,
                                            "SSE event has an invalid id",
                                        )
                                    event_epoch = parsed_event_id[0]
                                    event_seq = parsed_event_id[1]
                                    if requested_cursor is not None and (
                                        (
                                            requested_cursor[0] is not None
                                            and event_epoch != requested_cursor[0]
                                        )
                                        or event_seq <= requested_cursor[1]
                                    ):
                                        raise self._wire_error(
                                            path,
                                            "SSE event does not follow the requested cursor",
                                        )
                                    if stream_epoch is not None and (
                                        event_epoch != stream_epoch
                                        or (stream_seq is not None and event_seq <= stream_seq)
                                    ):
                                        raise self._wire_error(
                                            path,
                                            "SSE event ids are not strictly ordered",
                                        )
                                    stream_epoch = event_epoch
                                    stream_seq = event_seq
                                    yield SSEEvent(event_name, data, event_id)
                            event_name = "message"
                            event_id = None
                            data_lines = []
                            event_data_bytes = 0
                            event_data_lines = 0
                            continue
                        if line.startswith(":"):
                            continue
                        sse_field, separator, value = line.partition(":")
                        if separator and value.startswith(" "):
                            value = value[1:]
                        if sse_field == "event":
                            event_name = value
                        elif sse_field == "id":
                            if "\x00" not in value:
                                event_id = value
                        elif sse_field == "data":
                            event_data_lines += 1
                            if event_data_lines > _MAX_SSE_DATA_LINES:
                                raise RestError(
                                    502,
                                    {"error": ("SSE event has too many data lines")},
                                    url,
                                )
                            event_data_bytes += len(value.encode("utf-8")) + 1
                            if event_data_bytes > _MAX_RESPONSE_BYTES:
                                raise RestError(
                                    502,
                                    {"error": "SSE event data exceeds client limit"},
                                    url,
                                )
                            data_lines.append(value)
            finally:
                response.close()

        return generate()

    # -- contacts and channels ---------------------------------------------

    def upsert_contact(
        self,
        companion_name: str,
        pubkey: str,
        *,
        name: Optional[str] = None,
        adv_type: Optional[int] = None,
        favorite: Optional[bool] = None,
        gps_lat: Optional[float] = None,
        gps_lon: Optional[float] = None,
    ) -> dict:
        """Add or update a contact.

        ``name`` is required when creating a new contact. ``adv_type`` then
        defaults to the normal chat-contact type. On update, omitted fields
        keep their current values. Adverts auto-add contacts already, so this
        mainly covers the ones auto-add filtered out (wrong type, too many
        hops). Learned paths and server-owned flag bits are intentionally not
        writable.
        """
        fields = {
            key: value
            for key, value in {
                "name": name,
                "adv_type": adv_type,
                "favorite": favorite,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
            }.items()
            if value is not None
        }
        path = f"/companions/{_path_segment(companion_name)}/contacts/{_path_segment(pubkey)}"
        data = self._object_data(
            "POST",
            path,
            body=fields,
        )
        if not isinstance(data.get("contact"), dict):
            raise self._wire_error(path, "contact response has invalid contact")
        return data

    def set_favorite(self, companion_name: str, pubkey: str, favorite: bool = True) -> dict:
        """Mark or unmark an existing contact as a favourite.

        Favourites are protected from forced-trim eviction when the contact
        store fills. This writes flags bit 0 server-side so the other bits
        (which are in active use) are preserved. Create a contact with
        :meth:`upsert_contact` and a human-readable name first.
        """
        return self.upsert_contact(companion_name, pubkey, favorite=favorite)

    def delete_contact(self, companion_name: str, pubkey: str) -> dict:
        path = f"/companions/{_path_segment(companion_name)}/contacts/{_path_segment(pubkey)}"
        data = self._object_data(
            "DELETE",
            path,
        )
        if type(data.get("removed")) is not bool:
            raise self._wire_error(path, "contact response has invalid removed flag")
        return data

    def set_channel(self, companion_name: str, index: int, name: str, secret: bytes) -> dict:
        """Join or rename a channel. ``secret`` is the PSK (16 or 32 bytes).

        Write-only: no v1 endpoint ever returns a channel secret, so the PSK
        must be known out of band. The response echoes only index and name.
        """
        if type(index) is not int or index < 0:
            raise ValueError("index must be a non-negative integer")
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        if not isinstance(secret, (bytes, bytearray)):
            raise ValueError("secret must be bytes")
        if len(secret) not in (16, 32):
            raise ValueError("secret must be exactly 16 or 32 bytes")
        path = f"/companions/{_path_segment(companion_name)}/channels/{_path_segment(index)}"
        data = self._object_data(
            "PUT",
            path,
            body={"name": name, "secret": bytes(secret).hex()},
        )
        channel = data.get("channel")
        if (
            not isinstance(channel, dict)
            or channel.get("index") != index
            or channel.get("name") != name.strip()
            or "secret" in channel
        ):
            raise self._wire_error(path, "channel response is invalid")
        return data

    def clear_channel(self, companion_name: str, index: int) -> dict:
        if type(index) is not int or index < 0:
            raise ValueError("index must be a non-negative integer")
        path = f"/companions/{_path_segment(companion_name)}/channels/{_path_segment(index)}"
        data = self._object_data(
            "DELETE",
            path,
        )
        if data.get("removed") is not True:
            raise self._wire_error(path, "channel response has invalid removed flag")
        return data

    # -- devices / push ----------------------------------------------------

    def devices(self) -> list:
        """List all paired devices (operator/admin only)."""
        path = "/devices"
        data = self._list_data("GET", path)
        forbidden = {
            "token_id",
            "push_token",
            "push_relay_url",
            "mention_keywords",
        }
        for device in data:
            if not isinstance(device, dict):
                raise self._wire_error(path, "device entry must be an object")
            if forbidden.intersection(device):
                raise self._wire_error(path, "device entry contains private fields")
        return data

    def revoke_device(self, device_id: str) -> dict:
        """Revoke this paired device, or any device when using admin auth."""
        path = f"/devices/{_path_segment(device_id)}"
        data = self._object_data("DELETE", path)
        if data.get("revoked") is not True or data.get("device_id") != device_id:
            raise self._wire_error(path, "device revoke response is invalid")
        return data

    def register_push(
        self,
        device_id: str,
        push_token: str,
        *,
        push_detail: Optional[str] = None,
        mention_push: Optional[bool] = None,
        mention_keywords: Optional[list[str]] = None,
    ) -> dict:
        """Register with the repeater's operator-configured push relay.

        A device supplies only its platform token and privacy preferences.
        The relay URL belongs to repeater configuration and is intentionally
        not accepted here. Omitted preference arguments preserve their stored
        values; pass `push_detail="none"` to set that value explicitly.
        """
        body: dict = {"push_token": push_token}
        if push_detail is not None:
            body["push_detail"] = push_detail
        if mention_push is not None:
            body["mention_push"] = mention_push
        if mention_keywords is not None:
            body["mention_keywords"] = mention_keywords
        path = f"/devices/{_path_segment(device_id)}/push"
        data = self._object_data("POST", path, body=body)
        if (
            data.get("registered") is not True
            or data.get("device_id") != device_id
            or data.get("push_detail") not in ("none", "count", "preview")
            or type(data.get("mention_push")) is not bool
            or "push_token" in data
        ):
            raise self._wire_error(path, "push registration response is invalid")
        return data

    def unregister_push(self, device_id: str) -> dict:
        path = f"/devices/{_path_segment(device_id)}/push"
        data = self._object_data("DELETE", path)
        if data.get("unregistered") is not True or data.get("device_id") != device_id:
            raise self._wire_error(path, "push unregister response is invalid")
        return data

    # -- convenience -------------------------------------------------------

    def follow(
        self,
        companion_name: str,
        cursor: str,
        *,
        limit: int = 200,
        include: Optional[str] = None,
        max_pages: Optional[int] = 100,
    ) -> tuple[list, str]:
        """Drain sync pages until caught up. Returns ``(events, cursor)``.

        Raises :class:`RestError` if the server asks for a re-snapshot, since
        callers must handle that by re-bootstrapping rather than looping.
        """
        if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
            raise ValueError("max_pages must be a positive integer or None")
        sync_path = f"/companions/{_path_segment(companion_name)}/sync"
        sync_url = f"{self.base_url}/api/v1{sync_path}"
        collected: list = []
        pages = 0
        while True:
            if max_pages is not None and pages >= max_pages:
                raise RestError(
                    409,
                    {"error": "sync page limit reached before catch-up completed"},
                    sync_url,
                )
            result = self.sync(
                companion_name,
                cursor,
                limit=limit,
                include=include,
            )
            pages += 1
            if result.snapshot_required:
                raise RestError(
                    409,
                    {
                        "error": "snapshot_required",
                        "reset_reason": result.reset_reason,
                    },
                    sync_url,
                )
            collected.extend(result.events)
            if result.has_more and result.next_cursor == cursor:
                raise RestError(
                    502,
                    {"error": "sync cursor did not advance"},
                    sync_url,
                )
            cursor = result.next_cursor
            if not result.has_more:
                break
        return collected, cursor
