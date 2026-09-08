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
    assert args[-3:] == [su._SYSTEMCTL_BIN, "restart", "openhop-repeater"]
    if not root:
        assert args[:2] == [su._SUDO_BIN, "--non-interactive"]
    else:
        assert args[0] == "/usr/bin/systemd-run"


@pytest.mark.parametrize("outcome", ["timeout", "interrupted", "denied"])
def test_failed_submission_does_not_claim_success_or_retry(monkeypatch, outcome):
    run = Mock()
    if outcome == "timeout":
        run.side_effect = subprocess.TimeoutExpired("systemd-run", 5)
    else:
        run.return_value = subprocess.CompletedProcess(
            [], -15 if outcome == "interrupted" else 1, "", "Access denied"
        )
    monkeypatch.setattr(su.subprocess, "run", run)
    ok, message = su.restart_service()
    assert not ok
    if outcome != "denied":
        assert "unconfirmed" in message.lower()
    else:
        assert "Access denied" in message
    run.assert_called_once()


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
