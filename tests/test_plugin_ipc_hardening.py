"""Synthetic socket/lifecycle regressions; no subprocesses or network services."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from repeater.plugins.ipc import PluginIPCClient, PluginIPCError, PluginIPCServer
from repeater.plugins.manager import PluginManager, PluginManagerError
from repeater.plugins.storage import PluginStorage


class SlowManager(PluginManager):
    def __init__(self, root):
        super().__init__(PluginStorage(root))
        self.entered = threading.Event()
        self.release = threading.Event()
        self.consumed = None

    def install(self, wheel_path):
        self.entered.set()
        assert self.release.wait(3)
        self.consumed = Path(wheel_path).read_bytes()
        return {"id": "demo", "version": "1"}

    def status(self, plugin_id):
        return {"id": plugin_id, "state": "STOPPED"}


@pytest.fixture
def service(tmp_path):
    manager = SlowManager(tmp_path / "plugins")
    server = PluginIPCServer(tmp_path / "s", manager)
    server.start()
    try:
        yield manager, server
    finally:
        manager.release.set()
        server.stop()


def test_install_does_not_block_status(service, tmp_path):
    manager, server = service
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(PluginIPCClient(server.socket_path).install, str(wheel))
        try:
            assert manager.entered.wait(1)
            assert PluginIPCClient(server.socket_path, timeout=0.2).status("other")["id"] == "other"
        finally:
            manager.release.set()
        assert future.result()["id"] == "demo"


def test_install_has_separate_completion_budget(service, tmp_path):
    manager, server = service
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    client = PluginIPCClient(server.socket_path, timeout=0.05)
    client.operation_timeout = 1
    timer = threading.Timer(0.15, manager.release.set)
    timer.start()
    try:
        assert client.install(str(wheel))["id"] == "demo"
    finally:
        timer.join()


def test_timeout_is_unknown_and_manager_owns_upload(service, tmp_path):
    manager, server = service
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    client = PluginIPCClient(server.socket_path, timeout=0.1)
    client.operation_timeout = 0.1
    with pytest.raises(PluginIPCError) as error:
        client.install(str(wheel))
    assert error.value.code == 504
    assert error.value.outcome == "unknown"
    assert error.value.upload_safe is True
    wheel.unlink()
    manager.release.set()
    # A completion barrier is used rather than sleeping before checking consumption.
    server.stop()
    assert manager.consumed == b"wheel"
    assert not list((manager.storage.root / ".ipc-staging").glob("*"))


def test_slow_capacity_rejects_without_blocking_control(service, tmp_path):
    manager, server = service
    wheel = tmp_path / "demo.whl"
    wheel.write_bytes(b"wheel")
    # Capacity is deliberately one slow operation, with no waiting operation queue.
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(PluginIPCClient(server.socket_path).install, str(wheel))
        try:
            assert manager.entered.wait(1)
            with pytest.raises(PluginIPCError) as error:
                PluginIPCClient(server.socket_path, timeout=0.2).install(str(wheel))
            assert error.value.code == 503
            assert PluginIPCClient(server.socket_path, timeout=0.2).status("other")["id"] == "other"
        finally:
            manager.release.set()
        future.result()


def test_truncated_completion_is_unknown(tmp_path):
    import socket

    from repeater.plugins.ipc import _read_line

    path = Path("/tmp") / f"legacy-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def legacy():
        with listener.accept()[0] as conn:
            _read_line(conn)
            conn.sendall(b'{"ok":true')

    worker = threading.Thread(target=legacy)
    worker.start()
    try:
        with pytest.raises(PluginIPCError) as error:
            PluginIPCClient(path).install("unused.whl")
        assert error.value.code == 504
        assert error.value.outcome == "unknown"
        assert error.value.upload_safe is False
    finally:
        worker.join(1)
        listener.close()


def test_manager_install_does_not_hold_global_control_lock(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    storage = PluginStorage(tmp_path)
    storage.write_state("other", {"enabled": True, "version": "1"})

    def install(path, manifest):
        entered.set()
        assert release.wait(3)
        return {"id": "demo", "version": "1"}

    runtime = SimpleNamespace(
        install_wheel=install,
        stop=lambda *a, **k: None,
        status_dict=lambda pid: {"id": pid},
        _runtime_state={},
    )
    manager = PluginManager(storage, runtime)
    monkeypatch.setattr(
        "repeater.plugins.manager.load_manifest_from_wheel", lambda p: SimpleNamespace(id="demo")
    )
    with ThreadPoolExecutor(2) as pool:
        installing = pool.submit(manager.install, tmp_path / "demo.whl")
        try:
            assert entered.wait(1)
            assert pool.submit(manager.disable, "other").result(timeout=0.2)["id"] == "other"
            with pytest.raises(PluginManagerError) as error:
                manager.disable("demo")
            assert error.value.code == 409
        finally:
            release.set()
        installing.result()
