"""
WebSocket proxy for the companion frame protocol.

Bridges browser WebSocket to the companion TCP frame server.
Raw byte pipe — no parsing, all protocol logic lives in the client.

Authentication is operator-only: use a short-lived JWT in ``?token=`` or an
admin API token in ``X-API-Key``. Device-scoped tokens never enter this proxy.
"""

import ipaddress
import logging
import socket
import threading
from urllib.parse import parse_qs

import cherrypy
from ws4py.websocket import WebSocket

from repeater.companion.utils import validate_companion_registration_name
from repeater.data_acquisition.sqlite_handler import CompanionStorageError

from .auth.api_tokens import safe_api_token_name
from .auth.lease import (
    AUTHORIZATION_RECHECK_SECONDS,
    AuthorizationLease,
)
from .auth.policy import (
    api_token_scope,
    is_admin_scope,
    is_valid_bearer_token,
)

logger = logging.getLogger("CompanionWSProxy")

# Set by http_server.py before CherryPy starts
_daemon = None
_WS_QUERY_MAX_CHARS = 8192


def set_daemon(instance):
    global _daemon
    _daemon = instance


class CompanionFrameWebSocket(WebSocket):
    def opened(self):
        """Authenticate, resolve companion, open TCP socket, start reader."""
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        qs = ""
        if hasattr(self, "environ"):
            qs = self.environ.get("QUERY_STRING", "")

        if not isinstance(qs, str) or len(qs) > _WS_QUERY_MAX_CHARS:
            logger.warning("Connection rejected: invalid query")
            self.close(code=1008, reason="invalid query")
            return
        try:
            params = parse_qs(
                qs,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=3,
            )
        except ValueError:
            logger.warning("Connection rejected: invalid query")
            self.close(code=1008, reason="invalid query")
            return
        if (
            set(params) - {"token", "companion_name"}
            or any(len(params.get(name, ())) > 1 for name in params)
        ):
            logger.warning("Connection rejected: ambiguous query")
            self.close(code=1008, reason="invalid query")
            return
        token_parameter_present = "token" in params
        token = params.get("token", [None])[0]
        companion_name = params.get("companion_name", [None])[0]
        api_key = (
            self.environ.get("HTTP_X_API_KEY", "")
            if hasattr(self, "environ")
            else ""
        )

        if not jwt_handler:
            logger.warning("Connection rejected: no JWT handler configured")
            self.close(code=1011, reason="server configuration error")
            return

        if not token and not api_key:
            logger.warning("Connection rejected: missing credential")
            self.close(code=1008, reason="unauthorized")
            return
        if token_parameter_present and api_key:
            logger.warning("Connection rejected: multiple credentials")
            self.close(code=1008, reason="ambiguous credentials")
            return
        if token is not None and not is_valid_bearer_token(token):
            logger.warning("Connection rejected: malformed token")
            self.close(code=1008, reason="unauthorized")
            return
        if api_key and not is_valid_bearer_token(api_key):
            logger.warning("Connection rejected: malformed API key")
            self.close(code=1008, reason="unauthorized")
            return

        authenticated_user = None
        authorization = None
        jwt_auth_unavailable = False
        if token:
            try:
                payload = jwt_handler.verify_jwt(token)
                if payload:
                    authenticated_user = payload.get("sub", "unknown")
                    try:
                        authorization = AuthorizationLease.from_jwt_payload(payload)
                    except ValueError:
                        logger.error(
                            "JWT verifier returned a payload without a valid expiration"
                        )
                        jwt_auth_unavailable = True
                        authenticated_user = None
                    else:
                        if not authorization.is_active():
                            logger.warning("Connection rejected: JWT expired")
                            authenticated_user = None
                else:
                    # Query credentials are JWT-only. Long-lived API tokens
                    # use X-API-Key so they do not leak into URLs.
                    logger.warning("Connection rejected: invalid or non-JWT token")
            except Exception:
                logger.error("JWT authentication is unavailable")
                jwt_auth_unavailable = True

        if authenticated_user is None and api_key:
            if not token_manager:
                logger.error("API-token authentication is not configured")
                self.close(code=1011, reason="authentication unavailable")
                return
            try:
                token_info = token_manager.verify_token(api_key)
                if token_info:
                    scope = api_token_scope(token_info)
                    if not is_admin_scope(scope):
                        logger.warning(
                            "API token rejected: scope=%r token_id=%r",
                            scope,
                            token_info.get("id"),
                        )
                        self.close(code=1008, reason="forbidden")
                        return
                    token_name = safe_api_token_name(
                        token_info.get("name", "unknown")
                    )
                    authenticated_user = f"api_token:{token_name}"
                    authorization = AuthorizationLease.from_api_token(
                        token_info,
                        token_manager,
                    )
            except CompanionStorageError:
                logger.error("API-token authentication storage is unavailable")
                self.close(code=1011, reason="authentication unavailable")
                return
            except Exception:
                logger.error("API-token authentication is unavailable")
                self.close(code=1011, reason="authentication unavailable")
                return

        if authenticated_user is None:
            if jwt_auth_unavailable:
                self.close(code=1011, reason="authentication unavailable")
            else:
                self.close(code=1008, reason="unauthorized")
            return
        if authorization is None:
            logger.error("Connection authorization lease was not created")
            self.close(code=1011, reason="authentication unavailable")
            return

        if not companion_name:
            logger.warning("Connection rejected: missing companion_name")
            self.close(code=1008, reason="missing companion_name")
            return
        try:
            companion_name = validate_companion_registration_name(companion_name)
        except ValueError:
            logger.warning("Connection rejected: invalid companion_name")
            self.close(code=1008, reason="invalid companion_name")
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
            # create_connection resolves IPv4/IPv6 without guessing a family.
            self._tcp = socket.create_connection((tcp_host, tcp_port), timeout=5.0)
            logger.debug(f"TCP connected to {tcp_host}:{tcp_port} for '{companion_name}'")
        except Exception as e:
            logger.error(f"TCP connect failed for '{companion_name}' {tcp_host}:{tcp_port}: {e}")
            self._tcp = None
            self.close(code=1011, reason="TCP connect failed")
            return

        self._closing = False
        self._companion_name = companion_name
        self._authorization = authorization
        self._authorization_unavailable = False
        self._reader = threading.Thread(
            target=self._tcp_to_ws, daemon=True, name=f"ws-tcp-{companion_name}"
        )
        self._reader.start()

        logger.info(
            "Companion WS opened: user=%r, companion=%s, tcp=%s:%s",
            authenticated_user,
            companion_name,
            tcp_host,
            tcp_port,
        )

    def received_message(self, message):
        """WS → TCP"""
        tcp = getattr(self, "_tcp", None)
        if tcp is None or getattr(self, "_closing", True):
            return
        if not self._authorization_is_active():
            logger.info(
                "Closing companion WS after authorization ended for %r",
                getattr(self, "_companion_name", "?"),
            )
            self._teardown(*self._authorization_close_args())
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

    @staticmethod
    def _bound_listener_endpoint(server):
        """Return one numeric endpoint from the listener's actual socket.

        ``bind_address`` may be a hostname. Re-resolving it here would let DNS
        changes redirect this authenticated raw-frame proxy away from the
        local listener after startup. The bound socket is the authoritative
        runtime endpoint; wildcard sockets remain reachable through loopback.
        """
        listener = getattr(server, "_server", None)
        sockets = tuple(getattr(listener, "sockets", ()) or ())
        candidates = []
        for listener_socket in sockets:
            family = getattr(listener_socket, "family", None)
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                sockname = listener_socket.getsockname()
            except OSError:
                continue
            if not isinstance(sockname, tuple) or len(sockname) < 2:
                continue
            host, port = sockname[0], sockname[1]
            if not isinstance(host, str) or type(port) is not int:
                continue
            try:
                address = ipaddress.ip_address(host.split("%", 1)[0])
            except ValueError:
                # INET getsockname() should always be numeric. Fail closed if
                # an unusual socket wrapper violates that contract.
                continue
            if address.is_unspecified:
                host = "127.0.0.1" if address.version == 4 else "::1"
                address = ipaddress.ip_address(host)
            if not 1 <= port <= 65_535:
                continue
            # Prefer loopback when a hostname produced multiple bound sockets.
            # Otherwise either numeric address is the same local listener.
            candidates.append((not address.is_loopback, address.version, host, port))

        if not candidates:
            return None
        _, _, host, port = min(candidates)
        return (host, port)

    def _resolve_tcp_endpoint(self, companion_name):
        """Look up the actually running companion Frame listener.

        Returns ``(host, port)`` tuple or ``None`` if the companion can't be
        resolved. Mutable restart-required config must not redirect the proxy
        away from the listener that is live now.
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

        # Find the companion identity by name and verify its bridge is running.
        companion_hash = None
        for name, identity, _cfg in identity_manager.get_identities_by_type("companion"):
            if name == companion_name:
                h = identity.get_public_key()[0]
                if h in bridges:
                    companion_hash = h
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

        if companion_hash is None:
            return None

        companion_hash_str = f"0x{companion_hash:02x}"
        for server in getattr(_daemon, "companion_frame_servers", ()):
            if getattr(server, "companion_hash", None) != companion_hash_str:
                continue
            endpoint = self._bound_listener_endpoint(server)
            if endpoint is None:
                logger.warning(
                    "_resolve_tcp_endpoint: running '%s' has no usable bound "
                    "INET socket",
                    companion_name,
                )
                return None
            host, port = endpoint
            logger.debug(
                "_resolve_tcp_endpoint: running '%s' -> %s:%s",
                companion_name,
                host,
                port,
            )
            return (host, port)

        logger.warning(
            "_resolve_tcp_endpoint: '%s' has a running bridge but no Frame listener",
            companion_name,
        )
        return None

    def _tcp_to_ws(self):
        """TCP → WS reader loop"""
        name = getattr(self, "_companion_name", "?")
        tcp = getattr(self, "_tcp", None)
        if tcp is None:
            return
        close_args = ()
        try:
            while not getattr(self, "_closing", True):
                if not self._authorization_is_active():
                    logger.info(
                        "Closing companion WS after authorization ended for %r",
                        name,
                    )
                    close_args = self._authorization_close_args()
                    break
                authorization = getattr(self, "_authorization", None)
                wait_for = authorization.check_in(
                    AUTHORIZATION_RECHECK_SECONDS
                )
                tcp.settimeout(max(0.001, wait_for))
                try:
                    data = tcp.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    logger.info(f"TCP→WS: frame server closed connection for '{name}'")
                    break
                if not self._authorization_is_active():
                    logger.info(
                        "Closing companion WS after authorization ended for %r",
                        name,
                    )
                    close_args = self._authorization_close_args()
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
            self._teardown(*close_args)

    def _authorization_is_active(self) -> bool:
        authorization = getattr(self, "_authorization", None)
        self._authorization_unavailable = False
        if authorization is None:
            logger.error("Companion WS authorization lease is missing")
            self._authorization_unavailable = True
            return False
        try:
            return authorization.is_active()
        except CompanionStorageError:
            logger.error("Companion WS authorization storage is unavailable")
            self._authorization_unavailable = True
            return False
        except Exception:
            logger.error("Companion WS authorization recheck failed")
            self._authorization_unavailable = True
            return False

    def _authorization_close_args(self) -> tuple:
        if getattr(self, "_authorization_unavailable", False):
            return (1011, "authentication unavailable")
        return (1008, "authorization expired or revoked")

    def _teardown(self, code=None, reason=None):
        if getattr(self, "_closing", True):
            return
        self._closing = True

        name = getattr(self, "_companion_name", "?")
        logger.debug(f"Tearing down WS proxy for '{name}'")

        tcp = getattr(self, "_tcp", None)
        if tcp:
            try:
                tcp.close()
            except Exception as exc:
                logger.debug(f"WS proxy TCP close failed for {name!r}: {exc}")
            self._tcp = None

        try:
            if code is None:
                self.close()
            else:
                self.close(code=code, reason=reason)
        except Exception as exc:
            logger.debug(f"WS proxy close failed for {name!r}: {exc}")
