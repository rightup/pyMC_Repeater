"""IPC tests for plugin manager Unix socket protocol."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from repeater.plugins.ipc import (
    PluginIPCClient,
    PluginIPCError,
    PluginIPCServer,
    PluginManagerUnavailable,
)
from repeater.plugins.manager import PluginManager
from repeater.plugins.runtime import PluginRuntime
from repeater.plugins.storage import PluginStorage


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self._code = None

    def poll(self):
        return self._code

    def send_signal(self, sig):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code


def _wheel(path: Path) -> Path:
    manifest = {
        "schema": 1,
        "id": "openhop.demo",
        "name": "Demo",
        "version": "0.1.0",
        "runtime": {"type": "python", "entrypoint": "demo-cli"},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "share/openhop/plugins/openhop.demo/openhop-plugin.json",
            json.dumps(manifest),
        )
    return path


def _manager(tmp_path: Path) -> tuple[PluginManager, Path]:
    storage = PluginStorage(tmp_path / "plugins")

    def fake_run(cmd, **kwargs):
        if "venv" in cmd:
            venv = Path(cmd[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("x")
            (venv / "bin" / "python").chmod(0o755)
        if "pip" in cmd:
            Path(cmd[0]).parent.joinpath("demo-cli").write_text("x")
            Path(cmd[0]).parent.joinpath("demo-cli").chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime = PluginRuntime(storage, run_factory=fake_run, popen_factory=lambda *a, **k: FakeProc())
    # macOS AF_UNIX path length is limited; keep the socket under /tmp.
    sock_path = Path(f"/tmp/oh-pm-{tmp_path.name}.sock")
    return PluginManager(storage, runtime), sock_path


def test_ipc_list_status_start_stop(tmp_path: Path, monkeypatch):
    def absent_fake_group(*_args):
        raise ProcessLookupError

    # Lifecycle behavior uses fake Popen objects here; never signal host PIDs.
    monkeypatch.setattr("os.killpg", absent_fake_group)
    manager, sock_path = _manager(tmp_path)
    wheel = _wheel(tmp_path / "demo-0.1.0-py3-none-any.whl")
    manager.install(wheel)

    server = PluginIPCServer(sock_path, manager)
    server.start()
    try:
        client = PluginIPCClient(sock_path)
        plugins = client.list_plugins()
        assert any(p["id"] == "openhop.demo" for p in plugins)

        client.enable("openhop.demo")
        st = client.status("openhop.demo")
        assert st["state"] == "RUNNING"

        client.stop("openhop.demo")
        st = client.status("openhop.demo")
        assert st["state"] == "STOPPED"

        runtime_path = manager.storage.runtime_path("openhop.demo")
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(json.dumps({"schema": 1, "running": True}), encoding="utf-8")
        runtime = client.get_runtime("openhop.demo")
        assert runtime["exists"] is True
        assert runtime["runtime"]["schema"] == 1

        with pytest.raises(PluginIPCError):
            client.status("missing.plugin")
    finally:
        server.stop()
        manager.stop_all()


def test_manager_unavailable():
    client = PluginIPCClient("/tmp/definitely-missing-openhop-plugin-manager.sock")
    with pytest.raises(PluginManagerUnavailable):
        client.list_plugins()
