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

logger = logging.getLogger("WebSocket")

# Suppress noisy ws4py error logs for normal disconnections (ConnectionResetError, etc.)
logging.getLogger("ws4py").setLevel(logging.CRITICAL)

# Global set of connected clients
_connected_clients = set()

# Heartbeat configuration
PING_INTERVAL = 30  # seconds
_heartbeat_thread = None
_heartbeat_running = False
_websocket_plugin = None


class PacketWebSocket(WebSocket):
    def opened(self):
        """Called when a WebSocket connection is established"""
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        qs = ""
        if hasattr(self, "environ"):
            qs = self.environ.get("QUERY_STRING", "")

        params = parse_qs(qs)
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

        if token:
            try:
                payload = jwt_handler.verify_jwt(token)
                if payload:
                    if (
                        client_id
                        and payload.get("client_id")
                        and payload.get("client_id") != client_id
                    ):
                        logger.warning("WebSocket connection rejected: client_id mismatch")
                        self.close(code=1008, reason="unauthorized")
                        return
                    self.user = payload.get("sub")
                    _connected_clients.add(self)
                    logger.info(
                        f"WebSocket connected ({self.user or 'unknown user'}). Total clients: {len(_connected_clients)}"
                    )
                    return
            except Exception as e:
                logger.warning(f"WebSocket JWT auth error: {e}")

        api_token = api_key or token
        if api_token and token_manager:
            try:
                token_info = token_manager.verify_token(api_token)
                if token_info:
                    self.user = f"api_token:{token_info.get('name', 'unknown')}"
                    _connected_clients.add(self)
                    logger.info(
                        f"WebSocket connected (API token: {token_info.get('name', 'unknown')}). Total clients: {len(_connected_clients)}"
                    )
                    return
            except Exception as e:
                logger.warning(f"WebSocket API key auth error: {e}")

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
        try:
            data = json.loads(str(message))
            if data.get("type") == "ping":
                self.send(json.dumps({"type": "pong"}))
            elif data.get("type") == "pong":
                # Client responded to our ping
                pass
        except Exception as exc:
            logger.debug(f"Ignoring malformed WebSocket message: {exc}")


def broadcast_packet(packet_data: dict):

    if not _connected_clients:
        return

    message = json.dumps({"type": "packet", "data": packet_data})

    for client in list(_connected_clients):
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
        try:
            client.send(message)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            _connected_clients.discard(client)


def has_connected_clients() -> bool:
    """Return True when at least one authenticated websocket client is connected."""
    return bool(_connected_clients)


def _heartbeat_loop():
    """Background thread to send periodic pings to all connected clients"""
    global _heartbeat_running

    while _heartbeat_running:
        time.sleep(PING_INTERVAL)

        if not _connected_clients:
            continue

        ping_message = json.dumps({"type": "ping"})

        for client in list(_connected_clients):
            try:
                client.send(ping_message)
            except Exception as e:
                logger.debug(f"Heartbeat ping failed: {e}")
                _connected_clients.discard(client)


def init_websocket():
    """Initialize WebSocket plugin and start heartbeat"""
    global _heartbeat_thread, _heartbeat_running, _websocket_plugin

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

    # Start heartbeat thread
    if not _heartbeat_running:
        _heartbeat_running = True
        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        _heartbeat_thread.start()
        logger.info(f"WebSocket initialized with {PING_INTERVAL}s heartbeat")
    else:
        logger.info("WebSocket initialized")


def shutdown_websocket():
    """Stop websocket heartbeat and unsubscribe plugin for clean restart."""
    global _heartbeat_running, _heartbeat_thread, _websocket_plugin

    _heartbeat_running = False
    _heartbeat_thread = None
    _connected_clients.clear()

    if _websocket_plugin is not None:
        try:
            _websocket_plugin.unsubscribe()
        except Exception as e:
            logger.debug(f"WebSocket plugin unsubscribe failed: {e}")
        _websocket_plugin = None
