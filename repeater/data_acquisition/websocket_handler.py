"""
WebSocket handler for real-time packet updates - simple ws4py implementation
"""

import json
import logging
import threading
import time
from urllib.parse import parse_qs

import cherrypy
from ws4py.server.cherrypyserver import WebSocketPlugin, WebSocketTool
from ws4py.websocket import WebSocket

from repeater.data_acquisition.sqlite_handler import CompanionStorageError
from repeater.web.auth.api_tokens import safe_api_token_name
from repeater.web.auth.jwt_handler import verify_jwt_for_auth_fallback
from repeater.web.auth.lease import (
    AUTHORIZATION_RECHECK_SECONDS,
    AuthorizationLease,
)
from repeater.web.auth.policy import (
    api_token_scope,
    is_admin_scope,
    is_valid_bearer_token,
)

logger = logging.getLogger("WebSocket")

# Suppress noisy ws4py error logs for normal disconnections (ConnectionResetError, etc.)
logging.getLogger("ws4py").setLevel(logging.CRITICAL)

# Global set of connected clients
_connected_clients = set()

# Heartbeat configuration
PING_INTERVAL = 30  # seconds
_heartbeat_thread = None
_heartbeat_running = False
_heartbeat_stop = None
_websocket_plugin = None
_WS_QUERY_MAX_CHARS = 8192


class PacketWebSocket(WebSocket):
    def opened(self):
        """Called when a WebSocket connection is established"""
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        qs = ""
        if hasattr(self, "environ"):
            qs = self.environ.get("QUERY_STRING", "")

        if not isinstance(qs, str) or len(qs) > _WS_QUERY_MAX_CHARS:
            logger.warning("WebSocket connection rejected: invalid query")
            self.close(code=1008, reason="invalid query")
            return
        try:
            params = parse_qs(
                qs,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
            )
        except ValueError:
            logger.warning("WebSocket connection rejected: invalid query")
            self.close(code=1008, reason="invalid query")
            return
        if set(params) - {"token", "client_id"} or any(
            len(params.get(name, ())) > 1 for name in params
        ):
            logger.warning("WebSocket connection rejected: ambiguous query")
            self.close(code=1008, reason="invalid query")
            return
        token_parameter_present = "token" in params
        token = params.get("token", [None])[0]
        client_id = params.get("client_id", [None])[0]

        api_key = self.environ.get("HTTP_X_API_KEY", "") if hasattr(self, "environ") else ""

        if not jwt_handler:
            logger.warning("WebSocket connection rejected: no JWT handler configured")
            self.close(code=1011, reason="server configuration error")
            return

        if not token and not api_key:
            logger.warning("WebSocket connection rejected: missing token")
            self.close(code=1008, reason="unauthorized")
            return
        if token_parameter_present and api_key:
            logger.warning("WebSocket connection rejected: multiple credentials")
            self.close(code=1008, reason="ambiguous credentials")
            return
        if token is not None and not is_valid_bearer_token(token):
            logger.warning("WebSocket connection rejected: malformed token")
            self.close(code=1008, reason="unauthorized")
            return
        if api_key and not is_valid_bearer_token(api_key):
            logger.warning("WebSocket connection rejected: malformed API key")
            self.close(code=1008, reason="unauthorized")
            return
        if client_id is not None:
            client_id = client_id.strip()
            try:
                client_id_size = len(client_id.encode("utf-8"))
            except UnicodeEncodeError:
                client_id_size = 129
            if (
                not client_id
                or client_id_size > 128
                or any(not character.isprintable() for character in client_id)
            ):
                logger.warning("WebSocket connection rejected: invalid client_id")
                self.close(code=1008, reason="unauthorized")
                return

        jwt_auth_unavailable = False
        if token:
            try:
                payload = verify_jwt_for_auth_fallback(jwt_handler, token)
                if payload:
                    if (
                        client_id
                        and payload.get("client_id")
                        and payload.get("client_id") != client_id
                    ):
                        logger.warning("WebSocket connection rejected: client_id mismatch")
                        self.close(code=1008, reason="unauthorized")
                        return
                    try:
                        authorization = AuthorizationLease.from_jwt_payload(payload)
                    except ValueError:
                        logger.error(
                            "WebSocket JWT verifier returned a payload without a valid expiration"
                        )
                        self.close(
                            code=1011,
                            reason="authentication unavailable",
                        )
                        return
                    if not authorization.is_active():
                        self.close(code=1008, reason="unauthorized")
                        return
                    self.user = payload.get("sub")
                    self._authorization = authorization
                    self._authorization_unavailable = False
                    _connected_clients.add(self)
                    logger.info(
                        f"WebSocket connected ({self.user or 'unknown user'}). Total clients: {len(_connected_clients)}"
                    )
                    return
            except Exception:
                logger.error("WebSocket JWT authentication is unavailable")
                jwt_auth_unavailable = True

        # Prefer X-API-Key for long-lived API tokens so they do not leak into
        # URLs. Keep the historic ?token=<api-token> fallback for existing
        # packet-WebSocket clients after JWT verification has rejected it.
        # Both forms receive the same admin-scope and authorization-lease
        # checks below.
        api_token = api_key or (token if not jwt_auth_unavailable else None)
        legacy_query_api_token = bool(token and not api_key)
        if api_token and not token_manager:
            logger.error("WebSocket API-token authentication is not configured")
            self.close(code=1011, reason="authentication unavailable")
            return
        if api_token:
            try:
                token_info = token_manager.verify_token(api_token)
                if token_info:
                    scope = api_token_scope(token_info)
                    if not is_admin_scope(scope):
                        logger.warning(
                            "WebSocket API token rejected: scope=%r token_id=%r",
                            scope,
                            token_info.get("id"),
                        )
                        self.close(code=1008, reason="forbidden")
                        return
                    token_name = safe_api_token_name(token_info.get("name", "unknown"))
                    self.user = f"api_token:{token_name}"
                    self._authorization = AuthorizationLease.from_api_token(
                        token_info,
                        token_manager,
                    )
                    self._authorization_unavailable = False
                    _connected_clients.add(self)
                    if legacy_query_api_token:
                        logger.warning(
                            "Packet WebSocket accepted a legacy API token in "
                            "the token query parameter; use X-API-Key instead"
                        )
                    logger.info(
                        "WebSocket connected (API token: %r). Total clients: %d",
                        token_name,
                        len(_connected_clients),
                    )
                    return
            except CompanionStorageError:
                logger.error("WebSocket API-token authentication storage is unavailable")
                self.close(code=1011, reason="authentication unavailable")
                return
            except Exception:
                logger.error("WebSocket API-token authentication is unavailable")
                self.close(code=1011, reason="authentication unavailable")
                return

        if jwt_auth_unavailable:
            self.close(code=1011, reason="authentication unavailable")
            return

        logger.warning("WebSocket connection rejected: no valid authentication")
        self.close(code=1008, reason="unauthorized")

    def closed(self, code, reason=None):
        """Called when a WebSocket connection is closed"""
        _connected_clients.discard(self)
        user = getattr(self, "user", "unknown")
        logger.info(
            f"WebSocket disconnected (user: {user}, code: {code}, reason: {reason}). Total clients: {len(_connected_clients)}"
        )

    def received_message(self, message):
        """Handle messages from client"""
        if not self._authorization_is_active():
            self._close_for_authorization()
            return
        try:
            data = json.loads(str(message))
            if data.get("type") == "ping":
                self.send(json.dumps({"type": "pong"}))
            elif data.get("type") == "pong":
                # Client responded to our ping
                pass
        except Exception as exc:
            logger.debug(f"Ignoring malformed WebSocket message: {exc}")

    def _authorization_is_active(self) -> bool:
        authorization = getattr(self, "_authorization", None)
        self._authorization_unavailable = False
        if authorization is None:
            logger.error("Packet WebSocket authorization lease is missing")
            self._authorization_unavailable = True
            return False
        try:
            return authorization.is_active()
        except CompanionStorageError:
            logger.error("Packet WebSocket authorization storage is unavailable")
            self._authorization_unavailable = True
            return False
        except Exception:
            logger.error("Packet WebSocket authorization recheck failed")
            self._authorization_unavailable = True
            return False

    def _close_for_authorization(self) -> None:
        _connected_clients.discard(self)
        if getattr(self, "_authorization_unavailable", False):
            self.close(code=1011, reason="authentication unavailable")
        else:
            self.close(code=1008, reason="authorization expired or revoked")


def broadcast_packet(packet_data: dict):

    if not _connected_clients:
        return

    message = json.dumps({"type": "packet", "data": packet_data})

    for client in list(_connected_clients):
        if not client._authorization_is_active():
            client._close_for_authorization()
            continue
        try:
            client.send(message)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            _connected_clients.discard(client)


def broadcast_stats(stats_data: dict):

    if not _connected_clients:
        return

    message = json.dumps({"type": "stats", "data": stats_data})

    for client in list(_connected_clients):
        if not client._authorization_is_active():
            client._close_for_authorization()
            continue
        try:
            client.send(message)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            _connected_clients.discard(client)


def has_connected_clients() -> bool:
    """Return True when at least one authenticated websocket client is connected."""
    return bool(_connected_clients)


def _heartbeat_loop(stop_event: threading.Event):
    """Recheck authorization every 15s while preserving 30s wire pings."""
    global _heartbeat_running

    next_ping = time.monotonic() + PING_INTERVAL
    try:
        while not stop_event.wait(AUTHORIZATION_RECHECK_SECONDS):
            now = time.monotonic()
            send_ping = now >= next_ping
            if send_ping:
                next_ping = now + PING_INTERVAL
            ping_message = json.dumps({"type": "ping"}) if send_ping else None
            for client in list(_connected_clients):
                if not client._authorization_is_active():
                    client._close_for_authorization()
                    continue
                if ping_message is None:
                    continue
                try:
                    client.send(ping_message)
                except Exception as e:
                    logger.debug(f"Heartbeat ping failed: {e}")
                    _connected_clients.discard(client)
    finally:
        if threading.current_thread() is _heartbeat_thread:
            _heartbeat_running = False


def init_websocket():
    """Initialize WebSocket plugin and start heartbeat"""
    global _heartbeat_stop, _heartbeat_thread, _heartbeat_running, _websocket_plugin

    # Re-initialize plugin safely across CherryPy stop/start cycles.
    # ws4py's manager thread cannot be started twice, so always tear down
    # any previously subscribed plugin instance before creating a new one.
    if _websocket_plugin is not None:
        try:
            _websocket_plugin.unsubscribe()
        except Exception as e:
            logger.debug(f"WebSocket plugin unsubscribe during init failed: {e}")
        _websocket_plugin = None

    _websocket_plugin = WebSocketPlugin(cherrypy.engine)
    _websocket_plugin.subscribe()
    cherrypy.tools.websocket = WebSocketTool()

    # Start a heartbeat for this server generation. A prior shutdown can
    # leave its old thread finishing a slow authorization/storage call after
    # the one-second join bound. Its stop event is already set, so it must not
    # suppress the replacement heartbeat on an immediate HTTP restart.
    heartbeat_stopping = _heartbeat_stop is not None and _heartbeat_stop.is_set()
    heartbeat_alive = _heartbeat_thread is not None and _heartbeat_thread.is_alive()
    if not _heartbeat_running or not heartbeat_alive or heartbeat_stopping:
        _heartbeat_stop = threading.Event()
        _heartbeat_running = True
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(_heartbeat_stop,),
            daemon=True,
        )
        _heartbeat_thread.start()
        logger.info(f"WebSocket initialized with {PING_INTERVAL}s heartbeat")
    else:
        logger.info("WebSocket initialized")


def shutdown_websocket():
    """Stop websocket heartbeat and unsubscribe plugin for clean restart."""
    global _heartbeat_stop, _heartbeat_running, _heartbeat_thread, _websocket_plugin

    if _heartbeat_stop is not None:
        _heartbeat_stop.set()
    heartbeat_thread = _heartbeat_thread
    if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
        heartbeat_thread.join(timeout=1.0)
        if heartbeat_thread.is_alive():
            logger.warning("WebSocket heartbeat thread did not stop promptly")
    if heartbeat_thread is None or not heartbeat_thread.is_alive():
        _heartbeat_running = False
        _heartbeat_thread = None
        _heartbeat_stop = None
    _connected_clients.clear()

    if _websocket_plugin is not None:
        try:
            _websocket_plugin.unsubscribe()
        except Exception as e:
            logger.debug(f"WebSocket plugin unsubscribe failed: {e}")
        _websocket_plugin = None
