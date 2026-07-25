import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sys
import tempfile
import threading
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cherrypy
import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - production targets are POSIX.
    fcntl = None

from repeater.config import resolve_storage_dir
from repeater.data_acquisition import SQLiteHandler

from .api_endpoints import APIEndpoints
from .auth.api_tokens import APITokenManager
from .auth.cherrypy_tool import register_require_auth_tool
from .auth.jwt_handler import (
    JWT_SECRET_MIN_BYTES as _JWT_SECRET_MIN_BYTES,
    JWTHandler,
    validate_jwt_expiry_minutes as _jwt_expiry_minutes,
    validate_jwt_signing_secret as _jwt_signing_secret,
)
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
_CORS_METHODS = ("GET", "POST", "PUT", "DELETE", "OPTIONS")
_CORS_HEADERS = frozenset(
    {
        "authorization",
        "content-type",
        "idempotency-key",
        "last-event-id",
        "x-api-key",
    }
)
_CORS_EXPOSE_HEADERS = ("ETag", "Idempotency-Replayed", "Retry-After")
_JWT_SECRET_THREAD_LOCK = threading.Lock()


def _cors_origins(config: dict) -> tuple[str, ...]:
    """Return validated, exact browser origins from ``web.cors_origins``."""

    web_config = config.get("web", {}) if isinstance(config, dict) else {}
    raw = web_config.get("cors_origins", ())
    if isinstance(raw, str):
        raw = [raw]
    origins = []
    for value in raw if isinstance(raw, (list, tuple)) else ():
        origin = str(value).strip().rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(origin)
            port = parsed.port
        except ValueError:
            logger.warning("Ignoring invalid CORS origin: %r", value)
            continue
        if (
            origin == "*"
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            and not 1 <= port <= 65_535
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            logger.warning("Ignoring invalid CORS origin: %r", value)
            continue
        origins.append(origin)
    return tuple(dict.fromkeys(origins))


def _append_vary_origin(headers) -> None:
    values = [item.strip() for item in headers.get("Vary", "").split(",") if item.strip()]
    if "Origin" not in values:
        values.append("Origin")
    headers["Vary"] = ", ".join(values)


def _safe_cors(origins: tuple[str, ...]) -> None:
    """Serve preflight and response headers for an explicit origin allowlist."""

    origin = cherrypy.request.headers.get("Origin")
    if not origin:
        return
    if origin not in origins:
        if cherrypy.request.method == "OPTIONS":
            raise cherrypy.HTTPError(403, "Origin is not allowed")
        return

    response_headers = cherrypy.response.headers
    response_headers["Access-Control-Allow-Origin"] = origin
    response_headers["Access-Control-Allow-Methods"] = ", ".join(_CORS_METHODS)
    response_headers["Access-Control-Allow-Headers"] = ", ".join(
        sorted(header.title() for header in _CORS_HEADERS)
    )
    response_headers["Access-Control-Expose-Headers"] = ", ".join(
        _CORS_EXPOSE_HEADERS
    )
    response_headers["Access-Control-Max-Age"] = "600"
    _append_vary_origin(response_headers)

    if cherrypy.request.method != "OPTIONS":
        return
    requested_method = cherrypy.request.headers.get("Access-Control-Request-Method", "").upper()
    if requested_method and requested_method not in _CORS_METHODS:
        raise cherrypy.HTTPError(405, "CORS method is not allowed")
    requested_headers = {
        item.strip().lower()
        for item in cherrypy.request.headers.get("Access-Control-Request-Headers", "").split(",")
        if item.strip()
    }
    if not requested_headers.issubset(_CORS_HEADERS):
        raise cherrypy.HTTPError(400, "CORS header is not allowed")
    cherrypy.response.status = 204
    cherrypy.request.handler = lambda: b""


def _register_safe_cors_tool() -> None:
    if not hasattr(cherrypy.tools, "safe_cors"):
        cherrypy.tools.safe_cors = cherrypy.Tool(
            "before_handler",
            _safe_cors,
            priority=20,
        )


def _default_api_no_store() -> None:
    """Keep authenticated API responses out of shared/browser caches by default."""

    cherrypy.response.headers.setdefault("Cache-Control", "no-store")


def _register_api_no_store_tool() -> None:
    if not hasattr(cherrypy.tools, "api_no_store"):
        cherrypy.tools.api_no_store = cherrypy.Tool(
            "before_finalize",
            _default_api_no_store,
            priority=80,
        )


def _persist_generated_jwt_secret_locked(
    config_file: Path,
    jwt_secret: str,
) -> str:
    """Read, compare, and atomically update one config while its lock is held."""

    try:
        with config_file.open("r", encoding="utf-8") as config_stream:
            config_data = yaml.safe_load(config_stream) or {}
    except Exception as exc:
        raise RuntimeError(
            "repeater.security.jwt_secret is missing and the config file "
            f"cannot be read safely: {config_file}: {exc}"
        ) from exc

    if not isinstance(config_data, dict):
        raise RuntimeError(f"Configuration root must be an object: {config_file}")
    repeater_config = config_data.setdefault("repeater", {})
    if not isinstance(repeater_config, dict):
        raise RuntimeError("Configuration field repeater must be an object")
    security_config = repeater_config.setdefault("security", {})
    if not isinstance(security_config, dict):
        raise RuntimeError("Configuration field repeater.security must be an object")

    persisted_secret = security_config.get("jwt_secret")
    # Empty string was the historical example-config sentinel for
    # auto-generation. Preserve that safe migration path; whitespace and
    # every other weak explicit value still fail closed.
    if persisted_secret not in (None, ""):
        try:
            return _jwt_signing_secret(persisted_secret)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    try:
        jwt_secret = _jwt_signing_secret(jwt_secret)
    except ValueError as exc:
        raise RuntimeError(f"Generated {exc}") from exc

    security_config["jwt_secret"] = jwt_secret
    temporary_path: str | None = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{config_file.name}.",
            suffix=".tmp",
            dir=str(config_file.parent),
            text=True,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
            yaml.safe_dump(
                config_data,
                temporary,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=1000000,
            )
            temporary.flush()
            os.fsync(temporary.fileno())

        os.replace(temporary_path, config_file)
        temporary_path = None

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(config_file.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception as exc:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise RuntimeError(
            "repeater.security.jwt_secret is missing and could not be persisted "
            f"durably to {config_file}: {exc}"
        ) from exc

    logger.info("Saved auto-generated JWT secret to %s", config_file)
    return jwt_secret


def _persist_generated_jwt_secret(config_path: object, jwt_secret: str) -> str:
    """Generate exactly one durable secret across threads and processes.

    The on-disk YAML is the source document humans edit. A stable sidecar lock
    serializes the entire read/compare/replace transaction, so concurrent
    starters either persist the first strong candidate or reuse that winner.
    """

    if not isinstance(config_path, (str, os.PathLike)) or not str(config_path):
        raise RuntimeError(
            "repeater.security.jwt_secret is missing and no config path is available "
            "for durable persistence"
        )
    try:
        jwt_secret = _jwt_signing_secret(jwt_secret)
    except ValueError as exc:
        raise RuntimeError(f"Generated {exc}") from exc

    requested_path = Path(config_path).expanduser()
    try:
        config_file = requested_path.resolve(strict=True)
    except Exception as exc:
        raise RuntimeError(
            "repeater.security.jwt_secret is missing and the config file "
            f"cannot be resolved safely: {requested_path}: {exc}"
        ) from exc

    lock_path = config_file.with_name(f".{config_file.name}.jwt-secret.lock")
    if fcntl is None:
        raise RuntimeError(
            "repeater.security.jwt_secret is missing and automatic generation "
            "cannot be process-safe on this platform; configure an explicit "
            f"secret containing at least {_JWT_SECRET_MIN_BYTES} UTF-8 bytes"
        )
    lock_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with _JWT_SECRET_THREAD_LOCK:
        try:
            lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        except Exception as exc:
            raise RuntimeError(
                "repeater.security.jwt_secret is missing and its persistence "
                f"lock cannot be opened safely: {lock_path}: {exc}"
            ) from exc

        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            except Exception as exc:
                raise RuntimeError(
                    "repeater.security.jwt_secret is missing and its persistence "
                    f"lock cannot be acquired: {lock_path}: {exc}"
                ) from exc
            return _persist_generated_jwt_secret_locked(config_file, jwt_secret)
        finally:
            # Closing the descriptor releases flock even when the transaction
            # raises, without risking a cleanup error masking the real failure.
            os.close(lock_descriptor)


def _cors_response_headers(
    methods: str = "GET, POST, PUT, DELETE, OPTIONS",
) -> list[tuple[str, str]]:
    """Return wildcard CORS headers for header-authenticated API requests.

    Browser credentials are intentionally disabled: wildcard origins cannot be
    combined with Access-Control-Allow-Credentials.
    """
    return [
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", methods),
        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
    ]


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


def _json_error_page_v1(status, message, traceback, version):
    """error_page.default handler for the /api/v1 tree.

    CherryPy's default error page renders HTML for any status that doesn't
    have its own error_page.<code> entry (see cherrypy._cperror.get_error_page:
    ``pages.get(code) or pages.get('default')``). The Mobile Companion API v1
    (design doc §7.1) needs a consistent JSON envelope for every error, not
    just 401s, so this covers everything else (400/403/404/500/...) for
    requests under /api/v1. It intentionally mirrors HTTPStatsServer's
    error_page.401 handler's response shape.
    """
    # CherryPy passes status as e.g. "404 Not Found"; clients get the bare
    # code (openapi.yaml documents ErrorResponseV1.status as an integer).
    try:
        status_code = int(str(status).split(" ", 1)[0])
    except ValueError:
        status_code = status
    cherrypy.response.headers["Content-Type"] = "application/json"
    return json.dumps({"success": False, "error": message, "status": status_code})


# In-memory log buffer
class LogBuffer(logging.Handler):
    _SECRET_PATTERNS = (
        re.compile(
            r"""(?ix)
            (?P<key_quote>['"]?)
            (?P<key>\b(
                admin_password|guest_password|password|passwd|
                api[_-]?key|token|push_token|jwt_secret|
                identity_key|private_key|pairing_code|secret|psk
            )\b)
            (?P=key_quote)
            (?P<separator>\s*[:=]\s*)
            (?P<value>
                "(?:\\.|[^"\\])*"|
                '(?:\\.|[^'\\])*'|
                [^,\s;&}\]]+
            )
            """
        ),
        re.compile(r"(?i)\bBearer\s+[^\s,'\"]+"),
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
            key_quote = match.group("key_quote") or ""
            key = match.group("key")
            separator = match.group("separator")
            value = match.group("value")
            value_quote = (
                value[0]
                if len(value) >= 2
                and value[0] in {'"', "'"}
                and value[-1] == value[0]
                else ""
            )
            return (
                f"{key_quote}{key}{key_quote}{separator}"
                f"{value_quote}[REDACTED]{value_quote}"
            )

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
_log_buffer = LogBuffer(max_lines=300)


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
        self.config = config if config is not None else {}
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
    def favicon_ico(self):
        """Serve the favicon bundled with the compiled frontend."""
        self._resolve_html_dir()
        return self._serve_static_file(self.html_dir, ("favicon.ico",))

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
        self.config = config if config is not None else {}
        self.config_path = config_path
        self.daemon_instance = daemon_instance

        # Initialize authentication handlers
        self._init_auth_handlers()

        self.app = StatsApp(
            stats_getter,
            node_name,
            pub_key,
            send_advert_func,
            self.config,
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
        self._cors_origins = _cors_origins(self.config)
        if self._cors_enabled and not self._cors_origins:
            logger.warning(
                "CORS requested but web.cors_origins has no valid origins; "
                "cross-origin browser access remains disabled"
            )
            self._cors_enabled = False
        logger.info(
            "CORS enabled: %s (origins=%s)",
            self._cors_enabled,
            len(self._cors_origins),
        )

    def _init_auth_handlers(self):
        """Initialize JWT handler and API token manager."""
        repeater_config = self.config.setdefault("repeater", {})
        if not isinstance(repeater_config, dict):
            raise ValueError("repeater must be an object")
        security_config = repeater_config.setdefault("security", {})
        if not isinstance(security_config, dict):
            raise ValueError("repeater.security must be an object")

        # Validate the full auth policy before generating or persisting anything.
        jwt_expiry_minutes = _jwt_expiry_minutes(
            security_config.get("jwt_expiry_minutes", 60)
        )
        security_config.setdefault("jwt_expiry_minutes", jwt_expiry_minutes)

        configured_secret = security_config.get("jwt_secret")
        if configured_secret in (None, ""):
            generated_secret = secrets.token_hex(32)
            jwt_secret = _persist_generated_jwt_secret(
                self.config_path,
                generated_secret,
            )
            # ConfigManager holds this same dictionary. Updating it prevents a
            # later settings save from erasing the just-persisted credential.
            security_config["jwt_secret"] = jwt_secret
        else:
            jwt_secret = _jwt_signing_secret(configured_secret)

        self.jwt_handler = JWTHandler(jwt_secret, expiry_minutes=jwt_expiry_minutes)
        logger.info(f"JWT handler initialized (token expiry: {jwt_expiry_minutes} minutes)")

        # Initialize API token manager
        storage_dir = resolve_storage_dir(self.config, config_path=self.config_path)

        # The daemon already owns the canonical SQLite handler used by Frame
        # and the companion journal. Reuse it so bringing up (or rebuilding)
        # the HTTP surface cannot run startup recovery against a live send.
        repeater_handler = getattr(
            getattr(self, "daemon_instance", None),
            "repeater_handler",
            None,
        )
        storage = getattr(repeater_handler, "storage", None)
        self.sqlite_handler = getattr(storage, "sqlite_handler", None)
        if self.sqlite_handler is None:
            # Standalone embeddings and focused tests do not have a daemon.
            os.makedirs(storage_dir, exist_ok=True)
            self.sqlite_handler = SQLiteHandler(Path(storage_dir))
        self.token_manager = APITokenManager(self.sqlite_handler, jwt_secret)
        logger.info(
            "API token manager initialized with shared database at %s/repeater.db",
            storage_dir,
        )

    def _setup_server_cors(self):
        """Install the exact-origin CORS hook."""

        _register_safe_cors_tool()
        logger.info("CORS support enabled for %d exact origin(s)", len(self._cors_origins))

    def _json_error_handler(self, status, message, traceback, version):
        """Return JSON error responses instead of HTML for API endpoints"""
        cherrypy.response.headers["Content-Type"] = "application/json"
        return json.dumps({"success": False, "error": message})

    def start(self):

        try:
            _install_cheroot_bad_fd_unraisable_filter()
            register_require_auth_tool()
            _register_api_no_store_tool()

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
                    "tools.api_no_store.on": True,
                    "error_page.default": self._json_error_handler,
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
                # Mobile Companion API v1 public entry points (design doc
                # §7.1, §11.2): server_info is the unauthenticated discovery
                # endpoint an app validates a scanned URL against before it
                # has any credential. This config path also covers
                # "/api/v1/pair/start" (CherryPy config cascades to
                # descendants) even though that endpoint is admin-only —
                # it carries its own @require_auth decorator for that
                # reason (see PairV1.start's docstring in mobile_endpoints.py).
                "/api/v1": {
                    # JSON error envelope for the whole v1 tree. Only fires
                    # for statuses without their own error_page.<code> entry;
                    # the global error_page.401 above still wins for 401s
                    # (get_error_page checks pages.get(code) before
                    # pages.get('default')), but that handler is JSON too.
                    "error_page.default": _json_error_page_v1,
                },
                "/api/v1/server_info": {
                    "tools.require_auth.on": False,
                },
                "/api/v1/pair": {
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
                    "tools.safe_cors.on": True,
                    "tools.safe_cors.origins": self._cors_origins,
                    "tools.trailing_slash.on": False,
                }

                config["/"].update(cors_config)

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
                        ("Cache-Control", "no-store"),
                    ],
                    # Disable automatic trailing slash redirects
                    "tools.trailing_slash.on": False,
                }
            }
            if self._cors_enabled:
                auth_config["/"]["tools.safe_cors.on"] = True
                auth_config["/"]["tools.safe_cors.origins"] = self._cors_origins

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
                doc_config["/"]["tools.safe_cors.on"] = True
                doc_config["/"]["tools.safe_cors.origins"] = self._cors_origins

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
