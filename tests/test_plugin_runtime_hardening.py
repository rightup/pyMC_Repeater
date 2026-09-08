"""Regression tests for untrusted archives and plugin process lifetime."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from repeater.plugins import manifest as manifest_module
from repeater.plugins import runtime as runtime_module
from repeater.plugins import storage as storage_module
from repeater.plugins.manifest import ManifestError, load_manifest_from_wheel, parse_manifest
from repeater.plugins.runtime import PluginRuntime
from repeater.plugins.storage import PluginStorage


def manifest_data(entry="ui/index.html"):
    return {
        "schema": 1,
        "id": "openhop.demo",
        "name": "Demo",
        "version": "1.0.0",
        "ui": {"type": "application", "entry": entry},
    }


@pytest.mark.parametrize(
    "entry",
    [
        "index.html",
        "venv/index.html",
        "venv/bin/python",
        ".venv/index.html",
        "./index.html",
        "ui/./index.html",
    ],
)
def test_ui_requires_dedicated_non_reserved_subtree(entry):
    with pytest.raises(ManifestError):
        parse_manifest(manifest_data(entry))


@pytest.mark.parametrize("kind", ["directory", "leaf", "config"])
def test_extract_rejects_existing_symlinks(tmp_path, kind):
    release = tmp_path / "release"
    release.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "index.html"
    victim.write_text("untouched")
    wheel = tmp_path / "demo.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("ui/index.html", "changed")
        zf.writestr("config.default.json", "changed")
    if kind == "directory":
        (release / "ui").symlink_to(outside, target_is_directory=True)
    elif kind == "leaf":
        (release / "ui").mkdir()
        (release / "ui/index.html").symlink_to(victim)
    else:
        (release / "config.default.json").symlink_to(victim)
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))
    with pytest.raises((ValueError, OSError)):
        if kind == "config":
            runtime._extract_config_default_file(wheel, release)
        else:
            runtime._extract_ui_assets(wheel, release, parse_manifest(manifest_data()))
    assert victim.read_text() == "untouched"


def test_ui_matches_path_components_not_substrings(tmp_path):
    wheel = tmp_path / "demo.whl"
    release = tmp_path / "release"
    release.mkdir()
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("pkg/ui/index.html", "good")
        zf.writestr("notui/secret.txt", "wrong")
    PluginRuntime(PluginStorage(tmp_path))._extract_ui_assets(
        wheel, release, parse_manifest(manifest_data())
    )
    assert (release / "ui/index.html").read_text() == "good"
    assert not (release / "ui/secret.txt").exists()


@pytest.mark.parametrize("kind", ["manifest", "config", "metadata", "ui", "total", "count"])
def test_expanded_archive_limits(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(manifest_module, "ZIP_METADATA_MAX_BYTES", 128, raising=False)
    monkeypatch.setattr(manifest_module, "ZIP_MEMBER_MAX_BYTES", 128, raising=False)
    monkeypatch.setattr(manifest_module, "ZIP_TOTAL_MAX_BYTES", 200, raising=False)
    monkeypatch.setattr(manifest_module, "ZIP_MAX_MEMBERS", 3, raising=False)
    wheel = tmp_path / "upload.whl"
    members = {
        "manifest": {"openhop-plugin.json": json.dumps(manifest_data()) + " " * 256},
        "config": {"config.default.json": " " * 256},
        "metadata": {"demo.dist-info/METADATA": " " * 256},
        "ui": {"ui/index.html": "x" * 256},
        "total": {"ui/a": "x" * 110, "ui/b": "x" * 110},
        "count": {f"ui/{i}": "x" for i in range(4)},
    }[kind]
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    release = tmp_path / "release"
    release.mkdir()
    runtime = PluginRuntime(PluginStorage(tmp_path / "plugins"))
    with pytest.raises(ValueError, match="limit|exceeds"):
        if kind == "manifest":
            load_manifest_from_wheel(wheel)
        elif kind == "config":
            runtime._extract_config_default_file(wheel, release)
        elif kind == "metadata":
            runtime._ensure_pip_wheel_filename(wheel)
        else:
            runtime._extract_ui_assets(wheel, release, parse_manifest(manifest_data()))


def prepared_runtime(tmp_path, **kwargs):
    storage = PluginStorage(tmp_path / "plugins")
    paths = storage.ensure_plugin_layout("openhop.demo", "1.0.0")
    data = manifest_data()
    data["runtime"] = {"type": "python", "entrypoint": "demo-cli"}
    manifest = parse_manifest(data)
    storage.write_manifest(manifest.id, manifest.version, manifest)
    storage.write_state(manifest.id, {"version": manifest.version, "enabled": True})
    storage.set_current(manifest.id, manifest.version)
    return PluginRuntime(storage, **kwargs), paths


def test_failed_rebuild_restores_old_environment_and_retries(tmp_path):
    calls = []
    fail = True

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "venv" in cmd:
            target = Path(cmd[-1])
            (target / "bin").mkdir(parents=True)
            (target / "bin/python").symlink_to(sys.executable)
            (target / "pyvenv.cfg").write_text(
                f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n"
            )
        else:
            if fail:
                raise RuntimeError("pip failed")
            script = Path(cmd[0]).parent / "demo-cli"
            script.write_text(f"#!{cmd[0]}\nprint('working')\n")
            script.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime, paths = prepared_runtime(tmp_path, run_factory=run)
    venv = paths.venv_dir("1.0.0")
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("old")
    (venv / "pyvenv.cfg").write_text("version = 2.7.0\n")
    (paths.release_dir("1.0.0") / "demo-1.0.0-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="pip failed"):
        runtime.ensure_venv_compatible("openhop.demo")
    assert (venv / "bin/python").read_text() == "old"
    assert (venv / "pyvenv.cfg").read_text() == "version = 2.7.0\n"
    fail = False
    runtime.ensure_venv_compatible("openhop.demo")
    entry = runtime.resolve_entrypoint(paths, "1.0.0", "demo-cli")
    assert subprocess.check_output([str(entry)], text=True, timeout=5).strip() == "working"
    assert len([cmd for cmd in calls if "pip" in cmd]) == 2


@pytest.mark.parametrize("stage", ["old_renamed", "new_config"])
def test_fresh_runtime_recovers_interrupted_rebuild_snapshot(tmp_path, stage):
    snapshot = tmp_path / "snapshot"
    calls = []
    capturing = True

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "venv" in cmd:
            target = Path(cmd[-1])
            if capturing and stage == "old_renamed":
                shutil.copytree(tmp_path / "plugins", snapshot, symlinks=True)
                raise RuntimeError("snapshot captured")
            (target / "bin").mkdir(parents=True)
            (target / "bin/python").symlink_to(sys.executable)
            (target / "pyvenv.cfg").write_text(
                f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n"
            )
            if capturing:
                shutil.copytree(tmp_path / "plugins", snapshot, symlinks=True)
                raise RuntimeError("snapshot captured")
        else:
            entry = Path(cmd[0]).parent / "demo-cli"
            entry.write_text(f"#!{cmd[0]}\nprint('recovered')\n")
            entry.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime, paths = prepared_runtime(tmp_path, run_factory=run)
    venv = paths.venv_dir("1.0.0")
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("old")
    (venv / "pyvenv.cfg").write_text("version = 2.7.0\n")
    (paths.release_dir("1.0.0") / "demo-1.0.0-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="snapshot captured"):
        runtime.ensure_venv_compatible("openhop.demo")

    capturing = False
    calls.clear()
    fresh = PluginRuntime(PluginStorage(snapshot), run_factory=run)
    fresh.ensure_venv_compatible("openhop.demo")
    assert len([cmd for cmd in calls if "pip" in cmd]) == 1
    entry = fresh.resolve_entrypoint(fresh.storage.paths_for("openhop.demo"), "1.0.0", "demo-cli")
    assert subprocess.check_output([str(entry)], text=True, timeout=5).strip() == "recovered"
    calls.clear()
    fresh.ensure_venv_compatible("openhop.demo")
    assert calls == []


def test_install_subprocesses_have_deadlines(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runtime = PluginRuntime(PluginStorage(tmp_path), run_factory=run)
    runtime._create_venv(tmp_path / "venv")
    (tmp_path / "venv/bin").mkdir(parents=True)
    (tmp_path / "venv/bin/python").touch()
    runtime._pip_install(tmp_path / "venv", tmp_path / "demo-1.0.0-py3-none-any.whl")
    assert all(0 < call.get("timeout", 0) <= 300 for call in calls)


def test_log_tail_reads_only_bounded_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "LOG_TAIL_MAX_BYTES", 128, raising=False)
    storage = PluginStorage(tmp_path / "plugins")
    paths = storage.ensure_plugin_layout("openhop.demo", "1.0.0")
    paths.log_file.write_text("a" * 1024 + "\nlast line\n")
    result = storage.tail_log("openhop.demo")
    assert result[-1] == "last line"
    assert len("\n".join(result).encode()) <= 128


def test_running_plugin_log_is_continuously_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "LOG_MAX_BYTES", 1024)
    runtime, paths = prepared_runtime(tmp_path)
    bindir = paths.venv_dir("1.0.0") / "bin"
    bindir.mkdir(parents=True)
    entry = bindir / "demo-cli"
    marker = tmp_path / "ready"
    entry.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nos.write(1, b'x' * 32768)\nPath({str(marker)!r}).touch()\ntime.sleep(60)\n"
    )
    entry.chmod(0o755)
    try:
        runtime.start("openhop.demo")
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        time.sleep(0.1)
        assert paths.log_file.stat().st_size <= 1024
    finally:
        runtime.stop("openhop.demo")


def test_install_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "INSTALL_OUTPUT_MAX_BYTES", 128, raising=False)
    runtime = PluginRuntime(PluginStorage(tmp_path))
    result = runtime._run(
        [sys.executable, "-c", "print('x' * 8192)"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert len(result.stdout.encode()) <= 128


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group regression")
def test_install_timeout_kills_child_processes(tmp_path):
    marker = tmp_path / "child.pid"
    code = (
        "import subprocess,time; from pathlib import Path; "
        f"p=subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
        f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    runtime = PluginRuntime(PluginStorage(tmp_path))
    pid = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runtime._run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=False,
                timeout=0.3,
            )
        assert marker.exists()
        pid = int(marker.read_text())
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(pid)
    finally:
        if marker.exists():
            pid = int(marker.read_text())
        if pid and alive(pid):
            os.kill(pid, signal.SIGKILL)


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group regression")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_stop_kills_term_ignoring_descendant_even_after_parent_exit(tmp_path, parent_exits):
    runtime, paths = prepared_runtime(tmp_path, stop_timeout=0.15)
    venv = paths.venv_dir("1.0.0")
    (venv / "bin").mkdir(parents=True)
    entry = venv / "bin/demo-cli"
    marker = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    entry.write_text(
        f"#!{sys.executable}\nimport subprocess,time\nfrom pathlib import Path\np = subprocess.Popen([{sys.executable!r}, '-c', {child!r}])\nPath({str(marker)!r}).write_text(str(p.pid))\ntime.sleep({0.1 if parent_exits else 60})\n"
    )
    entry.chmod(0o755)
    pid = None
    proc = None
    try:
        runtime.start("openhop.demo")
        proc = runtime._handles["openhop.demo"].process
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        pid = int(marker.read_text())
        time.sleep(0.15)
        if parent_exits:
            proc.wait(timeout=5)
        runtime.stop("openhop.demo")
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(pid)
    finally:
        if pid and alive(pid):
            os.kill(pid, signal.SIGKILL)
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        runtime.stop("openhop.demo")
