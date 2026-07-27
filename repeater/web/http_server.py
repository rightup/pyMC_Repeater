import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cherrypy
import cherrypy_cors

from repeater.config import resolve_storage_dir
from repeater.data_acquisition import SQLiteHandler

from .api_endpoints import APIEndpoints
from .auth.api_tokens import APITokenManager
from .auth.cherrypy_tool import register_require_auth_tool
from .auth.jwt_handler import JWTHandler
from .auth_endpoints import AuthEndpoints

# WebSocket support
try:
    from repeater.data_acquisition.websocket_handler import (
        PacketWebSocket,
        init_websocket,
        shutdown_websocket,
    )

    from .companion_ws_proxy import CompanionFrameWebSocket
    from .companion_ws_proxy import set_daemon as _set_companion_daemon

    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger = logging.getLogger("HTTPServer")
    logger.warning("ws4py not available - WebSocket support disabled")

logger = logging.getLogger("HTTPServer")
_ORIGINAL_UNRAISABLEHOOK = sys.unraisablehook
_CHEROOT_UNRAISABLE_HOOK_INSTALLED = False


def _looks_like_cheroot_makefile_context(unraisable: object) -> bool:
    context = (
        f"{getattr(unraisable, 'object', '')!r} {getattr(unraisable, 'err_msg', '')!r}".lower()
    )
    return "cheroot" in context and "makefile" in context


def _install_cheroot_bad_fd_unraisable_filter() -> None:
    global _CHEROOT_UNRAISABLE_HOOK_INSTALLED
    if _CHEROOT_UNRAISABLE_HOOK_INSTALLED:
        return

    def _filtered_unraisablehook(unraisable):
        exc = getattr(unraisable, "exc_value", None)
        if (
            isinstance(exc, OSError)
            and getattr(exc, "errno", None) == 9
            and "bad file descriptor" in str(exc).lower()
            and _looks_like_cheroot_makefile_context(unraisable)
        ):
            return
        _ORIGINAL_UNRAISABLEHOOK(unraisable)

    sys.unraisablehook = _filtered_unraisablehook
    _CHEROOT_UNRAISABLE_HOOK_INSTALLED = True


# In-memory log buffer
class LogBuffer(logging.Handler):
    _SECRET_PATTERNS = (
        re.compile(
            r"(?i)\b(admin_password|guest_password|password|passwd|api[_-]?key|token|jwt_secret)\b(\s*[:=]\s*)(['\"]?)([^,'\"\s]+)(['\"]?)"
        ),
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]+"),
    )

    def __init__(self, max_lines=100):
        super().__init__()
        self.logs = deque(maxlen=max_lines)
        self._next_id = 1
        self._lock = threading.Lock()
        self._subscribers = []
        self.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

    @classmethod
    def _sanitize_log_text(cls, text: str) -> str:
        if not text:
            return ""

        sanitized = text

        def _replace_secret(match: re.Match) -> str:
            key = match.group(1)
            sep = match.group(2)
            quote_start = match.group(3) or ""
            quote_end = match.group(5) or quote_start
            return f"{key}{sep}{quote_start}[REDACTED]{quote_end}"

        sanitized = cls._SECRET_PATTERNS[0].sub(_replace_secret, sanitized)
        sanitized = cls._SECRET_PATTERNS[1].sub("Bearer [REDACTED]", sanitized)
        return sanitized

    def emit(self, record):

        try:
            formatted_message = self._sanitize_log_text(self.format(record))
            entry = {
                "id": self._next_log_id(),
                "message": formatted_message,
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "module": record.module,
                "pathname": record.pathname,
                "line": record.lineno,
                "thread": record.threadName,
                "process": record.processName,
            }

            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                entry["exception"] = self._sanitize_log_text(
                    formatter.formatException(record.exc_info)
                )

            with self._lock:
                self.logs.append(entry)
                dead_subscribers = []
                for subscriber in self._subscribers:
                    try:
                        subscriber.put_nowait(entry)
                    except Exception:
                        dead_subscribers.append(subscriber)

                if dead_subscribers:
                    self._subscribers = [
                        subscriber
                        for subscriber in self._subscribers
                        if subscriber not in dead_subscribers
                    ]
        except Exception:
            self.handleError(record)

    def _next_log_id(self):
        with self._lock:
            next_id = self._next_id
            self._next_id += 1
            return next_id

    def snapshot(self, since_id=None):
        with self._lock:
            records = list(self.logs)

        if since_id is None:
            return records

        return [record for record in records if record.get("id", 0) > since_id]

    def subscribe(self):
        subscriber = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscriber]


# Global log buffer instance
_log_buffer = LogBuffer(max_lines=1000)


class DocEndpoint:
    """Simple wrapper to serve API docs at /doc"""

    def __init__(self, api_endpoints):
        self.api_endpoints = api_endpoints

    @cherrypy.expose
    def index(self, **kwargs):
        """Serve Swagger UI at /doc"""
        return self.api_endpoints.docs()

    @cherrypy.expose
    def docs(self):
        """Serve Swagger UI at /doc/docs"""
        return self.api_endpoints.docs()

    @cherrypy.expose
    def openapi_json(self):
        """Serve OpenAPI spec in JSON format at /doc/openapi.json"""
        import json
        import os

        import yaml

        spec_path = os.path.join(os.path.dirname(__file__), "openapi.yaml")
        try:
            with open(spec_path, "r") as f:
                spec_content = yaml.safe_load(f)

            cherrypy.response.headers["Content-Type"] = "application/json"
            return json.dumps(spec_content).encode("utf-8")
        except FileNotFoundError:
            cherrypy.response.status = 404
            return json.dumps({"error": "OpenAPI spec not found"}).encode("utf-8")
        except Exception as e:
            cherrypy.response.status = 500
            return json.dumps({"error": f"Error loading OpenAPI spec: {e}"}).encode("utf-8")


class StatsApp:
    def __init__(
        self,
        stats_getter: Optional[Callable] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.stats_getter = stats_getter
        self.node_name = node_name
        self.pub_key = pub_key
        self.dashboard_template = None
        self.config = config or {}
        self.default_html_dir = os.path.join(os.path.dirname(__file__), "html")

        # Path to the compiled Vue.js application
        # Use web_path from config if provided, otherwise use default
        web_path = self.config.get("web", {}).get("web_path")
        self.html_dir = (
            web_path if web_path is not None and os.path.isdir(web_path) else self.default_html_dir
        )

        # Create nested API object for routing
        self.api = APIEndpoints(
            stats_getter, send_advert_func, self.config, event_loop, daemon_instance, config_path
        )

        # Create doc endpoint for API documentation
        self.doc = DocEndpoint(self.api)

    def _resolve_html_dir(self) -> str:
        web_path = self.config.get("web", {}).get("web_path")
        candidate = (
            web_path if web_path is not None and os.path.isdir(web_path) else self.default_html_dir
        )
        self.html_dir = candidate
        return candidate

    def apply_web_config(self) -> bool:
        previous = self.html_dir
        current = self._resolve_html_dir()
        return previous != current

    def _serve_static_file(self, root_dir: str, relative_parts: tuple[str, ...]):
        if not relative_parts:
            raise cherrypy.NotFound()
        root = Path(root_dir).resolve()
        target = (root.joinpath(*relative_parts)).resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            raise cherrypy.NotFound()
        guessed_type, _ = mimetypes.guess_type(str(target))
        cherrypy.response.headers["Content-Type"] = guessed_type or "application/octet-stream"
        return target.read_bytes()

    @cherrypy.expose
    def index(self, **kwargs):
        """Serve the Vue.js application index.html."""
        self._resolve_html_dir()
        index_path = os.path.join(self.html_dir, "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise cherrypy.HTTPError(404, "Application not found. Please build the frontend first.")
        except Exception as e:
            logger.error(f"Error serving index.html: {e}")
            raise cherrypy.HTTPError(500, "Internal server error")

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle client-side routing - serve index.html for all non-API routes."""
        self._resolve_html_dir()
        # Handle OPTIONS requests for any path
        if cherrypy.request.method == "OPTIONS":
            return ""

        # Let API routes pass through
        if args and args[0] == "api":
            raise cherrypy.NotFound()

        # Handle WebSocket routes
        if (
            args
            and len(args) >= 2
            and args[0] == "ws"
            and args[1] in ("packets", "companion_frame")
        ):
            # WebSocket tool will intercept this
            return ""
        # Serve frontend static assets dynamically from active html_dir
        if args and args[0] == "assets":
            return self._serve_static_file(os.path.join(self.html_dir, "assets"), tuple(args[1:]))

        if args and args[0] == "_next":
            return self._serve_static_file(os.path.join(self.html_dir, "_next"), tuple(args[1:]))

        if args and args[0] == "favicon.ico":
            return self._serve_static_file(self.html_dir, ("favicon.ico",))

        # For all other routes, serve the Vue.js app (client-side routing)
        return self.index()


class HTTPStatsServer:
    def __init__(
        self,
        host: str = "0.0.0.0",  # nosec B104 - intentional default for service exposure
        port: int = 8000,
        stats_getter: Optional[Callable] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.host = host
        self.port = port
        self.config = config or {}
        self.config_path = config_path
        self.daemon_instance = daemon_instance

        # Initialize authentication handlers
        self._init_auth_handlers()

        self.app = StatsApp(
            stats_getter,
            node_name,
            pub_key,
            send_advert_func,
            config,
            event_loop,
            daemon_instance,
            config_path,
        )

        # Create auth endpoints (APIEndpoints has the config_manager)
        self.auth_app = AuthEndpoints(
            self.config, self.jwt_handler, self.token_manager, self.app.api.config_manager
        )

        # Create documentation endpoints as separate app
        self.doc_app = DocEndpoint(self.app.api)

        # Set up CORS at the server level if enabled
        self._cors_enabled = self.config.get("web", {}).get("cors_enabled", False)
        logger.info(f"CORS enabled: {self._cors_enabled}")

    def _init_auth_handlers(self):
        """Initialize JWT handler and API token manager."""
        # Get or generate JWT secret from repeater.security
        repeater_config = self.config.get("repeater", {})
        security_config = repeater_config.get("security", {})
        jwt_secret = security_config.get("jwt_secret", "")

        if not jwt_secret:
            # Auto-generate JWT secret
            jwt_secret = secrets.token_hex(32)
            logger.warning(
                "No JWT secret found in config, auto-generated one. Please save this to config.yaml:"
            )

            # Try to save to config if config_path is available
            if self.config_path:
                try:
                    import yaml

                    with open(self.config_path, "r") as f:
                        config_data = yaml.safe_load(f) or {}

                    if "repeater" not in config_data:
                        config_data["repeater"] = {}
                    if "security" not in config_data["repeater"]:
                        config_data["repeater"]["security"] = {}
                    config_data["repeater"]["security"]["jwt_secret"] = jwt_secret

                    with open(self.config_path, "w") as f:
                        yaml.dump(config_data, f, default_flow_style=False)

                    logger.info(f"Saved auto-generated JWT secret to {self.config_path}")
                except Exception as e:
                    logger.error(f"Failed to save JWT secret to config: {e}")

        # Initialize JWT handler with configurable expiry (default 1 hour)
        jwt_expiry_minutes = security_config.get("jwt_expiry_minutes", 60)
        self.jwt_handler = JWTHandler(jwt_secret, expiry_minutes=jwt_expiry_minutes)
        logger.info(f"JWT handler initialized (token expiry: {jwt_expiry_minutes} minutes)")

        # Initialize API token manager
        storage_dir = resolve_storage_dir(self.config, config_path=self.config_path)

        # Ensure storage directory exists
        os.makedirs(storage_dir, exist_ok=True)

        # Initialize SQLiteHandler and APITokenManager
        self.sqlite_handler = SQLiteHandler(Path(storage_dir))
        self.token_manager = APITokenManager(self.sqlite_handler, jwt_secret)
        logger.info(f"API token manager initialized with database at {storage_dir}/repeater.db")

    def _setup_server_cors(self):
        """Set up CORS using cherrypy_cors.install()"""
        # Configure CORS to allow Authorization header
        # cherrypy-cors will handle preflight requests automatically
        cherrypy_cors.install()

        logger.info("CORS support enabled with Authorization header")

    def _json_error_handler(self, status, message, traceback, version):
        """Return JSON error responses instead of HTML for API endpoints"""
        cherrypy.response.headers["Content-Type"] = "application/json"
        return json.dumps({"success": False, "error": message})

    def start(self):

        try:
            _install_cheroot_bad_fd_unraisable_filter()
            register_require_auth_tool()

            if self._cors_enabled:
                self._setup_server_cors()

            self.app.apply_web_config()

            # Build config with conditional CORS settings
            config = {
                "/": {
                    "tools.sessions.on": False,
                    # "tools.gzip.on": True,
                    # "tools.gzip.mime_types": ["application/json", "text/html", "text/plain"],
                    # Ensure proper content types for static files
                    "tools.staticfile.content_types": {
                        "js": "application/javascript",
                        "css": "text/css",
                        "html": "text/html; charset=utf-8",
                        "svg": "image/svg+xml",
                        "txt": "text/plain",
                    },
                },
                # Require authentication for all /api endpoints
                "/api": {
                    "tools.require_auth.on": True,
                },
                # Enable gzip for bulk packet downloads
                "/api/bulk_packets": {
                    "tools.gzip.on": True,
                    "tools.gzip.mime_types": ["application/json"],
                    "tools.gzip.compress_level": 6,
                },
                # Public documentation endpoints (no auth required)
                "/api/openapi": {
                    "tools.require_auth.on": False,
                },
                "/api/docs": {
                    "tools.require_auth.on": False,
                },
                # Public setup wizard endpoints (no auth required)
                "/api/needs_setup": {
                    "tools.require_auth.on": False,
                },
                "/api/site_info": {
                    "tools.require_auth.on": False,
                },
                "/api/hardware_options": {
                    "tools.require_auth.on": False,
                },
                "/api/radio_presets": {
                    "tools.require_auth.on": False,
                },
                "/api/serial_ports": {
                    "tools.require_auth.on": False,
                },
                "/api/setup_wizard": {
                    "tools.require_auth.on": False,
                },
                "/api/config_import": {
                    "tools.require_auth.on": False,
                },
            }

            # Add WebSocket configuration to main config if available
            if WEBSOCKET_AVAILABLE:
                try:
                    init_websocket()
                    config["/ws/packets"] = {
                        "tools.websocket.on": True,
                        "tools.websocket.handler_cls": PacketWebSocket,
                        "tools.trailing_slash.on": False,
                        "tools.require_auth.on": False,
                        "tools.gzip.on": False,
                    }
                    logger.info("WebSocket endpoint configured at /ws/packets")

                    # Companion frame proxy (binary WS ↔ TCP byte pipe)
                    if self.daemon_instance:
                        _set_companion_daemon(self.daemon_instance)
                        config["/ws/companion_frame"] = {
                            "tools.websocket.on": True,
                            "tools.websocket.handler_cls": CompanionFrameWebSocket,
                            "tools.trailing_slash.on": False,
                            "tools.require_auth.on": False,
                            "tools.gzip.on": False,
                        }
                        logger.info("WebSocket endpoint configured at /ws/companion_frame")
                except Exception as e:
                    logger.error(f"Failed to initialize WebSocket: {e}")
                    import traceback

                    logger.error(traceback.format_exc())

            # Add CORS configuration if enabled
            if self._cors_enabled:
                cors_config = {
                    "cors.expose.on": True,
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                        ("Access-Control-Allow-Credentials", "true"),
                    ],
                    # Disable automatic trailing slash redirects to prevent CORS issues
                    "tools.trailing_slash.on": False,
                }

                # Apply CORS to paths
                config["/"].update(cors_config)
                config["/api"].update(cors_config)

            http_cfg = self.config.get("http", {}) if isinstance(self.config, dict) else {}
            thread_pool = max(2, int(http_cfg.get("thread_pool", 8)))
            thread_pool_max = max(thread_pool, int(http_cfg.get("thread_pool_max", 16)))
            socket_timeout = max(15, int(http_cfg.get("socket_timeout", 65)))
            socket_queue_size = max(10, int(http_cfg.get("socket_queue_size", 100)))

            cherrypy.config.update(
                {
                    "server.socket_host": self.host,
                    "server.socket_port": self.port,
                    "server.socket_queue_size": socket_queue_size,
                    "engine.autoreload.on": False,
                    "log.screen": False,
                    "log.access_file": "",  # Disable access log file
                    "log.error_file": "",  # Disable error log file
                    # Disable automatic trailing slash redirects globally
                    "tools.trailing_slash.on": False,
                    # Custom error handler to return JSON for API endpoints
                    "error_page.401": self._json_error_handler,
                    # Add auth handlers to config so they're accessible in endpoints
                    "jwt_handler": self.jwt_handler,
                    "token_manager": self.token_manager,
                    # Bound the thread pool to prevent unbounded growth.
                    # SSE streams each hold one thread; allow headroom for concurrent
                    # SSE clients plus normal API polling without growing unboundedly.
                    "server.thread_pool": thread_pool,
                    "server.thread_pool_max": thread_pool_max,
                    # Close idle/stale connections so their threads return to the pool.
                    "server.socket_timeout": socket_timeout,
                }
            )
            logger.info(
                "HTTP worker config: thread_pool=%s, thread_pool_max=%s, socket_timeout=%ss, socket_queue_size=%s",
                thread_pool,
                thread_pool_max,
                socket_timeout,
                socket_queue_size,
            )

            # Mount main app
            cherrypy.tree.mount(self.app, "/", config)

            # Mount auth endpoints
            auth_config = {
                "/": {
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Content-Type", "application/json"),
                    ],
                    # Disable automatic trailing slash redirects
                    "tools.trailing_slash.on": False,
                }
            }
            if self._cors_enabled:
                auth_config["/"]["cors.expose.on"] = True
                # Add CORS headers for OPTIONS requests
                auth_config["/"]["tools.response_headers.headers"].extend(
                    [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                        ("Access-Control-Allow-Credentials", "true"),
                    ]
                )

            cherrypy.tree.mount(self.auth_app, "/auth", auth_config)

            # Mount documentation endpoints as separate app (no auth required for docs)
            doc_config = {
                "/": {
                    "tools.require_auth.on": False,  # Docs are publicly accessible
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Content-Type", "text/html; charset=utf-8"),
                    ],
                    "tools.trailing_slash.on": False,
                }
            }
            if self._cors_enabled:
                doc_config["/"]["cors.expose.on"] = True
                doc_config["/"]["tools.response_headers.headers"].extend(
                    [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                    ]
                )

            cherrypy.tree.mount(self.doc_app, "/doc", doc_config)

            # Store auth handlers in cherrypy config for middleware access
            cherrypy.config.update(
                {
                    "jwt_handler": self.jwt_handler,
                    "token_manager": self.token_manager,
                    "security_config": self.config.get("security", {}),
                }
            )

            # Completely disable access logging
            cherrypy.log.access_log.propagate = False
            cherrypy.log.error_log.setLevel(logging.ERROR)

            cherrypy.engine.start()
            server_url = "http://{}:{}".format(self.host, self.port)
            logger.info(f"HTTP stats server started on {server_url}")

        except Exception as e:
            logger.error(f"Failed to start HTTP server: {e}")
            raise

    def stop(self):
        try:
            if WEBSOCKET_AVAILABLE:
                try:
                    shutdown_websocket()
                except Exception as e:
                    logger.debug(f"WebSocket shutdown skipped/failed: {e}")
            cherrypy.engine.exit()
            logger.info("HTTP stats server stopped")
        except Exception as e:
            logger.warning(f"Error stopping HTTP server: {e}")
