"""Small validation helpers shared by network-facing JSON endpoints.

The companion API accepts input from phones, browsers, and autonomous agents.
Keeping the rules here makes the wire contract easy to read and prevents each
handler from inventing subtly different limits.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Optional

import cherrypy


DEFAULT_JSON_BODY_BYTES = 16 * 1024
MAX_JSON_NESTING_DEPTH = 64
SQLITE_ROW_ID_MAX = (1 << 63) - 1


class _DuplicateJSONField(ValueError):
    pass


def _reject_non_finite_json_number(value: str) -> None:
    """Reject Python's non-standard ``NaN``/``Infinity`` JSON extensions."""

    raise ValueError(f"Non-finite JSON number: {value}")


def _parse_finite_json_float(value: str) -> float:
    """Parse a JSON decimal only when Python can represent it finitely."""

    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite JSON number: {value}")
    return parsed


def _json_object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONField(f"Duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_excessive_json_nesting(value: Any) -> None:
    """Bound structural depth without using Python recursion."""

    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_NESTING_DEPTH:
            raise cherrypy.HTTPError(
                400,
                f"JSON body exceeds {MAX_JSON_NESTING_DEPTH} nesting levels",
            )
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def read_json_object(
    *,
    max_bytes: int = DEFAULT_JSON_BODY_BYTES,
    require_json_content_type: bool = False,
    allow_empty_without_content_type: bool = False,
) -> dict:
    """Read one bounded JSON object from the active CherryPy request.

    Public credential/bootstrap handlers opt into the media-type requirement.
    Besides making their contract explicit, this prevents a cross-origin
    browser from submitting them as a CORS-simple ``text/plain`` or form
    request. Authenticated v1 actions may additionally allow a truly empty
    body without a media type while still requiring ``application/json`` for
    every non-empty body. Private legacy handlers retain their historical
    no-header compatibility.
    """

    content_type = str(cherrypy.request.headers.get("Content-Type", "") or "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if (
        require_json_content_type
        and not allow_empty_without_content_type
        and media_type != "application/json"
    ):
        raise cherrypy.HTTPError(
            415,
            "Content-Type must be application/json",
        )

    content_length = cherrypy.request.headers.get("Content-Length")
    if content_length:
        try:
            parsed_length = int(content_length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > max_bytes:
                raise cherrypy.HTTPError(413, f"JSON body exceeds {max_bytes} bytes")
        except ValueError:
            raise cherrypy.HTTPError(400, "Invalid Content-Length") from None

    raw = cherrypy.request.body.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise cherrypy.HTTPError(413, f"JSON body exceeds {max_bytes} bytes")
    if raw and require_json_content_type and media_type != "application/json":
        raise cherrypy.HTTPError(
            415,
            "Content-Type must be application/json",
        )
    if not raw:
        return {}
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_non_finite_json_number,
            parse_float=_parse_finite_json_float,
        )
    except _DuplicateJSONField as exc:
        raise cherrypy.HTTPError(400, str(exc)) from None
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise cherrypy.HTTPError(400, f"Invalid JSON body: {exc}") from None
    _reject_excessive_json_nesting(value)
    if not isinstance(value, dict):
        raise cherrypy.HTTPError(400, "JSON body must be an object")
    return value


def reject_unknown_fields(body: dict, allowed: Iterable[str]) -> None:
    """Reject misspelled or unsupported fields instead of silently ignoring them."""

    allowed_set = set(allowed)
    unknown = sorted(str(key) for key in body if key not in allowed_set)
    if unknown:
        raise cherrypy.HTTPError(400, f"Unknown field(s): {', '.join(unknown)}")


def text_field(
    body: dict,
    name: str,
    *,
    default: Optional[str] = None,
    required: bool = False,
    max_bytes: int,
    strip: bool = False,
) -> Optional[str]:
    """Return a UTF-8 string with an explicit byte limit."""

    value: Any = body.get(name, default)
    if value is None:
        if required:
            raise cherrypy.HTTPError(400, f"{name} required")
        return None
    if not isinstance(value, str):
        raise cherrypy.HTTPError(400, f"{name} must be a string")
    if strip:
        value = value.strip()
    if required and not value:
        raise cherrypy.HTTPError(400, f"{name} required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise cherrypy.HTTPError(400, f"{name} must be valid UTF-8") from None
    if size > max_bytes:
        raise cherrypy.HTTPError(400, f"{name} exceeds {max_bytes} UTF-8 bytes")
    return value


def reject_control_characters(value: str, name: str) -> None:
    """Reject terminal/log-hostile non-printing characters in readable text."""

    if any(not character.isprintable() for character in value):
        raise cherrypy.HTTPError(400, f"{name} must not contain control characters")


def positive_sqlite_row_id(value: object, name: str) -> int:
    """Parse a positive identifier that SQLite can bind as an INTEGER."""

    try:
        row_id = int(str(value))
    except (TypeError, ValueError, OverflowError):
        raise cherrypy.HTTPError(400, f"Invalid {name}") from None
    if not 1 <= row_id <= SQLITE_ROW_ID_MAX:
        raise cherrypy.HTTPError(
            400,
            f"{name} must be between 1 and {SQLITE_ROW_ID_MAX}",
        )
    return row_id


def integer_field(
    body: dict,
    name: str,
    *,
    default: Optional[int] = None,
    low: Optional[int] = None,
    high: Optional[int] = None,
) -> Optional[int]:
    """Return a bounded JSON integer without coercing another JSON type."""

    value = body.get(name, default)
    if value is None:
        return None
    # ``bool`` subclasses ``int`` in Python, and ``int(1.5)`` silently
    # truncates.  Neither is an integer on this JSON contract.
    if type(value) is not int:
        raise cherrypy.HTTPError(400, f"{name} must be an integer")
    result = value
    if low is not None and result < low:
        raise cherrypy.HTTPError(400, f"{name} must be at least {low}")
    if high is not None and result > high:
        raise cherrypy.HTTPError(400, f"{name} must be at most {high}")
    return result


def boolean_field(
    body: dict,
    name: str,
    *,
    default: Optional[bool] = None,
) -> Optional[bool]:
    """Return a JSON boolean without Python truthiness coercion."""

    if name not in body:
        return default
    value = body[name]
    if value is None and default is None:
        return None
    if type(value) is not bool:
        raise cherrypy.HTTPError(400, f"{name} must be a boolean")
    return value


def finite_float_field(
    body: dict,
    name: str,
    *,
    default: Optional[float] = None,
    low: Optional[float] = None,
    high: Optional[float] = None,
) -> Optional[float]:
    """Return a finite bounded JSON number without accepting booleans/text."""

    value = body.get(name, default)
    if value is None:
        return None
    if type(value) not in (int, float):
        raise cherrypy.HTTPError(400, f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise cherrypy.HTTPError(400, f"{name} must be finite") from None
    if not math.isfinite(result):
        raise cherrypy.HTTPError(400, f"{name} must be finite")
    if low is not None and result < low:
        raise cherrypy.HTTPError(400, f"{name} must be at least {low}")
    if high is not None and result > high:
        raise cherrypy.HTTPError(400, f"{name} must be at most {high}")
    return result
