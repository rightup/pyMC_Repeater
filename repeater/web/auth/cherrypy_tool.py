import logging

import cherrypy

from repeater.data_acquisition.sqlite_handler import CompanionStorageError

from .jwt_handler import verify_jwt_for_auth_fallback
from .lease import forget_jwt_expiration, remember_jwt_expiration
from .policy import (
    allows_query_jwt,
    api_token_scope,
    bearer_token_from_header,
    has_multiple_v1_credential_transports,
    is_known_scope,
    is_valid_bearer_token,
    scope_allows_api_path,
)

logger = logging.getLogger("HTTPServer")

_AUTH_TOOL_REQUEST_MARKER = object()


def request_was_authorized_by_tool() -> bool:
    """Return whether the API-tree tool authenticated this exact request."""

    return getattr(cherrypy.request, "_openhop_auth_tool_marker", None) is _AUTH_TOOL_REQUEST_MARKER


def _authorize(user, *, jwt_payload=None):
    """Apply the API-tree boundary after a credential has authenticated."""

    scope = user.get("scope")
    path = cherrypy.request.path_info
    if is_known_scope(scope) and scope_allows_api_path(scope, path):
        cherrypy.request.user = user
        forget_jwt_expiration(cherrypy.request)
        if jwt_payload is not None:
            remember_jwt_expiration(cherrypy.request, jwt_payload)
        # Many API handlers retain their upstream ``@require_auth`` decorator
        # even though the mounted /api tree is already protected by this tool.
        # A process-private identity marker lets that decorator reuse this
        # completed decision without verifying the credential twice.  Merely
        # setting ``request.user`` is intentionally insufficient.
        cherrypy.request._openhop_auth_tool_marker = _AUTH_TOOL_REQUEST_MARKER
        return

    logger.warning(
        "Forbidden API request (scope=%r, token_id=%r, path=%s)",
        scope,
        user.get("token_id"),
        path,
    )
    raise cherrypy.HTTPError(403, "Token scope is not allowed for this endpoint")


def _api_token_user(token_info):
    return {
        "username": "api_token",
        "token_id": token_info["id"],
        "token_name": token_info["name"],
        "auth_type": "api_token",
        "scope": api_token_scope(token_info),
    }


def _verify_api_token(token_manager, token):
    """Verify a presented API token without disguising storage outages as 401."""

    try:
        return token_manager.verify_token(token)
    except CompanionStorageError as exc:
        logger.error("API-token authentication storage is unavailable")
        raise cherrypy.HTTPError(503, "Authentication storage unavailable") from exc


def check_auth():
    """
    CherryPy tool to check authentication before processing request.

    Checks for either JWT in Authorization header, API token in X-API-Key header,
    or JWT token in query parameter (for EventSource/SSE connections).
    Sets cherrypy.request.user on success.
    Returns 401 JSON response on failure.
    """
    # OPTIONS never reaches protected endpoint code.  The safe-CORS tool adds
    # allowlist headers when configured; this remains a harmless empty
    # response for same-origin and non-browser callers.
    if cherrypy.request.method == "OPTIONS":
        cherrypy.response.status = 204
        # ``None`` remains safe when a route has CherryPy's json_out tool;
        # returning bytes here would make that tool raise during finalization.
        cherrypy.request.handler = lambda: None
        return

    # Skip auth check for /auth/login endpoint
    if cherrypy.request.path_info == "/auth/login":
        return

    # Get auth handlers from config
    jwt_handler = cherrypy.config.get("jwt_handler")
    token_manager = cherrypy.config.get("token_manager")

    if not jwt_handler or not token_manager:
        logger.error("Auth handlers not initialized in cherrypy.config")
        raise cherrypy.HTTPError(500, "Authentication system not configured")

    request_params = getattr(cherrypy.request, "params", None)
    query_token_present = request_params is not None and "token" in request_params
    query_token = request_params.pop("token", None) if request_params is not None else None

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

    # Check for JWT token in Authorization header first
    token = bearer_token_from_header(auth_header)
    if token is not None:
        payload = verify_jwt_for_auth_fallback(jwt_handler, token)

        if payload:
            # Web UI operators are always 'admin' scope (design doc §11.1);
            # scopes are a mobile-device-token concept, not a JWT one.
            _authorize(
                {
                    "username": payload.get("sub"),
                    "client_id": payload.get("client_id"),
                    "auth_type": "jwt",
                    "scope": "admin",
                },
                jwt_payload=payload,
            )
            return

        # Not a valid JWT -- device API tokens may also be sent as a Bearer
        # value (design doc always allowed this transport).
        token_info = _verify_api_token(token_manager, token)

        if token_info:
            _authorize(_api_token_user(token_info))
            return

    # Strip query credentials before dispatch.  A JWT may authenticate this
    # request only on the exact Mobile v1 EventSource route.
    if is_valid_bearer_token(query_token) and allows_query_jwt(
        cherrypy.request.method,
        cherrypy.request.path_info,
    ):
        payload = jwt_handler.verify_jwt(query_token)

        if payload:
            _authorize(
                {
                    "username": payload.get("sub"),
                    "client_id": payload.get("client_id"),
                    "auth_type": "jwt_query",
                    "scope": "admin",
                },
                jwt_payload=payload,
            )
            return
    elif query_token:
        logger.warning(
            "Rejected query-string credential (method=%s, path=%s)",
            cherrypy.request.method,
            cherrypy.request.path_info,
        )

    # Check for API token in X-API-Key header
    if is_valid_bearer_token(api_key):
        token_info = _verify_api_token(token_manager, api_key)

        if token_info:
            _authorize(_api_token_user(token_info))
            return
    elif api_key:
        logger.warning("Rejected malformed API token")

    # No valid authentication found
    logger.warning(f"Unauthorized access attempt to {cherrypy.request.path_info}")
    raise cherrypy.HTTPError(401, "Unauthorized - Valid JWT or API token required")


def register_require_auth_tool():
    if not hasattr(cherrypy.tools, "require_auth"):
        cherrypy.tools.require_auth = cherrypy.Tool("before_handler", check_auth)
        logger.info("CherryPy require_auth tool registered")
