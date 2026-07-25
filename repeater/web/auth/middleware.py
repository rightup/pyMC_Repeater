import logging
from functools import wraps

import cherrypy

from repeater.data_acquisition.sqlite_handler import CompanionStorageError

from .cherrypy_tool import request_was_authorized_by_tool
from .jwt_handler import verify_jwt_for_auth_fallback
from .lease import forget_jwt_expiration, remember_jwt_expiration
from .policy import (
    allows_query_jwt,
    api_token_scope,
    bearer_token_from_header,
    has_multiple_v1_credential_transports,
    is_admin_user,
    is_known_scope,
    is_valid_bearer_token,
    scope_allows_api_path,
)

logger = logging.getLogger(__name__)


def _api_token_user(token_info):
    """Build the one request-user shape used by all decorator-authenticated tokens."""

    return {
        "username": "api_token",
        "token_name": token_info["name"],
        "token_id": token_info["id"],
        "auth_type": "api_token",
        "scope": api_token_scope(token_info),
    }


def _forbidden_scope(user):
    scope = user.get("scope")
    logger.warning(
        "Rejected API token scope %r (token_id=%r, path=%s)",
        scope,
        user.get("token_id"),
        cherrypy.request.path_info,
    )
    cherrypy.response.status = 403
    cherrypy.response.headers["Content-Type"] = "application/json"
    return {"success": False, "error": "Forbidden - Token scope is not allowed"}


def _verify_api_token(token_manager, token):
    """Return token metadata or a stable 503 response when storage is down."""

    try:
        return token_manager.verify_token(token), None
    except CompanionStorageError:
        logger.error("API-token authentication storage is unavailable")
        cherrypy.response.status = 503
        cherrypy.response.headers["Content-Type"] = "application/json"
        return None, {
            "success": False,
            "error": "Authentication storage unavailable",
        }


def require_auth(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Preflight must not invoke protected handler logic.  The server-level
        # safe-CORS tool owns origin/header validation and response headers.
        if cherrypy.request.method == "OPTIONS":
            cherrypy.response.status = 204
            return None

        # The mounted /api tree authenticates in a before-handler tool.  Reuse
        # only that exact, process-marked decision; a caller-controlled header
        # or a stale/plain ``request.user`` mapping cannot take this path.
        if request_was_authorized_by_tool():
            user = getattr(cherrypy.request, "user", None)
            scope = user.get("scope") if isinstance(user, dict) else None
            if is_known_scope(scope) and scope_allows_api_path(
                scope,
                cherrypy.request.path_info,
            ):
                return func(*args, **kwargs)
            return _forbidden_scope(user if isinstance(user, dict) else {})

        # Get auth handlers from global cherrypy config (not app config)
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        if not jwt_handler or not token_manager:
            logger.error("Auth handlers not configured")
            raise cherrypy.HTTPError(500, "Authentication not configured")

        request_params = getattr(cherrypy.request, "params", None)
        if request_params is None:
            request_params = {}
        query_token_present = "token" in request_params
        query_token = request_params.pop("token", None)

        auth_header = cherrypy.request.headers.get("Authorization", "")
        api_key = cherrypy.request.headers.get("X-API-Key", "")
        if has_multiple_v1_credential_transports(
            cherrypy.request.path_info,
            authorization_header=auth_header,
            api_key=api_key,
            query_token_present=query_token_present,
        ):
            logger.warning(
                "Rejected multiple credential transports (path=%s)",
                cherrypy.request.path_info,
            )
            raise cherrypy.HTTPError(
                400,
                "Mobile API requests must use exactly one credential transport",
            )

        # Try JWT authentication first
        token = bearer_token_from_header(auth_header)
        if token is not None:
            payload = verify_jwt_for_auth_fallback(jwt_handler, token)

            if payload:
                # JWT is valid. Web UI operators are always 'admin' scope
                # (design doc §11.1) -- scopes are a mobile-device-token
                # concept, not a JWT one.
                cherrypy.request.user = {
                    "username": payload["sub"],
                    "client_id": payload["client_id"],
                    "auth_type": "jwt",
                    "scope": "admin",
                }
                forget_jwt_expiration(cherrypy.request)
                remember_jwt_expiration(cherrypy.request, payload)
                return func(*args, **kwargs)
            else:
                # Not a valid JWT -- device API tokens may also be sent as a
                # Bearer value (design doc always allowed this transport).
                token_info, storage_error = _verify_api_token(token_manager, token)
                if storage_error is not None:
                    return storage_error

                if token_info:
                    user = _api_token_user(token_info)
                    if not is_known_scope(user["scope"]) or not scope_allows_api_path(
                        user["scope"],
                        cherrypy.request.path_info,
                    ):
                        return _forbidden_scope(user)
                    cherrypy.request.user = user
                    forget_jwt_expiration(cherrypy.request)
                    return func(*args, **kwargs)

        if is_valid_bearer_token(query_token) and allows_query_jwt(
            cherrypy.request.method,
            cherrypy.request.path_info,
        ):
            payload = jwt_handler.verify_jwt(query_token)

            if payload:
                cherrypy.request.user = {
                    "username": payload["sub"],
                    "client_id": payload["client_id"],
                    "auth_type": "jwt_query",
                    "scope": "admin",
                }
                forget_jwt_expiration(cherrypy.request)
                remember_jwt_expiration(cherrypy.request, payload)
                return func(*args, **kwargs)
            else:
                logger.warning("Invalid or expired JWT query token")
        elif query_token:
            logger.warning(
                "Rejected query-string credential (method=%s, path=%s)",
                cherrypy.request.method,
                cherrypy.request.path_info,
            )

        # Try API token authentication
        if is_valid_bearer_token(api_key):
            token_info, storage_error = _verify_api_token(token_manager, api_key)
            if storage_error is not None:
                return storage_error

            if token_info:
                user = _api_token_user(token_info)
                if not is_known_scope(user["scope"]) or not scope_allows_api_path(
                    user["scope"],
                    cherrypy.request.path_info,
                ):
                    return _forbidden_scope(user)
                cherrypy.request.user = user
                forget_jwt_expiration(cherrypy.request)
                return func(*args, **kwargs)
            else:
                logger.warning("Invalid API token")
        elif api_key:
            logger.warning("Rejected malformed API token")

        # No valid authentication found
        logger.warning(f"Unauthorized access attempt to {cherrypy.request.path_info}")

        cherrypy.response.status = 401
        cherrypy.response.headers["Content-Type"] = "application/json"
        return {"success": False, "error": "Unauthorized - Valid JWT or API token required"}

    return wrapper


def require_admin(func):
    """Authenticate a request and require operator-level authorization."""

    @wraps(func)
    def admin_wrapper(*args, **kwargs):
        # CORS preflight carries no credential and performs no protected action.
        if cherrypy.request.method == "OPTIONS":
            cherrypy.response.status = 204
            return None

        user = getattr(cherrypy.request, "user", None)
        if not is_admin_user(user):
            logger.warning(
                "Rejected non-admin request (scope=%r, token_id=%r, path=%s)",
                user.get("scope") if isinstance(user, dict) else None,
                user.get("token_id") if isinstance(user, dict) else None,
                cherrypy.request.path_info,
            )
            cherrypy.response.status = 403
            cherrypy.response.headers["Content-Type"] = "application/json"
            return {"success": False, "error": "Forbidden - Admin scope required"}

        return func(*args, **kwargs)

    return require_auth(admin_wrapper)
