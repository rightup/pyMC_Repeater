"""Small, shared authorization rules for HTTP and WebSocket callers.

Authentication answers "is this credential valid?"  This module answers the
separate question "where may the authenticated caller go?"  Keeping those
rules here prevents each transport from inventing its own interpretation of a
token scope.
"""

from collections.abc import Mapping
import re
from typing import Any, Optional

from repeater.companion.utils import validate_companion_registration_name

ADMIN_SCOPE = "admin"
COMPANION_SCOPE_PREFIX = "companion:"
ADMIN_PASSWORD_MIN_CHARS = 8
ADMIN_PASSWORD_MAX_BYTES = 1024
# Historical public sentinel that new setup/password changes explicitly reject.
DEFAULT_ADMIN_PASSWORD = "admin123"  # nosec B105
_BEARER_TOKEN_RE = re.compile(r"^[A-Za-z0-9\-._~+/]+={0,}$")
_BEARER_TOKEN_MAX_CHARS = 4096


def validate_new_admin_password(value: object) -> str:
    """Return a password that every supported login client can submit."""

    if not isinstance(value, str):
        raise ValueError("Administrator password must be a string")
    visible = value.strip()
    if len(visible) < ADMIN_PASSWORD_MIN_CHARS:
        raise ValueError(
            "Administrator password must contain at least 8 characters "
            "after trimming surrounding whitespace"
        )
    if visible == DEFAULT_ADMIN_PASSWORD:
        raise ValueError("Administrator password cannot be the default admin123")
    try:
        password_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Administrator password must be valid UTF-8") from None
    if password_bytes > ADMIN_PASSWORD_MAX_BYTES:
        raise ValueError("Administrator password must not exceed 1024 UTF-8 bytes")
    return value


def api_token_scope(token_info: Mapping[str, Any]) -> Any:
    """Return an API token's effective scope.

    Tokens created before scopes were introduced have ``NULL`` (or no scope
    field in older adapters).  Those are the one deliberate migration
    exception: they retain their historical administrator access.  Every
    explicit, unrecognized scope fails closed.
    """

    scope = token_info.get("scope")
    return ADMIN_SCOPE if scope is None else scope


def is_admin_scope(scope: Any) -> bool:
    """Return whether *scope* grants operator-level access."""

    return scope == ADMIN_SCOPE


def is_companion_scope(scope: Any) -> bool:
    """Return whether *scope* is a well-formed companion scope."""

    if not isinstance(scope, str) or not scope.startswith(COMPANION_SCOPE_PREFIX):
        return False
    companion_name = scope[len(COMPANION_SCOPE_PREFIX) :]
    if companion_name == "*":
        return True
    try:
        validate_companion_registration_name(companion_name)
    except ValueError:
        return False
    return True


def bearer_token_from_header(header: Any) -> Optional[str]:
    """Return one bounded RFC 6750 bearer credential."""

    if not isinstance(header, str):
        return None
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not is_valid_bearer_token(parts[1]):
        return None
    return parts[1]


def is_valid_bearer_token(token: Any) -> bool:
    """Accept JWT/API-token characters and reject whitespace or controls."""

    return (
        isinstance(token, str)
        and 0 < len(token) <= _BEARER_TOKEN_MAX_CHARS
        and _BEARER_TOKEN_RE.fullmatch(token) is not None
    )


def has_multiple_v1_credential_transports(
    path: Any,
    *,
    authorization_header: Any,
    api_key: Any,
    query_token_present: bool,
) -> bool:
    """Reject an ambiguous principal choice only on the Mobile API.

    The legacy Repeater API historically applies credential precedence and
    keeps that compatibility. Mobile v1 is a new contract: exactly one of
    Authorization, X-API-Key, or its SSE-only query JWT may be presented.
    """

    if not isinstance(path, str) or not (path == "/api/v1" or path.startswith("/api/v1/")):
        return False
    presented = (
        bool(authorization_header),
        bool(api_key),
        bool(query_token_present),
    )
    return sum(presented) > 1


def is_known_scope(scope: Any) -> bool:
    """Return whether *scope* is understood by this server version."""

    return is_admin_scope(scope) or is_companion_scope(scope)


def is_admin_user(user: Any) -> bool:
    """Return whether an authenticated request user is an administrator."""

    return isinstance(user, Mapping) and is_admin_scope(user.get("scope"))


def scope_allows_api_path(scope: Any, path: str) -> bool:
    """Return whether *scope* may enter *path*.

    Companion authorization inside ``/api/v1`` remains resource-specific: the
    v1 handlers decide which companion/device a token owns.  This outer rule
    only prevents a device credential from crossing into the legacy Repeater
    API, authentication endpoints, or other operator surfaces.
    """

    if is_admin_scope(scope):
        return True
    if not is_companion_scope(scope):
        return False
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    return path == "/api/v1" or path.startswith("/api/v1/")


def allows_query_jwt(method: str, path: str) -> bool:
    """Allow URL credentials only on exact browser-native SSE routes.

    Query strings leak more readily than headers (for example through
    browser history and access logs).  Native ``EventSource`` cannot set an
    ``Authorization`` header, so the Mobile v1 stream and the pre-v1
    compatibility stream are the only HTTP exceptions.  Exact methods and
    path shapes keep a ``?token=`` parameter from silently authenticating
    another API endpoint.
    """

    if method != "GET":
        return False
    if path == "/api/companion/events":
        return True
    segments = path.split("/")
    if (
        len(segments) != 6
        or segments[:4] != ["", "api", "v1", "companions"]
        or segments[5] != "events"
    ):
        return False
    try:
        validate_companion_registration_name(segments[4])
    except ValueError:
        return False
    return True
