"""Lifecycle and install tests for the plugin manager."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from repeater.plugins.manager import PluginManager, PluginManagerError
from repeater.plugins.manifest import PluginManifest, RuntimeSpec
from repeater.plugins.runtime import PluginRuntime, PluginState
from repeater.plugins.storage import PluginStorage


class FakeProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self._code = None
        self.signals = []

    def poll(self):
        return self._code

    def send_signal(self, sig):
        self.signals.append(sig)
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code

    def exit(self, code=1):
        self._code = code


def _make_wheel(path: Path, manifest: dict, ui_files: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"share/openhop/plugins/{manifest['id']}/openhop-plugin.json",
            json.dumps(manifest),
        )
        if ui_files:
            for name, content in ui_files.items():
                zf.writestr(name, content)
    return path


def test_install_creates_layout_and_isolated_venv(tmp_path: Path, monkeypatch):
    storage = PluginStorage(tmp_path / "plugins")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # Simulate venv creation by writing python binary
        if "-m" in cmd and "venv" in cmd:
            venv = Path(cmd[-1])
            bin_dir = venv / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "python").chmod(0o755)
        if "-m" in cmd and "pip" in cmd:
            # create entrypoint after pip install
            py = Path(cmd[0])
            entry = py.parent / "demo-cli"
            entry.write_text("#!/bin/sh\n")
            entry.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime = PluginRuntime(storage, run_factory=fake_run)
    manager = PluginManager(storage, runtime)

    wheel = tmp_path / "demo-0.1.0-py3-none-any.whl"
    _make_wheel(
        wheel,
        {
            "schema": 1,
            "id": "openhop.demo",
            "name": "Demo",
            "version": "0.1.0",
            "runtime": {"type": "python", "entrypoint": "demo-cli"},
        },
    )

    status = manager.install(wheel)
    assert status["id"] == "openhop.demo"
    assert status["enabled"] is False
    paths = storage.paths_for("openhop.demo")
    assert paths.data_dir.is_dir()
    assert paths.venv_dir("0.1.0").is_dir()
    assert any("venv" in c for c in calls)
    assert any("pip" in c for c in calls)
    # Ensure we never invoked the system pip against a non-venv target alone
    for c in calls:
        if "pip" in c:
            assert str(paths.venv_dir("0.1.0")) in c[0] or "venv" in "".join(c)


def test_enable_start_stop_disable(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    procs: list[FakeProc] = []

    def fake_popen(cmd, **kwargs):
        p = FakeProc(pid=1000 + len(procs))
        procs.append(p)
        # Write a line to log fd if provided
        log_fp = kwargs.get("stdout")
        if log_fp and hasattr(log_fp, "write"):
            log_fp.write("hello from plugin\n")
        return p

    def fake_run(cmd, **kwargs):
        if "-m" in cmd and "venv" in cmd:
            venv = Path(cmd[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("x")
            (venv / "bin" / "python").chmod(0o755)
        if "-m" in cmd and "pip" in cmd:
            py = Path(cmd[0])
            ep = py.parent / "demo-cli"
            ep.write_text("x")
            ep.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime = PluginRuntime(storage, popen_factory=fake_popen, run_factory=fake_run)
    manager = PluginManager(storage, runtime)

    wheel = tmp_path / "demo-0.1.0-py3-none-any.whl"
    _make_wheel(
        wheel,
        {
            "schema": 1,
            "id": "openhop.demo",
            "name": "Demo",
            "version": "0.1.0",
            "runtime": {"type": "python", "entrypoint": "demo-cli"},
        },
    )
    manager.install(wheel)

    st = manager.enable("openhop.demo")
    assert st["enabled"] is True
    assert st["state"] == PluginState.RUNNING.value
    assert len(procs) == 1

    st = manager.stop_plugin("openhop.demo")
    assert st["state"] == PluginState.STOPPED.value

    st = manager.start_plugin("openhop.demo")
    assert st["state"] == PluginState.RUNNING.value

    st = manager.restart_plugin("openhop.demo")
    assert st["state"] == PluginState.RUNNING.value

    st = manager.disable("openhop.demo")
    assert st["enabled"] is False
    assert st["state"] == PluginState.DISABLED.value

    logs = manager.logs("openhop.demo", tail=10)
    assert any("hello" in line for line in logs["lines"])


def test_crash_loop_becomes_failed(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": True})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
        ),
    )
    storage.set_current("openhop.demo", "0.1.0")
    paths = storage.paths_for("openhop.demo")
    bin_dir = paths.venv_dir("0.1.0") / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "demo-cli").write_text("x")
    (bin_dir / "demo-cli").chmod(0o755)

    procs: list[FakeProc] = []

    def fake_popen(cmd, **kwargs):
        p = FakeProc(pid=2000 + len(procs))
        procs.append(p)
        return p

    runtime = PluginRuntime(
        storage,
        popen_factory=fake_popen,
        crash_max_exits=3,
        crash_window=60.0,
    )
    runtime.start("openhop.demo")
    assert runtime.get_state("openhop.demo") == PluginState.RUNNING

    # Unexpected exits → restart until FAILED
    for _ in range(3):
        procs[-1].exit(1)
        runtime._check_crashes()

    assert runtime.get_state("openhop.demo") == PluginState.FAILED
    assert len(procs) >= 3


def test_unexpected_exit_restarts(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": True})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
        ),
    )
    storage.set_current("openhop.demo", "0.1.0")
    bin_dir = storage.paths_for("openhop.demo").venv_dir("0.1.0") / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "demo-cli").write_text("x")
    (bin_dir / "demo-cli").chmod(0o755)

    procs: list[FakeProc] = []

    def fake_popen(cmd, **kwargs):
        p = FakeProc(pid=3000 + len(procs))
        procs.append(p)
        return p

    runtime = PluginRuntime(storage, popen_factory=fake_popen, crash_max_exits=5)
    runtime.start("openhop.demo")
    procs[0].exit(1)
    runtime._check_crashes()
    assert runtime.get_state("openhop.demo") == PluginState.RUNNING
    assert len(procs) == 2


def test_uninstall_keeps_data_by_default(tmp_path: Path):
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
    manager = PluginManager(storage, runtime)
    wheel = tmp_path / "demo-0.1.0-py3-none-any.whl"
    _make_wheel(
        wheel,
        {
            "schema": 1,
            "id": "openhop.demo",
            "name": "Demo",
            "version": "0.1.0",
            "runtime": {"type": "python", "entrypoint": "demo-cli"},
        },
    )
    manager.install(wheel)
    paths = storage.paths_for("openhop.demo")
    (paths.data_dir / "keep.txt").write_text("keep")
    manager.enable("openhop.demo")
    manager.uninstall("openhop.demo", delete_data=False)
    assert (paths.data_dir / "keep.txt").is_file()
    assert storage.read_state("openhop.demo") is None


def test_manager_start_enables_boot_plugins(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": True})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
        ),
    )
    storage.set_current("openhop.demo", "0.1.0")
    bin_dir = storage.paths_for("openhop.demo").venv_dir("0.1.0") / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "demo-cli").write_text("x")
    (bin_dir / "demo-cli").chmod(0o755)

    started = []

    def fake_popen(cmd, **kwargs):
        started.append(cmd)
        return FakeProc()

    runtime = PluginRuntime(storage, popen_factory=fake_popen)
    manager = PluginManager(storage, runtime)
    manager.start()
    assert started
    manager.stop_all()


def test_get_config_uses_manifest_defaults_when_unsaved(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": False})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
            config_defaults={"meshcore_host": "127.0.0.1", "nomad_url": "http://x"},
        ),
    )
    storage.set_current("openhop.demo", "0.1.0")
    manager = PluginManager(storage)
    cfg = manager.get_config("openhop.demo")
    assert cfg["exists"] is False
    assert cfg["defaults"]["meshcore_host"] == "127.0.0.1"
    assert cfg["config"]["nomad_url"] == "http://x"


def test_get_set_config_and_optional_restart(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": True})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
        ),
    )
    storage.set_current("openhop.demo", "0.1.0")
    bin_dir = storage.paths_for("openhop.demo").venv_dir("0.1.0") / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "demo-cli").write_text("x")
    (bin_dir / "demo-cli").chmod(0o755)

    procs: list[FakeProc] = []

    def fake_popen(cmd, **kwargs):
        p = FakeProc(pid=4000 + len(procs))
        procs.append(p)
        return p

    runtime = PluginRuntime(storage, popen_factory=fake_popen)
    manager = PluginManager(storage, runtime)

    cfg = manager.get_config("openhop.demo")
    assert cfg["config"] == {}
    assert cfg["exists"] is False

    saved = manager.set_config(
        "openhop.demo",
        {"nomad_url": "http://example", "nomad_model": "qwen"},
        restart=True,
    )
    assert saved["config"]["nomad_url"] == "http://example"
    assert saved["exists"] is True
    assert saved["restarted"] is True
    assert len(procs) >= 1


def test_ensure_pip_wheel_filename_rewrites_invalid_temp_name(tmp_path: Path):
    """Uploaded temp names like plugin-XXXX.whl must be rewritten for pip."""
    import zipfile

    bad = tmp_path / "plugin-p7kja66a.whl"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr(
            "openhop_nomad_plugin-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: openhop-nomad-plugin\nVersion: 0.1.0\n",
        )
        zf.writestr("pkg/__init__.py", "")

    fixed = PluginRuntime._ensure_pip_wheel_filename(bad)
    assert fixed.name == "openhop_nomad_plugin-0.1.0-py3-none-any.whl"
    assert fixed.is_file()
    assert PluginRuntime._looks_like_wheel_filename(fixed.name)


def test_start_disabled_raises(tmp_path: Path):
    storage = PluginStorage(tmp_path / "plugins")
    storage.ensure_plugin_layout("openhop.demo", "0.1.0")
    storage.write_state("openhop.demo", {"version": "0.1.0", "enabled": False})
    storage.write_manifest(
        "openhop.demo",
        "0.1.0",
        PluginManifest(
            schema=1,
            id="openhop.demo",
            name="Demo",
            version="0.1.0",
            runtime=RuntimeSpec(type="python", entrypoint="demo-cli"),
        ),
    )
    manager = PluginManager(storage)
    with pytest.raises(PluginManagerError):
        manager.start_plugin("openhop.demo")
