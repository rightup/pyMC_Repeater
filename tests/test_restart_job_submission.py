"""The restart command must run outside the service being restarted."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from repeater import service_utils as su


@pytest.fixture(autouse=True)
def systemd_host(monkeypatch):
    monkeypatch.setattr(su, "is_container", lambda: False)
    monkeypatch.setattr(su, "is_buildroot", lambda: False)
    monkeypatch.setattr(su.os, "geteuid", lambda: 1000)


@pytest.mark.parametrize("root", [False, True])
def test_restart_submits_delayed_external_job(monkeypatch, root):
    monkeypatch.setattr(su.os, "geteuid", lambda: 0 if root else 1000)
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su._SUDO_SYSTEMCTL_BIN)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(su.subprocess, "run", run)
    ok, message = su.restart_service()
    assert ok and "initiated" in message
    run.assert_called_once()
    args = run.call_args.args[0]
    assert "/usr/bin/systemd-run" in args
    assert "--on-active=2s" in args
    assert "--timer-property=AccuracySec=100ms" in args
    assert "--timer-property=RemainAfterElapse=no" in args
    assert "--collect" in args
    expected_systemctl = su._SYSTEMCTL_BIN if root else su._SUDO_SYSTEMCTL_BIN
    assert args[-3:] == [expected_systemctl, "restart", "openhop-repeater"]
    if not root:
        assert args[:2] == [su._SUDO_BIN, "--non-interactive"]
    else:
        assert args[0] == "/usr/bin/systemd-run"


def test_restart_non_root_prefers_sudoers_whitelisted_systemctl_path(monkeypatch):
    monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su._SUDO_SYSTEMCTL_BIN)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(su.subprocess, "run", run)

    ok, _message = su.restart_service()

    assert ok is True
    args = run.call_args.args[0]
    assert args[-3:] == [su._SUDO_SYSTEMCTL_BIN, "restart", "openhop-repeater"]


@pytest.mark.parametrize("outcome", ["timeout", "interrupted"])
def test_failed_submission_does_not_claim_success_or_retry(monkeypatch, outcome):
    run = Mock()
    if outcome == "timeout":
        run.side_effect = subprocess.TimeoutExpired("systemd-run", 5)
    else:
        run.return_value = subprocess.CompletedProcess([], -15, "", "Access denied")
    monkeypatch.setattr(su.subprocess, "run", run)
    ok, message = su.restart_service()
    assert not ok
    assert "unconfirmed" in message.lower()
    run.assert_called_once()


def test_non_root_denied_schedule_retries_legacy_and_succeeds(monkeypatch):
    monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su._SUDO_SYSTEMCTL_BIN)
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(su.subprocess, "run", run)

    ok, message = su.restart_service()

    assert ok is True
    assert "legacy fallback" in message
    assert run.call_count == 2


def test_non_root_denied_schedule_legacy_interrupted_is_likely_success(monkeypatch):
    monkeypatch.setattr(su.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(su.os.path, "exists", lambda p: p == su._SUDO_SYSTEMCTL_BIN)
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "sudo: a password is required"),
            subprocess.CompletedProcess([], -15, "", ""),
        ]
    )
    monkeypatch.setattr(su.subprocess, "run", run)

    ok, message = su.restart_service()

    assert ok is True
    assert "likely initiated" in message
    assert run.call_count == 2


def test_installer_authorizes_only_fixed_restart_timer_command():
    script = (Path(__file__).resolve().parents[1] / "manage.sh").read_text()
    rules = [line for line in script.splitlines() if "NOPASSWD:" in line]
    assert len(rules) == 2
    command = (
        "/usr/bin/systemd-run --quiet --collect --unit=openhop-repeater-restart "
        "--on-active=2s --timer-property=AccuracySec=100ms "
        "--timer-property=RemainAfterElapse=no /usr/bin/systemctl restart openhop-repeater"
    )
    assert all(command in line for line in rules)
    assert "*" not in command
