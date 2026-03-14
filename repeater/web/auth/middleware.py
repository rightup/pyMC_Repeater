import logging
from functools import wraps

import cherrypy

logger = logging.getLogger(__name__)


def require_auth(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Skip authentication for OPTIONS requests (CORS preflight)
        if cherrypy.request.method == "OPTIONS":
            return func(*args, **kwargs)

        # Get auth handlers from global cherrypy config (not app config)
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        if not jwt_handler or not token_manager:
            logger.error("Auth handlers not configured")
            raise cherrypy.HTTPError(500, "Authentication not configured")

        # Try JWT authentication first
        auth_header = cherrypy.request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            payload = jwt_handler.verify_jwt(token)

            if payload:
                # JWT is valid
                cherrypy.request.user = {
                    "username": payload["sub"],
                    "client_id": payload["client_id"],
                    "auth_type": "jwt",
                }
                return func(*args, **kwargs)
            else:
                logger.warning("Invalid or expired JWT token")

        # Try API token authentication
        api_key = cherrypy.request.headers.get("X-API-Key", "")
        if api_key:
            token_info = token_manager.verify_token(api_key)

            if token_info:
                # API token is valid
                cherrypy.request.user = {
                    "username": "api_token",
                    "token_name": token_info["name"],
                    "token_id": token_info["id"],
                    "auth_type": "api_token",
                }
                return func(*args, **kwargs)
            else:
                logger.warning("Invalid API token")

        # No valid authentication found
        logger.warning(f"Unauthorized access attempt to {cherrypy.request.path_info}")

        cherrypy.response.status = 401
        cherrypy.response.headers["Content-Type"] = "application/json"
        return {"success": False, "error": "Unauthorized - Valid JWT or API token required"}

    return wrapper
