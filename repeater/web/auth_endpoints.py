"""
Authentication endpoints for login and token management
"""

import json
import logging
import math
import secrets
import threading
import time

import cherrypy

from repeater.data_acquisition.sqlite_handler import CompanionStorageError

from .api_validation import (
    positive_sqlite_row_id,
    read_json_object,
    reject_control_characters,
    reject_unknown_fields,
    text_field,
)
from .auth.middleware import require_admin
from .auth.policy import (
    api_token_scope,
    bearer_token_from_header,
    is_admin_scope,
    is_valid_bearer_token,
    validate_new_admin_password,
)

logger = logging.getLogger(__name__)

_MIN_ADMIN_PASSWORD_LEN = 8


def _validation_error(exc: cherrypy.HTTPError) -> dict:
    """Preserve the established JSON auth contract for validation failures."""

    cherrypy.response.status = exc.status
    message = getattr(exc, "_message", None)
    if not message:
        message = exc.args[1] if len(exc.args) > 1 else "Invalid request"
    return {"success": False, "error": str(message)}


def _validation_error_bytes(exc: cherrypy.HTTPError) -> bytes:
    return json.dumps(_validation_error(exc)).encode("utf-8")


def _auth_storage_unavailable_response(
    operation: str,
    exc: CompanionStorageError,
) -> dict:
    """Map token-store failures to an observable response without leaking details."""

    logger.error("Authentication storage unavailable during %s: %s", operation, exc)
    cherrypy.response.status = 503
    return {"success": False, "error": "Authentication storage unavailable"}


def _auth_storage_unavailable(operation: str, exc: CompanionStorageError) -> bytes:
    return json.dumps(_auth_storage_unavailable_response(operation, exc)).encode("utf-8")


def _api_token_candidates(
    bearer_api_token: str,
    raw_api_key: str,
) -> tuple[str, ...]:
    """Return distinct API credentials in the same order as the auth middleware."""

    api_key = raw_api_key if is_valid_bearer_token(raw_api_key) else ""
    return tuple(dict.fromkeys(token for token in (bearer_api_token, api_key) if token))


class _LoginThrottle:
    """In-memory login throttle with exponential backoff."""

    def __init__(
        self,
        per_ip_threshold: int = 5,
        per_user_threshold: int = 5,
        global_threshold: int = 20,
        base_backoff_sec: int = 1,
        max_backoff_sec: int = 60,
        window_sec: int = 300,
        time_fn=None,
    ):
        self.per_ip_threshold = per_ip_threshold
        self.per_user_threshold = per_user_threshold
        self.global_threshold = global_threshold
        self.base_backoff_sec = base_backoff_sec
        self.max_backoff_sec = max_backoff_sec
        self.window_sec = window_sec
        self._time_fn = time_fn or time.monotonic
        self._lock = threading.Lock()
        self._ip_states = {}
        self._user_states = {}
        self._global_state = {"failures": 0, "last_failure": 0.0, "blocked_until": 0.0}

    def _state(self, bucket: dict, key: str):
        if key not in bucket:
            bucket[key] = {"failures": 0, "last_failure": 0.0, "blocked_until": 0.0}
        return bucket[key]

    def _prune_locked(self, now: float) -> None:
        """Drop inactive attacker-controlled keys while holding the lock."""

        for bucket in (self._ip_states, self._user_states):
            stale = [
                key
                for key, state in bucket.items()
                if now - float(state.get("last_failure", 0.0)) > self.window_sec
            ]
            for key in stale:
                bucket.pop(key, None)

    def _maybe_decay(self, state: dict, now: float) -> None:
        last = state.get("last_failure", 0.0)
        if last and (now - last) > self.window_sec:
            state["failures"] = 0
            state["blocked_until"] = 0.0

    def _record_failure(self, state: dict, threshold: int, now: float) -> None:
        self._maybe_decay(state, now)
        state["failures"] = int(state.get("failures", 0)) + 1
        state["last_failure"] = now
        if state["failures"] >= threshold:
            exponent = state["failures"] - threshold
            delay = min(self.max_backoff_sec, self.base_backoff_sec * (2**exponent))
            state["blocked_until"] = max(float(state.get("blocked_until", 0.0)), now + delay)

    def _retry_after(self, state: dict, now: float) -> int:
        self._maybe_decay(state, now)
        blocked_until = float(state.get("blocked_until", 0.0))
        if blocked_until <= now:
            return 0
        return max(1, math.ceil(blocked_until - now))

    def get_retry_after(self, client_ip: str, username: str) -> int:
        now = self._time_fn()
        user_key = (username or "").strip().lower() or "<unknown>"
        ip_key = client_ip or "<unknown>"
        with self._lock:
            self._prune_locked(now)
            # Read-only admission checks must not allocate attacker-controlled
            # keys. Only a recorded authentication failure creates state.
            ip_retry = self._retry_after(self._ip_states.get(ip_key, {}), now)
            user_retry = self._retry_after(
                self._user_states.get(user_key, {}),
                now,
            )
            global_retry = self._retry_after(self._global_state, now)
            return max(ip_retry, user_retry, global_retry)

    def register_failure(self, client_ip: str, username: str) -> int:
        now = self._time_fn()
        user_key = (username or "").strip().lower() or "<unknown>"
        ip_key = client_ip or "<unknown>"
        with self._lock:
            self._prune_locked(now)
            self._record_failure(self._state(self._ip_states, ip_key), self.per_ip_threshold, now)
            self._record_failure(
                self._state(self._user_states, user_key), self.per_user_threshold, now
            )
            self._record_failure(self._global_state, self.global_threshold, now)
            ip_retry = self._retry_after(self._state(self._ip_states, ip_key), now)
            user_retry = self._retry_after(self._state(self._user_states, user_key), now)
            global_retry = self._retry_after(self._global_state, now)
            return max(ip_retry, user_retry, global_retry)

    def register_success(self, client_ip: str, username: str) -> None:
        user_key = (username or "").strip().lower() or "<unknown>"
        ip_key = client_ip or "<unknown>"
        with self._lock:
            self._ip_states.pop(ip_key, None)
            self._user_states.pop(user_key, None)
            # So one successful login doesn't hide broad abuse patterns, keep global
            # state but soften it.
            self._global_state["failures"] = max(0, int(self._global_state.get("failures", 0)) - 1)


class AuthAPIEndpoints:
    """Nested endpoint for /api/auth/* RESTful routes"""

    def __init__(self):
        # Create tokens nested endpoint for /api/auth/tokens
        self.tokens = TokensAPIEndpoint()


class TokensAPIEndpoint:
    """RESTful token management endpoints for /api/auth/tokens"""

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_admin
    def index(self):
        cherrypy.response.headers["Cache-Control"] = "no-store"

        # Handle CORS preflight
        if cherrypy.request.method == "OPTIONS":
            return {}

        # Get token manager from cherrypy config
        token_manager = cherrypy.config.get("token_manager")
        if not token_manager:
            cherrypy.response.status = 500
            return {"success": False, "error": "Token manager not available"}

        if cherrypy.request.method == "GET":
            try:
                tokens = token_manager.list_tokens()
                return {"success": True, "tokens": tokens}
            except CompanionStorageError as exc:
                return _auth_storage_unavailable_response("token listing", exc)
            except Exception as e:
                logger.error(f"Token list error: {e}")
                cherrypy.response.status = 500
                return {"success": False, "error": "Failed to list tokens"}

        elif cherrypy.request.method == "POST":
            try:
                data = read_json_object(
                    max_bytes=4096,
                    require_json_content_type=True,
                )
                reject_unknown_fields(data, {"name"})
                name = text_field(
                    data,
                    "name",
                    required=True,
                    max_bytes=128,
                )
                if name is None:  # Defensive: required=True normally rejects this.
                    raise cherrypy.HTTPError(400, "name required")
                reject_control_characters(name, "name")
                name = name.strip()
                if not name:
                    raise cherrypy.HTTPError(400, "name required")

                # Create the token
                token_id, plaintext_token = token_manager.create_token(name)

                logger.info(
                    f"Generated API token '{name}' (ID: {token_id}) by user {cherrypy.request.user['username']}"
                )

                return {
                    "success": True,
                    "token": plaintext_token,
                    "token_id": token_id,
                    "name": name,
                    "warning": "Save this token securely - it will not be shown again",
                }

            except cherrypy.HTTPError as exc:
                return _validation_error(exc)
            except CompanionStorageError as exc:
                return _auth_storage_unavailable_response("token creation", exc)
            except Exception as e:
                logger.error(f"Token generation error: {e}")
                cherrypy.response.status = 500
                return {"success": False, "error": "Failed to generate token"}
        else:
            raise cherrypy.HTTPError(405, "Method not allowed")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_admin
    def default(self, token_id=None):
        cherrypy.response.headers["Cache-Control"] = "no-store"

        # Handle CORS preflight
        if cherrypy.request.method == "OPTIONS":
            return {}

        # Get token manager from cherrypy config
        token_manager = cherrypy.config.get("token_manager")
        if not token_manager:
            cherrypy.response.status = 500
            return {"success": False, "error": "Token manager not available"}

        if cherrypy.request.method == "DELETE":
            try:
                if not token_id:
                    cherrypy.response.status = 400
                    return {"success": False, "error": "Token ID is required"}

                # Convert to int
                try:
                    token_id_int = positive_sqlite_row_id(token_id, "token_id")
                except cherrypy.HTTPError as exc:
                    return _validation_error(exc)

                # Revoke the token
                success = token_manager.revoke_token(token_id_int)

                if success:
                    logger.info(
                        f"Revoked API token ID {token_id_int} by user {cherrypy.request.user['username']}"
                    )
                    return {"success": True, "message": "Token revoked successfully"}
                else:
                    cherrypy.response.status = 404
                    return {"success": False, "error": "Token not found"}

            except CompanionStorageError as exc:
                return _auth_storage_unavailable_response("token revocation", exc)
            except Exception as e:
                logger.error(f"Token revocation error: {e}")
                cherrypy.response.status = 500
                return {"success": False, "error": "Failed to revoke token"}
        else:
            raise cherrypy.HTTPError(405, "Method not allowed")


class AuthEndpoints:
    def __init__(
        self,
        config,
        jwt_handler,
        token_manager,
        config_manager=None,
        login_throttle=None,
    ):
        self.config = config
        self.jwt_handler = jwt_handler
        self.token_manager = token_manager
        self.config_manager = config_manager
        self._login_throttle = login_throttle or _LoginThrottle()

    @staticmethod
    def _get_request_ip() -> str:
        """Extract client IP for login throttling/auditing."""
        # Forwarding headers are attacker-controlled unless the server has an
        # explicit trusted-proxy policy.  This service currently has none, so
        # throttle on the actual peer address.
        remote = getattr(cherrypy.request, "remote", None)
        if remote and getattr(remote, "ip", None):
            return str(remote.ip)

        return "unknown"

    @cherrypy.expose
    def login(self, **kwargs):

        cherrypy.response.headers["Content-Type"] = "application/json"
        cherrypy.response.headers["Cache-Control"] = "no-store"

        # Handle CORS preflight
        if cherrypy.request.method == "OPTIONS":
            cherrypy.response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            cherrypy.response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-API-Key"
            )
            return b""

        if cherrypy.request.method != "POST":
            raise cherrypy.HTTPError(405, "Method not allowed")

        try:
            # Parse JSON manually since json_in cannot be combined with this
            # endpoint's explicit OPTIONS handling.
            data = read_json_object(
                max_bytes=4096,
                require_json_content_type=True,
            )
            reject_unknown_fields(data, {"username", "password", "client_id"})
            username = text_field(data, "username", required=True, max_bytes=64, strip=True)
            password = text_field(data, "password", required=True, max_bytes=1024)
            client_id = text_field(data, "client_id", required=True, max_bytes=128, strip=True)
            reject_control_characters(username, "username")
            reject_control_characters(client_id, "client_id")
            client_ip = self._get_request_ip()

            retry_after = self._login_throttle.get_retry_after(client_ip, username)
            if retry_after > 0:
                cherrypy.response.status = 429
                cherrypy.response.headers["Retry-After"] = str(retry_after)
                logger.warning(
                    "Login throttled for user '%s' from %s (retry_after=%ss)",
                    username,
                    client_ip,
                    retry_after,
                )
                return json.dumps(
                    {
                        "success": False,
                        "error": "Too many login attempts. Please wait and try again.",
                        "retry_after": retry_after,
                    }
                ).encode("utf-8")

            # Validate credentials against config
            # Check if username is 'admin' and password matches config
            repeater_config = self.config.get("repeater", {})
            security_config = repeater_config.get("security", {})
            config_password = security_config.get("admin_password", "")

            # The historical default is a setup sentinel, not a credential.
            # Issuing an administrator JWT for it would bypass the public
            # bootstrap boundary that deliberately remains open.
            if not isinstance(config_password, str) or config_password in ("", "admin123"):
                logger.warning("Login attempt rejected - password not configured")
                cherrypy.response.status = 409
                return json.dumps(
                    {
                        "success": False,
                        "error": "System not configured. Please complete setup wizard.",
                    }
                ).encode("utf-8")

            if len(config_password) < _MIN_ADMIN_PASSWORD_LEN:
                logger.warning(
                    "Weak admin password configured (len=%s). Login remains allowed for compatibility.",
                    len(config_password),
                )

            if username == "admin" and secrets.compare_digest(password, config_password):
                self._login_throttle.register_success(client_ip, username)
                # Create JWT token
                token = self.jwt_handler.create_jwt(username, client_id)

                logger.info(
                    "Successful login for user '%s' from client '%s...' ip=%s",
                    username,
                    client_id[:8],
                    client_ip,
                )

                return json.dumps(
                    {
                        "success": True,
                        "token": token,
                        "expires_in": self.jwt_handler.expiry_minutes * 60,
                        "username": username,
                    }
                ).encode("utf-8")
            else:
                retry_after = self._login_throttle.register_failure(client_ip, username)
                if retry_after > 0:
                    cherrypy.response.status = 429
                    cherrypy.response.headers["Retry-After"] = str(retry_after)
                    logger.warning(
                        "Failed login attempt throttled for user '%s' from %s (retry_after=%ss)",
                        username,
                        client_ip,
                        retry_after,
                    )
                    return json.dumps(
                        {
                            "success": False,
                            "error": "Too many login attempts. Please wait and try again.",
                            "retry_after": retry_after,
                        }
                    ).encode("utf-8")

                cherrypy.response.status = 401
                logger.warning("Failed login attempt for user '%s' from %s", username, client_ip)

                # Don't reveal which part was wrong
                return json.dumps(
                    {"success": False, "error": "Invalid username or password"}
                ).encode("utf-8")

        except cherrypy.HTTPError as exc:
            return _validation_error_bytes(exc)
        except Exception as e:
            logger.error(f"Login error: {e}")
            cherrypy.response.status = 500
            return json.dumps({"success": False, "error": "Internal server error"}).encode("utf-8")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_admin
    def verify(self):
        cherrypy.response.headers["Cache-Control"] = "no-store"
        if cherrypy.request.method != "GET":
            raise cherrypy.HTTPError(405, "Method not allowed")

        return {"success": True, "authenticated": True, "user": cherrypy.request.user}

    @cherrypy.expose
    def refresh(self, **kwargs):

        cherrypy.response.headers["Content-Type"] = "application/json"
        cherrypy.response.headers["Cache-Control"] = "no-store"

        # Handle CORS preflight
        if cherrypy.request.method == "OPTIONS":
            cherrypy.response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            cherrypy.response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-API-Key"
            )
            return b""

        if cherrypy.request.method != "POST":
            raise cherrypy.HTTPError(405, "Method not allowed")

        try:
            # Manual authentication check (can't use @require_auth since we need to handle OPTIONS)
            auth_header = cherrypy.request.headers.get("Authorization", "")
            raw_api_key = cherrypy.request.headers.get("X-API-Key", "")

            jwt_handler = cherrypy.config.get("jwt_handler")
            token_manager = cherrypy.config.get("token_manager")

            if not jwt_handler or not token_manager:
                logger.error("Auth handlers not configured")
                cherrypy.response.status = 500
                return json.dumps(
                    {"success": False, "error": "Authentication not configured"}
                ).encode("utf-8")

            user_info = None
            # Request authentication state, not a credential literal.
            bearer_api_token = ""  # nosec B105

            # Check JWT first
            token = bearer_token_from_header(auth_header)
            if token is not None:
                payload = jwt_handler.verify_jwt(token)
                if payload:
                    user_info = {
                        "username": payload["sub"],
                        "client_id": payload.get("client_id"),
                        "auth_method": "jwt",
                    }
                else:
                    bearer_api_token = token

            # Check API token
            if not user_info:
                for presented_api_token in _api_token_candidates(
                    bearer_api_token,
                    raw_api_key,
                ):
                    try:
                        token_data = token_manager.verify_token(presented_api_token)
                    except CompanionStorageError as exc:
                        return _auth_storage_unavailable("token refresh", exc)
                    if not token_data:
                        continue
                    scope = api_token_scope(token_data)
                    if not is_admin_scope(scope):
                        logger.warning(
                            "Denied JWT refresh for API token %r with scope %r",
                            token_data.get("id"),
                            scope,
                        )
                        cherrypy.response.status = 403
                        return json.dumps(
                            {"success": False, "error": "Forbidden - Admin scope required"}
                        ).encode("utf-8")
                    user_info = {
                        "username": "admin",
                        "token_id": token_data["id"],
                        "auth_method": "api_token",
                    }
                    break

            if not user_info:
                cherrypy.response.status = 401
                return json.dumps(
                    {"success": False, "error": "Unauthorized - Valid JWT or API token required"}
                ).encode("utf-8")

            data = read_json_object(
                max_bytes=4096,
                require_json_content_type=True,
            )
            reject_unknown_fields(data, {"client_id"})
            client_id = text_field(
                data,
                "client_id",
                default=user_info.get("client_id", ""),
                required=True,
                max_bytes=128,
                strip=True,
            )
            reject_control_characters(client_id, "client_id")

            if not client_id:
                cherrypy.response.status = 400
                return json.dumps({"success": False, "error": "Client ID is required"}).encode(
                    "utf-8"
                )

            # Create new JWT token (refreshes expiry time)
            new_token = self.jwt_handler.create_jwt(user_info["username"], client_id)

            logger.info(
                f"Token refreshed for user '{user_info['username']}' from client '{client_id[:8]}...'"
            )

            return json.dumps(
                {
                    "success": True,
                    "token": new_token,
                    "expires_in": self.jwt_handler.expiry_minutes * 60,
                    "username": user_info["username"],
                }
            ).encode("utf-8")

        except cherrypy.HTTPError as exc:
            return _validation_error_bytes(exc)
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            cherrypy.response.status = 500
            return json.dumps({"success": False, "error": "Failed to refresh token"}).encode(
                "utf-8"
            )

    @cherrypy.expose
    def change_password(self):

        cherrypy.response.headers["Content-Type"] = "application/json"
        cherrypy.response.headers["Cache-Control"] = "no-store"

        # Handle CORS preflight
        if cherrypy.request.method == "OPTIONS":
            cherrypy.response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            cherrypy.response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-API-Key"
            )
            return b""

        if cherrypy.request.method != "POST":
            raise cherrypy.HTTPError(405, "Method not allowed")

        # Require authentication for POST
        # Get auth handlers from global cherrypy config
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        if not jwt_handler or not token_manager:
            logger.error("Auth handlers not configured")
            raise cherrypy.HTTPError(500, "Authentication not configured")

        # Try JWT authentication first
        auth_header = cherrypy.request.headers.get("Authorization", "")
        user = None
        # Request authentication state, not a credential literal.
        bearer_api_token = ""  # nosec B105

        token = bearer_token_from_header(auth_header)
        if token is not None:
            payload = jwt_handler.verify_jwt(token)

            if payload:
                user = {
                    "username": payload["sub"],
                    "client_id": payload["client_id"],
                    "auth_type": "jwt",
                }
            else:
                bearer_api_token = token

        # Try API token authentication if JWT failed
        if not user:
            raw_api_key = cherrypy.request.headers.get("X-API-Key", "")
            for api_key in _api_token_candidates(
                bearer_api_token,
                raw_api_key,
            ):
                try:
                    token_info = token_manager.verify_token(api_key)
                except CompanionStorageError as exc:
                    return _auth_storage_unavailable("password change", exc)

                if not token_info:
                    continue
                scope = api_token_scope(token_info)
                if not is_admin_scope(scope):
                    logger.warning(
                        "Denied password change for API token %r with scope %r",
                        token_info.get("id"),
                        scope,
                    )
                    cherrypy.response.status = 403
                    return json.dumps(
                        {"success": False, "error": "Forbidden - Admin scope required"}
                    ).encode("utf-8")
                user = {
                    "username": "api_token",
                    "token_name": token_info["name"],
                    "token_id": token_info["id"],
                    "auth_type": "api_token",
                }
                break

        if not user:
            cherrypy.response.status = 401
            return json.dumps(
                {"success": False, "error": "Unauthorized - Valid JWT or API token required"}
            ).encode("utf-8")

        try:
            data = read_json_object(
                max_bytes=4096,
                require_json_content_type=True,
            )
            reject_unknown_fields(data, {"current_password", "new_password"})
            current_password = text_field(
                data,
                "current_password",
                required=True,
                max_bytes=1024,
            )
            new_password = text_field(
                data,
                "new_password",
                required=True,
                max_bytes=1024,
            )

            try:
                validate_new_admin_password(new_password)
            except ValueError as exc:
                cherrypy.response.status = 400
                return json.dumps({"success": False, "error": str(exc)}).encode("utf-8")

            # Verify current password
            repeater_config = self.config.get("repeater", {})
            security_config = repeater_config.get("security", {})
            config_password = security_config.get("admin_password", "")

            if not config_password:
                cherrypy.response.status = 500
                return json.dumps({"success": False, "error": "System configuration error"}).encode(
                    "utf-8"
                )

            if not secrets.compare_digest(current_password, config_password):
                cherrypy.response.status = 401
                return json.dumps(
                    {"success": False, "error": "Current password is incorrect"}
                ).encode("utf-8")

            # Save to config file using ConfigManager
            if not self.config_manager:
                cherrypy.response.status = 500
                return json.dumps(
                    {"success": False, "error": "Config manager not available"}
                ).encode("utf-8")

            previous_password = config_password
            security_config["admin_password"] = new_password
            try:
                saved = self.config_manager.save_to_file()
            except Exception:
                security_config["admin_password"] = previous_password
                raise
            if saved:
                logger.info(f"Admin password changed successfully by user {user['username']}")
                return json.dumps(
                    {
                        "success": True,
                        "message": "Password changed successfully. Please log in again with your new password.",
                    }
                ).encode("utf-8")

            security_config["admin_password"] = previous_password
            cherrypy.response.status = 500
            return json.dumps(
                {"success": False, "error": "Failed to save password to config file"}
            ).encode("utf-8")

        except cherrypy.HTTPError as exc:
            return _validation_error_bytes(exc)
        except Exception as e:
            logger.error(f"Password change error: {e}")
            cherrypy.response.status = 500
            return json.dumps({"success": False, "error": "Failed to change password"}).encode(
                "utf-8"
            )
