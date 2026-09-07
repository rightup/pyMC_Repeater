"""JSON-lines Unix domain socket IPC between Repeater and the plugin manager."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .manager import PluginManager, PluginManagerError

logger = logging.getLogger("PluginIPC")

DEFAULT_TIMEOUT = 30.0
OPERATION_TIMEOUT = 900.0  # completion budget, not a subprocess cancellation deadline
MAX_CONNECTIONS = 16
SLOW_OPS = frozenset({"install", "catalogue_install", "update", "catalogue", "check_update"})
INSTALL_OPS = frozenset({"install", "catalogue_install", "update"})
MAX_LINE_BYTES = 8 * 1024 * 1024  # install paths are short; keep headroom
MAX_AF_UNIX_PATH_BYTES = 92


def _safe_socket_path(path: Path | str) -> Path:
    candidate = Path(path)
    if len(str(candidate)) <= MAX_AF_UNIX_PATH_BYTES:
        return candidate
    digest = hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:16]
    fallback = Path(tempfile.gettempdir()) / f"openhop-plugin-{digest}.sock"
    logger.warning(
        "Unix socket path %s is too long for AF_UNIX; using %s instead", candidate, fallback
    )
    return fallback


class PluginIPCError(Exception):
    def __init__(self, message: str, code: int = 500):
        super().__init__(message)
        self.code = code


class PluginIPCOutcomeUnknown(PluginIPCError):
    """Transport stopped waiting; the manager may still complete the operation."""

    def __init__(self, *, upload_safe: bool = False):
        super().__init__(
            "Plugin manager response was lost or timed out; outcome is unknown. "
            "The operation may still complete. Check plugin status before retrying.",
            504,
        )
        self.outcome = "unknown"
        self.upload_safe = upload_safe


class PluginManagerUnavailable(PluginIPCError):
    def __init__(self, message: str = "plugin manager unavailable"):
        super().__init__(message, code=503)


def _read_line(
    conn: socket.socket,
    max_bytes: int = MAX_LINE_BYTES,
    *,
    buffer: Optional[bytearray] = None,
    deadline: Optional[float] = None,
) -> bytes:
    buf = buffer if buffer is not None else bytearray()
    if deadline is None:
        deadline = time.monotonic() + (conn.gettimeout() or DEFAULT_TIMEOUT)
    while True:
        end = buf.find(b"\n")
        if end >= 0:
            if end > max_bytes:
                raise PluginIPCError("IPC message too large", 413)
            line = bytes(buf[:end])
            del buf[: end + 1]
            return line
        if len(buf) > max_bytes:
            raise PluginIPCError("IPC message too large", 413)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("IPC response deadline exceeded")
        conn.settimeout(remaining)
        chunk = conn.recv(min(4096, max_bytes + 1 - len(buf)))
        if not chunk:
            if buf:
                raise PluginIPCError("incomplete IPC message", 502)
            return b""
        buf.extend(chunk)


def _send_msg(conn: socket.socket, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    conn.sendall(data)


class PluginIPCServer:
    """Serve plugin manager operations over a Unix domain socket."""

    def __init__(self, socket_path: Path | str, manager: PluginManager):
        self.socket_path = _safe_socket_path(socket_path)
        self.manager = manager
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._slow_slot = threading.BoundedSemaphore(1)
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
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
            "get_runtime": self._op_get_runtime,
            "set_config": self._op_set_config,
            "catalogue": self._op_catalogue,
            "catalogue_install": self._op_catalogue_install,
            "check_update": self._op_check_update,
            "update": self._op_update,
            "progress": self._op_progress,
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
        deadline = time.monotonic() + 3
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
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
            if not self._slots.acquire(blocking=False):
                # No unbounded executor queue; a saturated transport rejects immediately.
                try:
                    conn.settimeout(0.1)
                    _send_msg(
                        conn, {"ok": False, "error": "plugin IPC busy; retry later", "code": 503}
                    )
                except OSError:
                    pass
                finally:
                    conn.close()
                continue
            worker = threading.Thread(target=self._connection_worker, args=(conn,), daemon=True)
            with self._workers_lock:
                self._workers.add(worker)
            worker.start()

    def _connection_worker(self, conn: socket.socket) -> None:
        try:
            self._handle_connection(conn)
        except Exception as exc:
            logger.debug("IPC connection error: %s", exc)
        finally:
            conn.close()
            with self._workers_lock:
                self._workers.discard(threading.current_thread())
            self._slots.release()

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
            slow = op in SLOW_OPS
            if slow and not self._slow_slot.acquire(blocking=False):
                raise PluginIPCError("plugin download/install busy; retry later", 503)
            try:
                if op == "install":
                    wheel_path = request.get("wheel_path")
                    if not isinstance(wheel_path, str) or not wheel_path.strip():
                        raise PluginIPCError("wheel_path is required", 400)
                    with self.manager.stage_upload(wheel_path.strip()) as staged:
                        if request.get("completion_protocol") == 1:
                            _send_msg(conn, {"id": req_id, "ok": True, "processing": True})
                        result = self.manager.install(staged)
                else:
                    if op in INSTALL_OPS and request.get("completion_protocol") == 1:
                        _send_msg(conn, {"id": req_id, "ok": True, "processing": True})
                    result = self._handlers[op](request)
                _send_msg(conn, {"id": req_id, "ok": True, "result": result})
            finally:
                if slow:
                    self._slow_slot.release()
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
        except OSError:
            # A disconnected waiter is not an operation failure or cancellation.
            raise
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

    def _op_get_runtime(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.manager.get_runtime(self._require_id(request))

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

    def _op_progress(self, request: dict[str, Any]) -> dict[str, Any]:
        plugin_id = self._require_id(request)
        try:
            since = int(request.get("since") or 0)
        except (TypeError, ValueError):
            raise PluginIPCError("since must be an integer", 400)
        return self.manager.progress(plugin_id, since=since)

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
        self.socket_path = _safe_socket_path(socket_path)
        self.timeout = timeout
        self.operation_timeout = OPERATION_TIMEOUT
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
        if op in INSTALL_OPS:
            request["completion_protocol"] = 1
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise PluginManagerUnavailable(f"plugin manager unavailable: {exc}") from exc

        upload_safe = False
        try:
            _send_msg(sock, request)
            buffer = bytearray()
            deadline = time.monotonic() + self.timeout
            completion_deadline = time.monotonic() + self.operation_timeout
            while True:
                try:
                    raw = _read_line(sock, buffer=buffer, deadline=deadline)
                except PluginIPCError as exc:
                    raise PluginIPCOutcomeUnknown(upload_safe=upload_safe) from exc
                if not raw:
                    raise PluginIPCOutcomeUnknown(upload_safe=upload_safe)
                try:
                    response = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise PluginIPCOutcomeUnknown(upload_safe=upload_safe) from exc
                if not isinstance(response, dict):
                    raise PluginIPCOutcomeUnknown(upload_safe=upload_safe)
                if not response.get("ok"):
                    code = int(response.get("code") or 500)
                    err = str(response.get("error") or "plugin manager error")
                    raise PluginIPCError(err, code)
                if response.get("id") != req_id:
                    raise PluginIPCOutcomeUnknown(upload_safe=upload_safe)
                if response.get("processing") is True:
                    upload_safe = op == "install"
                    deadline = completion_deadline
                    continue
                return response.get("result")
        except OSError as exc:
            raise PluginIPCOutcomeUnknown(upload_safe=upload_safe) from exc
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

    def get_runtime(self, plugin_id: str) -> dict[str, Any]:
        return self.call("get_runtime", plugin_id=plugin_id)

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

    def progress(self, plugin_id: str, *, since: int = 0) -> dict[str, Any]:
        return self.call("progress", plugin_id=plugin_id, since=int(since))

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
