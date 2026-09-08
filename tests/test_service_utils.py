import io
import subprocess
from unittest.mock import MagicMock

import pytest

from repeater import service_utils as su


def test_is_buildroot_via_metadata_file(monkeypatch):
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su.BUILDROOT_METADATA_PATH)
    assert su.is_buildroot() is True


def test_is_buildroot_via_os_release(monkeypatch):
    monkeypatch.setattr(
        su.os.path,
        "exists",
        lambda p: p == "/etc/os-release",
    )
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: io.StringIO("NAME=x\nID=buildroot\n"),
    )
    assert su.is_buildroot() is True


def test_get_buildroot_image_info_parse_and_error(monkeypatch):
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: io.StringIO("\nfoo=bar\ninvalid\nimage_version=1.2.3\n"),
    )
    info = su.get_buildroot_image_info()
    assert info["foo"] == "bar"
    assert info["image_version"] == "1.2.3"
    assert su.get_buildroot_image_version() == "1.2.3"

    def _raise(*_args, **_kwargs):
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", _raise)
    assert su.get_buildroot_image_info() == {}


def test_is_container_detection_paths(monkeypatch):
    # /.dockerenv path
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == "/.dockerenv")
    monkeypatch.delenv("container", raising=False)
    assert su.is_container() is True

    # env var path
    monkeypatch.setattr(su.os.path, "exists", lambda _p: False)
    monkeypatch.setenv("container", "docker")
    assert su.is_container() is True


@pytest.mark.parametrize(
    "environ_bytes,cgroup_text,host_path,expected",
    [
        (b"abc\x00container=docker\x00", "", False, True),
        (b"abc", "1:name=systemd:/docker/abc", False, True),
        (b"abc", "1:name=systemd:/", True, True),
        (b"abc", "1:name=systemd:/", False, False),
    ],
)
def test_is_container_proc_and_host_paths(
    monkeypatch, environ_bytes, cgroup_text, host_path, expected
):
    monkeypatch.setattr(
        su.os.path, "exists", lambda p: p == "/run/host/container-manager" and host_path
    )
    monkeypatch.delenv("container", raising=False)

    def _open(path, mode="r", encoding=None):
        if path == "/proc/1/environ":
            return io.BytesIO(environ_bytes)
        if path == "/proc/1/cgroup":
            return io.StringIO(cgroup_text)
        raise OSError("unexpected")

    monkeypatch.setattr("builtins.open", _open)
    assert su.is_container() is expected


def test_get_container_restart_message():
    msg = su.get_container_restart_message()
    assert "Container restart initiated" in msg
    assert "Docker or Home Assistant" in msg


def test_restart_service_container_path(monkeypatch):
    monkeypatch.setattr(su, "is_container", lambda: True)
    sched = MagicMock()
    monkeypatch.setattr(su, "_schedule_container_exit", sched)

    ok, msg = su.restart_service()
    assert ok is True
    assert "Container restart initiated" in msg
    sched.assert_called_once()


def test_restart_service_buildroot_paths(monkeypatch):
    monkeypatch.setattr(su, "is_container", lambda: False)
    monkeypatch.setattr(su, "is_buildroot", lambda: True)

    # missing init script
    monkeypatch.setattr(su.os.path, "exists", lambda _p: False)
    ok, msg = su.restart_service()
    assert ok is False
    assert "init script not found" in msg

    # popen success
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su.INIT_SCRIPT)
    monkeypatch.setattr(su.subprocess, "Popen", MagicMock())
    ok, msg = su.restart_service()
    assert ok is True
    assert "Service restart initiated" in msg

    # popen failure
    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(su.subprocess, "Popen", _raise)
    ok, msg = su.restart_service()
    assert ok is False
    assert "Restart failed" in msg


@pytest.mark.parametrize(
    "error, expected",
    [
        (FileNotFoundError("systemd-run"), "scheduler unavailable"),
        (RuntimeError("bad"), "Restart command failed"),
    ],
)
def test_restart_scheduler_launch_errors(monkeypatch, error, expected):
    monkeypatch.setattr(su, "is_container", lambda: False)
    monkeypatch.setattr(su, "is_buildroot", lambda: False)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(su.subprocess, "run", fail)
    ok, message = su.restart_service()
    assert ok is False
    assert expected in message


def test_ensure_plugin_manager_service_runs_sudo_bootstrap(monkeypatch):
    monkeypatch.setattr(su, "is_container", lambda: False)
    monkeypatch.setattr(su, "is_buildroot", lambda: False)
    monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        su.os.path, "isfile", lambda p: p == "/opt/openhop_repeater/venv/bin/python"
    )
    monkeypatch.setattr(
        su.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None
    )

    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["/usr/bin/sudo", "--non-interactive", "/bin/systemctl"] and cmd[3:5] == [
            "is-active",
            "--quiet",
        ]:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(su.subprocess, "run", _run)

    ok, msg = su.ensure_plugin_manager_service()
    assert ok is True
    assert msg == "Plugin-manager service is installed and active"
    install_call = next(
        call
        for call in calls
        if call[:3] == ["/usr/bin/sudo", "--non-interactive", "/usr/bin/install"]
    )
    assert install_call[:3] == ["/usr/bin/sudo", "--non-interactive", "/usr/bin/install"]
    assert install_call[-1] == "/etc/systemd/system/openhop-plugin-manager.service"
    assert calls[-1][:3] == ["/usr/bin/sudo", "--non-interactive", "/bin/systemctl"]
