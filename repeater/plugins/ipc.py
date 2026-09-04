"""JSON-lines Unix domain socket IPC between Repeater and the plugin manager."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .manager import PluginManager, PluginManagerError

logger = logging.getLogger("PluginIPC")

DEFAULT_TIMEOUT = 30.0
MAX_LINE_BYTES = 8 * 1024 * 1024  # install paths are short; keep headroom


class PluginIPCError(Exception):
    def __init__(self, message: str, code: int = 500):
        super().__init__(message)
        self.code = code


class PluginManagerUnavailable(PluginIPCError):
    def __init__(self, message: str = "plugin manager unavailable"):
        super().__init__(message, code=503)


def _read_line(conn: socket.socket, max_bytes: int = MAX_LINE_BYTES) -> bytes:
    buf = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
        if len(buf) > max_bytes:
            raise PluginIPCError("IPC message too large", 413)
    if not buf:
        return b""
    # Take first line only
    line, _, _rest = bytes(buf).partition(b"\n")
    return line


def _send_msg(conn: socket.socket, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    conn.sendall(data)


class PluginIPCServer:
    """Serve plugin manager operations over a Unix domain socket."""

    def __init__(self, socket_path: Path | str, manager: PluginManager):
        self.socket_path = Path(socket_path)
        self.manager = manager
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list": self._op_list,
            "status": self._op_status,
            "install": self._op_install,
            "enable": self._op_enable,
            "disable": self._op_disable,
            "start": self._op_start,
            "stop": self._op_stop,
            "restart": self._op_restart,
            "logs": self._op_logs,
            "uninstall": self._op_uninstall,
            "ui_info": self._op_ui_info,
            "get_config": self._op_get_config,
            "set_config": self._op_set_config,
            "catalogue": self._op_catalogue,
            "catalogue_install": self._op_catalogue_install,
            "check_update": self._op_check_update,
            "update": self._op_update,
            "ping": self._op_ping,
        }

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as exc:
                logger.warning("Could not remove stale socket %s: %s", self.socket_path, exc)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        try:
            # Repeater and the manager may be separate processes in one service
            # group, so the local IPC socket is intentionally group-writable.
            os.chmod(self.socket_path, 0o660)  # nosec B103
        except OSError as exc:
            logger.debug("Could not set IPC socket permissions: %s", exc)
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve_loop, name="plugin-ipc", daemon=True)
        self._thread.start()
        logger.info("Plugin manager IPC listening on %s", self.socket_path)

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    def _serve_loop(self) -> None:
        sock = self._sock
        if sock is None:
            logger.error("IPC serve loop started without a socket")
            return
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                self._handle_connection(conn)
            except Exception as exc:
                logger.debug("IPC connection error: %s", exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(DEFAULT_TIMEOUT)
        raw = _read_line(conn)
        if not raw:
            return
        req_id: Any = None
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise PluginIPCError("request must be a JSON object", 400)
            req_id = request.get("id")
            op = request.get("op")
            if not isinstance(op, str) or op not in self._handlers:
                raise PluginIPCError(f"unknown op: {op!r}", 400)
            result = self._handlers[op](request)
            _send_msg(conn, {"id": req_id, "ok": True, "result": result})
        except PluginManagerError as exc:
            _send_msg(
                conn,
                {"id": req_id, "ok": False, "error": str(exc), "code": int(exc.code)},
            )
        except PluginIPCError as exc:
            _send_msg(
                conn,
                {"id": req_id, "ok": False, "error": str(exc), "code": int(exc.code)},
            )
        except Exception as exc:
            logger.exception("IPC handler failure")
            _send_msg(
                conn,
                {"id": req_id, "ok": False, "error": str(exc), "code": 500},
            )

    def _op_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def _op_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"plugins": self.manager.list_plugins()}

    def _require_id(self, request: dict[str, Any]) -> str:
        plugin_id = request.get("plugin_id") or request.get("id_name")
        # Avoid clashing with request correlation "id"
        if plugin_id is None:
            plugin_id = request.get("plugin")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise PluginIPCError("plugin_id is required", 400)
        return plugin_id.strip()

    def _op_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.status(self._require_id(request))

    def _op_install(self, request: dict[str, Any]) -> dict[str, Any]:
        wheel_path = request.get("wheel_path")
        if not isinstance(wheel_path, str) or not wheel_path.strip():
            raise PluginIPCError("wheel_path is required", 400)
        return self.manager.install(wheel_path.strip())

    def _op_enable(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.enable(self._require_id(request))

    def _op_disable(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.disable(self._require_id(request))

    def _op_start(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.start_plugin(self._require_id(request))

    def _op_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.stop_plugin(self._require_id(request))

    def _op_restart(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.restart_plugin(self._require_id(request))

    def _op_logs(self, request: dict[str, Any]) -> dict[str, Any]:
        tail = request.get("tail", 200)
        try:
            tail_i = int(tail)
        except (TypeError, ValueError) as exc:
            raise PluginIPCError("tail must be an integer", 400) from exc
        return self.manager.logs(self._require_id(request), tail=tail_i)

    def _op_uninstall(self, request: dict[str, Any]) -> dict[str, Any]:
        delete_data = bool(request.get("delete_data", False))
        return self.manager.uninstall(self._require_id(request), delete_data=delete_data)

    def _op_ui_info(self, request: dict[str, Any]) -> dict[str, Any]:
        plugin_id = self._require_id(request)
        status = self.manager.status(plugin_id)
        root = self.manager.ui_root_for(plugin_id)
        return {
            **status,
            "ui_root": str(root) if root else None,
        }

    def _op_get_config(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.get_config(self._require_id(request))

    def _op_set_config(self, request: dict[str, Any]) -> dict[str, Any]:
        config = request.get("config")
        if not isinstance(config, dict):
            raise PluginIPCError("config must be a JSON object", 400)
        restart = bool(request.get("restart", False))
        return self.manager.set_config(self._require_id(request), config, restart=restart)

    def _op_catalogue(self, request: dict[str, Any]) -> dict[str, Any]:
        force = bool(request.get("force_refresh") or request.get("refresh"))
        return self.manager.list_catalogue(force_refresh=force)

    def _op_catalogue_install(self, request: dict[str, Any]) -> dict[str, Any]:
        plugin_id = self._require_id(request)
        version = request.get("version")
        if version is not None:
            version = str(version).strip() or None
        force = bool(request.get("force_refresh") or request.get("refresh"))
        return self.manager.install_from_catalogue(plugin_id, version=version, force_refresh=force)

    def _op_check_update(self, request: dict[str, Any]) -> dict[str, Any]:
        plugin_id = self._require_id(request)
        force = bool(request.get("force_refresh") or request.get("refresh"))
        return self.manager.check_update(plugin_id, force_refresh=force)

    def _op_update(self, request: dict[str, Any]) -> dict[str, Any]:
        plugin_id = self._require_id(request)
        version = request.get("version")
        if version is not None:
            version = str(version).strip() or None
        force = bool(request.get("force_refresh") or request.get("refresh"))
        return self.manager.update_plugin(plugin_id, version=version, force_refresh=force)


class PluginIPCClient:
    """Client used by the Repeater HTTP layer."""

    def __init__(self, socket_path: Path | str, timeout: float = DEFAULT_TIMEOUT):
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._next_id = 1
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.socket_path.exists()

    def call(self, op: str, **params: Any) -> Any:
        if not self.available():
            raise PluginManagerUnavailable()

        with self._lock:
            req_id = self._next_id
            self._next_id += 1

        request = {"id": req_id, "op": op, **params}
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
        except OSError as exc:
            raise PluginManagerUnavailable(f"plugin manager unavailable: {exc}") from exc

        try:
            _send_msg(sock, request)
            raw = _read_line(sock)
            if not raw:
                raise PluginManagerUnavailable("empty response from plugin manager")
            try:
                response = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise PluginIPCError(f"invalid IPC response: {exc}", 502) from exc
            if not isinstance(response, dict):
                raise PluginIPCError("invalid IPC response type", 502)
            if not response.get("ok"):
                code = int(response.get("code") or 500)
                err = str(response.get("error") or "plugin manager error")
                raise PluginIPCError(err, code)
            return response.get("result")
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def list_plugins(self) -> list[dict[str, Any]]:
        result = self.call("list") or {}
        return list(result.get("plugins") or [])

    def status(self, plugin_id: str) -> dict[str, Any]:
        return self.call("status", plugin_id=plugin_id)

    def install(self, wheel_path: str) -> dict[str, Any]:
        return self.call("install", wheel_path=wheel_path)

    def enable(self, plugin_id: str) -> dict[str, Any]:
        return self.call("enable", plugin_id=plugin_id)

    def disable(self, plugin_id: str) -> dict[str, Any]:
        return self.call("disable", plugin_id=plugin_id)

    def start(self, plugin_id: str) -> dict[str, Any]:
        return self.call("start", plugin_id=plugin_id)

    def stop(self, plugin_id: str) -> dict[str, Any]:
        return self.call("stop", plugin_id=plugin_id)

    def restart(self, plugin_id: str) -> dict[str, Any]:
        return self.call("restart", plugin_id=plugin_id)

    def logs(self, plugin_id: str, tail: int = 200) -> dict[str, Any]:
        return self.call("logs", plugin_id=plugin_id, tail=tail)

    def uninstall(self, plugin_id: str, delete_data: bool = False) -> dict[str, Any]:
        return self.call("uninstall", plugin_id=plugin_id, delete_data=delete_data)

    def ui_info(self, plugin_id: str) -> dict[str, Any]:
        return self.call("ui_info", plugin_id=plugin_id)

    def get_config(self, plugin_id: str) -> dict[str, Any]:
        return self.call("get_config", plugin_id=plugin_id)

    def set_config(
        self, plugin_id: str, config: dict[str, Any], *, restart: bool = False
    ) -> dict[str, Any]:
        return self.call("set_config", plugin_id=plugin_id, config=config, restart=restart)

    def catalogue(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return self.call("catalogue", force_refresh=bool(force_refresh))

    def catalogue_install(
        self,
        plugin_id: str,
        *,
        version: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plugin_id": plugin_id,
            "force_refresh": bool(force_refresh),
        }
        if version:
            payload["version"] = version
        return self.call("catalogue_install", **payload)

    def check_update(self, plugin_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        return self.call("check_update", plugin_id=plugin_id, force_refresh=bool(force_refresh))

    def update(
        self,
        plugin_id: str,
        *,
        version: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plugin_id": plugin_id,
            "force_refresh": bool(force_refresh),
        }
        if version:
            payload["version"] = version
        return self.call("update", **payload)
