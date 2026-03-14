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
        cherrypy.response.status = 500
        return {"success": False, "error": "Authentication system not configured"}
    
    # Check for JWT token in Authorization header first
    auth_header = cherrypy.request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove "Bearer " prefix
        payload = jwt_handler.verify_jwt(token)
        
        if payload:
            cherrypy.request.user = {
                "username": payload.get("sub"),
                "client_id": payload.get("client_id"),
                "auth_type": "jwt",
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
            }
            # Remove token from params to avoid exposing it in logs
            del cherrypy.request.params["token"]
            return
    
    # Check for API token in X-API-Key header
    api_key = cherrypy.request.headers.get("X-API-Key", "")
    if api_key:
        token_info = token_manager.verify_token(api_key)
        
        if token_info:
            cherrypy.request.user = {
                "token_id": token_info["id"],
                "token_name": token_info["name"],
                "auth_type": "api_token",
            }
            return

    # No valid authentication found
    logger.warning(f"Unauthorized access attempt to {cherrypy.request.path_info}")
    raise cherrypy.HTTPError(401, "Unauthorized - Valid JWT or API token required")


# Register the tool
cherrypy.tools.require_auth = cherrypy.Tool("before_handler", check_auth)
logger.info("CherryPy require_auth tool registered")
