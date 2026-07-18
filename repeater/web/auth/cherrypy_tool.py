import logging

import cherrypy

logger = logging.getLogger("HTTPServer")


def check_auth():
    """
    CherryPy tool to check authentication before processing request.

    Checks for either JWT in Authorization header, API token in X-API-Key header,
    or JWT token in query parameter (for EventSource/SSE connections).
    Sets cherrypy.request.user on success.
    Returns 401 JSON response on failure.
    """
    # Skip auth check for OPTIONS requests (CORS preflight)
    if cherrypy.request.method == "OPTIONS":
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

    # Check for JWT token in Authorization header first
    auth_header = cherrypy.request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove "Bearer " prefix
        payload = jwt_handler.verify_jwt(token)

        if payload:
            # Web UI operators are always 'admin' scope (design doc §11.1);
            # scopes are a mobile-device-token concept, not a JWT one.
            cherrypy.request.user = {
                "username": payload.get("sub"),
                "client_id": payload.get("client_id"),
                "auth_type": "jwt",
                "scope": "admin",
            }
            return

        # Not a valid JWT -- device API tokens may also be sent as a Bearer
        # value (design doc always allowed this transport).
        token_info = token_manager.verify_token(token)

        if token_info:
            # verify_token already NULL-defaults scope to 'admin' for
            # pre-migration tokens (design doc §11.1 backward compat).
            cherrypy.request.user = {
                "token_id": token_info["id"],
                "token_name": token_info["name"],
                "auth_type": "api_token",
                "scope": token_info.get("scope", "admin"),
            }
            return

    # Check for JWT token in query parameter (for EventSource/SSE)
    # EventSource doesn't support custom headers, so we use query param
    query_token = cherrypy.request.params.get("token")
    if query_token:
        payload = jwt_handler.verify_jwt(query_token)

        if payload:
            cherrypy.request.user = {
                "username": payload.get("sub"),
                "client_id": payload.get("client_id"),
                "auth_type": "jwt_query",
                "scope": "admin",
            }
            # Remove token from params to avoid exposing it in logs
            del cherrypy.request.params["token"]
            return

    # Check for API token in X-API-Key header
    api_key = cherrypy.request.headers.get("X-API-Key", "")
    if api_key:
        token_info = token_manager.verify_token(api_key)

        if token_info:
            # verify_token already NULL-defaults scope to 'admin' for
            # pre-migration tokens (design doc §11.1 backward compat).
            cherrypy.request.user = {
                "token_id": token_info["id"],
                "token_name": token_info["name"],
                "auth_type": "api_token",
                "scope": token_info.get("scope", "admin"),
            }
            return

    # No valid authentication found
    logger.warning(f"Unauthorized access attempt to {cherrypy.request.path_info}")
    raise cherrypy.HTTPError(401, "Unauthorized - Valid JWT or API token required")


def register_require_auth_tool():
    if not hasattr(cherrypy.tools, "require_auth"):
        cherrypy.tools.require_auth = cherrypy.Tool("before_handler", check_auth)
        logger.info("CherryPy require_auth tool registered")
