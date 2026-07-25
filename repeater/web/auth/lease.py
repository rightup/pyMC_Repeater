"""Bounded authorization for long-lived HTTP and WebSocket sessions.

Normal requests authenticate once and finish quickly. Streams and WebSockets
must also stop when their JWT expires or their API token is revoked. This
module keeps that rule identical across transports without retaining raw
credentials.
"""

import math
import time
from collections.abc import Callable, Mapping
from typing import Any, Optional, Protocol

from .policy import api_token_scope, is_known_scope

AUTHORIZATION_RECHECK_SECONDS = 15.0
JWT_EXPIRY_REQUEST_ATTRIBUTE = "_openhop_jwt_expires_at"


class _TokenManager(Protocol):
    """Small storage surface an authorization lease needs."""

    def get_token(self, token_id: int) -> Optional[Mapping[str, Any]]: ...


def jwt_expires_at(payload: object) -> Optional[float]:
    """Return one finite JWT NumericDate, or ``None`` for an invalid claim."""

    if not isinstance(payload, Mapping):
        return None
    value = payload.get("exp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    expires_at = float(value)
    return expires_at if math.isfinite(expires_at) else None


def remember_jwt_expiration(request: object, payload: object) -> Optional[float]:
    """Keep a verified JWT's expiry in private request context for streams."""

    expires_at = jwt_expires_at(payload)
    if expires_at is None:
        return None
    setattr(request, JWT_EXPIRY_REQUEST_ATTRIBUTE, expires_at)
    return expires_at


def forget_jwt_expiration(request: object) -> None:
    """Remove a stale private expiry before authorizing with another method."""

    try:
        delattr(request, JWT_EXPIRY_REQUEST_ATTRIBUTE)
    except AttributeError:
        pass


class AuthorizationLease:
    """Revalidate one long-lived authenticated session without raw secrets."""

    def __init__(
        self,
        *,
        expires_at: Optional[float] = None,
        token_manager: Optional[_TokenManager] = None,
        token_id: Optional[int] = None,
        token_scope: object = None,
        token_check: Optional[Callable[[Mapping[str, Any]], bool]] = None,
        recheck_seconds: float = AUTHORIZATION_RECHECK_SECONDS,
    ):
        is_api_token = token_manager is not None or token_id is not None
        if (expires_at is not None) == is_api_token:
            raise ValueError("Authorization lease requires exactly one credential")
        if expires_at is not None:
            if not math.isfinite(expires_at):
                raise ValueError("JWT expiration is invalid")
        else:
            if type(token_id) is not int or token_id <= 0:
                raise ValueError("API token ID is invalid")
            if not is_known_scope(token_scope):
                raise ValueError("API token scope is invalid")
            if (
                isinstance(recheck_seconds, bool)
                or not isinstance(recheck_seconds, (int, float))
                or not math.isfinite(float(recheck_seconds))
                or recheck_seconds <= 0
            ):
                raise ValueError("Authorization recheck interval is invalid")

        self._expires_at = expires_at
        self._token_manager = token_manager
        self._token_id = token_id
        self._token_scope = token_scope
        self._token_check = token_check
        self._recheck_seconds = float(recheck_seconds)
        self._next_token_check = 0.0

    @classmethod
    def from_jwt_payload(cls, payload: object) -> "AuthorizationLease":
        expires_at = jwt_expires_at(payload)
        if expires_at is None:
            raise ValueError("Verified JWT has no valid expiration")
        return cls(expires_at=expires_at)

    @classmethod
    def from_api_token(
        cls,
        token_info: Mapping[str, Any],
        token_manager: _TokenManager,
        *,
        token_check: Optional[Callable[[Mapping[str, Any]], bool]] = None,
    ) -> "AuthorizationLease":
        return cls(
            token_manager=token_manager,
            token_id=token_info.get("id"),
            token_scope=api_token_scope(token_info),
            token_check=token_check,
        )

    @classmethod
    def from_request(
        cls,
        request: object,
        token_manager: _TokenManager,
        *,
        token_check: Optional[Callable[[Mapping[str, Any]], bool]] = None,
    ) -> "AuthorizationLease":
        user = getattr(request, "user", None)
        if not isinstance(user, Mapping):
            raise ValueError("Authenticated request principal is missing")
        auth_type = user.get("auth_type")
        if auth_type in ("jwt", "jwt_query"):
            expires_at = getattr(request, JWT_EXPIRY_REQUEST_ATTRIBUTE, None)
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(float(expires_at))
            ):
                raise ValueError("Authenticated JWT expiration is missing")
            return cls(expires_at=float(expires_at))
        if auth_type == "api_token":
            return cls(
                token_manager=token_manager,
                token_id=user.get("token_id"),
                token_scope=user.get("scope"),
                token_check=token_check,
            )
        raise ValueError("Authenticated request principal is unsupported")

    def is_active(self, *, force: bool = False) -> bool:
        """Return whether the credential still authorizes this session.

        Storage failures intentionally propagate so each transport can close
        with an observable service-error reason instead of failing open.
        """

        if self._expires_at is not None:
            return time.time() < self._expires_at

        now = time.monotonic()
        if not force and now < self._next_token_check:
            return True
        self._next_token_check = now + self._recheck_seconds
        if self._token_manager is None or self._token_id is None:
            return False
        token_info = self._token_manager.get_token(self._token_id)
        if token_info is None:
            return False
        if token_info.get("id") != self._token_id:
            return False
        if api_token_scope(token_info) != self._token_scope:
            return False
        return self._token_check is None or bool(self._token_check(token_info))

    def check_in(self, maximum_wait: float) -> float:
        """Return seconds until the next required authorization decision."""

        wait = max(0.0, float(maximum_wait))
        if self._expires_at is not None:
            remaining = self._expires_at - time.time()
        else:
            remaining = self._next_token_check - time.monotonic()
        return max(0.0, min(wait, remaining))
