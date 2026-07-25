import logging
import time
from typing import Any, Dict, Optional

import jwt

logger = logging.getLogger(__name__)

JWT_EXPIRY_MINUTES_MIN = 1
JWT_EXPIRY_MINUTES_MAX = 7 * 24 * 60
JWT_SECRET_MIN_BYTES = 32


def validate_jwt_expiry_minutes(value: object) -> int:
    """Return an exact, bounded JWT lifetime from configuration."""

    if (
        type(value) is not int
        or value < JWT_EXPIRY_MINUTES_MIN
        or value > JWT_EXPIRY_MINUTES_MAX
    ):
        raise ValueError(
            "repeater.security.jwt_expiry_minutes must be an integer between "
            f"{JWT_EXPIRY_MINUTES_MIN} and {JWT_EXPIRY_MINUTES_MAX}"
        )
    return value


def validate_jwt_signing_secret(value: object) -> str:
    """Return a nonblank JWT signing secret with an explicit strength floor."""

    message = (
        "repeater.security.jwt_secret must be a nonblank string containing "
        f"at least {JWT_SECRET_MIN_BYTES} UTF-8 bytes"
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(message) from None
    if size < JWT_SECRET_MIN_BYTES:
        raise ValueError(message)
    return value


class JWTHandler:
    def __init__(self, secret: str, expiry_minutes: int = 15):
        self.secret = validate_jwt_signing_secret(secret)
        self.expiry_minutes = validate_jwt_expiry_minutes(expiry_minutes)

    def create_jwt(self, username: str, client_id: str) -> str:

        now = int(time.time())
        expiry = now + (self.expiry_minutes * 60)

        payload = {"sub": username, "exp": expiry, "iat": now, "client_id": client_id}

        token = jwt.encode(payload, self.secret, algorithm="HS256")
        logger.info(f"Created JWT for user '{username}' with client_id '{client_id[:8]}...'")
        return token

    def verify_jwt(self, token: str, *, quiet: bool = False) -> Optional[Dict]:
        """Verify a JWT, optionally suppressing expected rejection logs.

        ``quiet`` is for callers that intentionally try the same credential as
        another authentication type after JWT verification rejects it.  The
        caller remains responsible for logging one final authentication
        failure when every supported type has rejected the credential.
        """

        try:
            payload = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                options={"require": ["exp", "iat", "sub", "client_id"]},
            )
            if not self._valid_text_claim(payload.get("sub"), 64):
                if not quiet:
                    logger.warning("Invalid JWT token: invalid sub claim")
                return None
            if not self._valid_text_claim(payload.get("client_id"), 128):
                if not quiet:
                    logger.warning("Invalid JWT token: invalid client_id claim")
                return None
            return payload
        except jwt.ExpiredSignatureError:
            if not quiet:
                logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            if not quiet:
                logger.warning("Invalid JWT token: %s", e)
            return None

    @staticmethod
    def _valid_text_claim(value, max_bytes: int) -> bool:
        if not isinstance(value, str) or not value or value != value.strip():
            return False
        try:
            encoded_size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            return False
        return (
            encoded_size <= max_bytes
            and all(character.isprintable() for character in value)
        )


def verify_jwt_for_auth_fallback(
    jwt_handler: Any,
    token: str,
) -> Optional[Dict]:
    """Quietly probe a real JWT handler before an API-token fallback.

    Minimal custom handlers that implement the historical one-argument method
    remain supported.  JWT-only authentication paths should call
    ``verify_jwt`` directly so their rejection diagnostics stay observable.
    """

    if isinstance(jwt_handler, JWTHandler):
        return jwt_handler.verify_jwt(token, quiet=True)
    return jwt_handler.verify_jwt(token)
