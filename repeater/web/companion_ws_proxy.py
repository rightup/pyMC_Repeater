"""
WebSocket proxy for the companion frame protocol.

Bridges browser WebSocket to the companion TCP frame server.
Raw byte pipe — no parsing, all protocol logic lives in the client.
"""

import logging
import socket
import threading
from urllib.parse import parse_qs

import cherrypy
from ws4py.websocket import WebSocket

logger = logging.getLogger("CompanionWSProxy")

# Set by http_server.py before CherryPy starts
_daemon = None


def set_daemon(instance):
    global _daemon
    _daemon = instance


class CompanionFrameWebSocket(WebSocket):

    def opened(self):
        """Authenticate, resolve companion, open TCP socket, start reader."""
        # JWT auth — same pattern as PacketWebSocket
        jwt_handler = cherrypy.config.get("jwt_handler")

        qs = ""
        if hasattr(self, "environ"):
            qs = self.environ.get("QUERY_STRING", "")

        params = parse_qs(qs)
        token = params.get("token", [None])[0]
        companion_name = params.get("companion_name", [None])[0]

        if not jwt_handler:
            logger.warning("Connection rejected: no JWT handler configured")
            self.close(code=1011, reason="server configuration error")
            return

        if not token:
            logger.warning("Connection rejected: missing token")
            self.close(code=1008, reason="unauthorized")
            return

        try:
            payload = jwt_handler.verify_jwt(token)
            if not payload:
                logger.warning("Connection rejected: invalid token")
                self.close(code=1008, reason="unauthorized")
                return
        except Exception as e:
            logger.warning(f"Auth error: {e}")
            self.close(code=1008, reason="unauthorized")
            return

        if not companion_name:
            logger.warning("Connection rejected: missing companion_name")
            self.close(code=1008, reason="missing companion_name")
            return

        # Resolve companion TCP port + bind address from config
        resolved = self._resolve_tcp_endpoint(companion_name)
        if resolved is None:
            logger.warning(f"Connection rejected: companion '{companion_name}' not found")
            self.close(code=1008, reason="companion not found")
            return

        tcp_host, tcp_port = resolved

        # Open TCP socket to the companion frame server
        try:
            self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tcp.settimeout(5.0)
            self._tcp.connect((tcp_host, tcp_port))
            self._tcp.settimeout(None)
            logger.debug(f"TCP connected to {tcp_host}:{tcp_port} for '{companion_name}'")
        except Exception as e:
            logger.error(f"TCP connect failed for '{companion_name}' {tcp_host}:{tcp_port}: {e}")
            self._tcp = None
            self.close(code=1011, reason="TCP connect failed")
            return

        self._closing = False
        self._companion_name = companion_name
        self._reader = threading.Thread(
            target=self._tcp_to_ws, daemon=True, name=f"ws-tcp-{companion_name}"
        )
        self._reader.start()

        user = payload.get("sub", "unknown")
        logger.info(f"Companion WS opened: user={user}, companion={companion_name}, tcp={tcp_host}:{tcp_port}")

    def received_message(self, message):
        """WS → TCP"""
        tcp = getattr(self, "_tcp", None)
        if tcp is None or getattr(self, "_closing", True):
            return
        try:
            data = message.data
            if isinstance(data, str):
                data = data.encode("latin-1")
            tcp.sendall(data)
        except Exception as e:
            name = getattr(self, "_companion_name", "?")
            logger.warning(f"WS→TCP send failed for '{name}': {e}")
            self._teardown()

    def closed(self, code, reason=None):
        name = getattr(self, "_companion_name", "?")
        logger.info(f"Companion WS closed: companion={name}, code={code}, reason={reason}")
        self._teardown()

    # ── internal ─────────────────────────────────────────────────────────

    def _resolve_tcp_endpoint(self, companion_name):
        """Look up companion TCP host + port from daemon config.

        Returns ``(host, port)`` tuple or ``None`` if the companion can't be
        resolved.  When ``bind_address`` is ``0.0.0.0`` (all interfaces) we
        connect via ``127.0.0.1``; otherwise we use the configured address.
        """
        if not _daemon:
            logger.warning("_resolve_tcp_endpoint: daemon not set")
            return None

        identity_manager = getattr(_daemon, "identity_manager", None)
        bridges = getattr(_daemon, "companion_bridges", {})

        if not identity_manager:
            logger.warning("_resolve_tcp_endpoint: no identity_manager")
            return None
        if not bridges:
            logger.warning("_resolve_tcp_endpoint: no companion_bridges (dict empty or missing)")
            return None

        # Find the companion identity by name and verify its bridge is running
        found = False
        for name, identity, _cfg in identity_manager.get_identities_by_type("companion"):
            if name == companion_name:
                h = identity.get_public_key()[0]
                if h in bridges:
                    found = True
                else:
                    logger.warning(
                        f"_resolve_tcp_endpoint: companion '{companion_name}' identity found "
                        f"(hash=0x{h:02x}) but no bridge registered for that hash. "
                        f"Known bridge hashes: {[f'0x{k:02x}' for k in bridges.keys()]}"
                    )
                break
        else:
            # Loop completed without finding the name
            known = [n for n, _, _ in identity_manager.get_identities_by_type("companion")]
            logger.warning(
                f"_resolve_tcp_endpoint: companion '{companion_name}' not in identity_manager. "
                f"Known companions: {known}"
            )

        if not found:
            return None

        # Look up TCP port + bind address from config
        companions = _daemon.config.get("identities", {}).get("companions") or []
        for entry in companions:
            if entry.get("name") == companion_name:
                settings = entry.get("settings") or {}
                port = settings.get("tcp_port", 5000)
                bind = settings.get("bind_address", "0.0.0.0")
                # 0.0.0.0 = all interfaces — connect via loopback
                host = "127.0.0.1" if bind == "0.0.0.0" else bind
                logger.debug(f"_resolve_tcp_endpoint: '{companion_name}' → {host}:{port}")
                return (host, port)

        logger.warning(
            f"_resolve_tcp_endpoint: '{companion_name}' found in identity_manager but missing from config"
        )
        return None

    def _tcp_to_ws(self):
        """TCP → WS reader loop"""
        name = getattr(self, "_companion_name", "?")
        tcp = getattr(self, "_tcp", None)
        if tcp is None:
            return
        try:
            while not getattr(self, "_closing", True):
                data = tcp.recv(4096)
                if not data:
                    logger.info(f"TCP→WS: frame server closed connection for '{name}'")
                    break
                try:
                    self.send(data, binary=True)
                except Exception as e:
                    logger.warning(f"TCP→WS: WS send failed for '{name}': {e}")
                    break
        except OSError as e:
            # Socket error (connection reset, etc.) — normal during teardown
            if not getattr(self, "_closing", True):
                logger.warning(f"TCP→WS: socket error for '{name}': {e}")
        except Exception as e:
            logger.warning(f"TCP→WS: unexpected error for '{name}': {e}")
        finally:
            self._teardown()

    def _teardown(self):
        if getattr(self, "_closing", True):
            return
        self._closing = True

        name = getattr(self, "_companion_name", "?")
        logger.debug(f"Tearing down WS proxy for '{name}'")

        tcp = getattr(self, "_tcp", None)
        if tcp:
            try:
                tcp.close()
            except Exception:
                pass
            self._tcp = None

        try:
            self.close()
        except Exception:
            pass
