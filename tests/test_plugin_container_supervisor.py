"""Container process supervision for Repeater and plugin manager."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

from repeater.plugins.container_supervisor import ContainerSupervisor, plugin_manager_enabled


class FakeProcess:
    def __init__(self, pid: int, polls: list[int | None]):
        self.pid = pid
        self._polls = list(polls)
        self.returncode = None
        self.wait_calls = []
        self.killed = False

    def poll(self):
        if self._polls:
            value = self._polls.pop(0)
            if value is not None:
                self.returncode = value
            return value
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


def test_plugin_manager_enabled_honours_config_and_environment(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled: false\n", encoding="utf-8")

    assert plugin_manager_enabled(config, {}) is False
    assert plugin_manager_enabled(config, {"OPENHOP_PLUGIN_MANAGER": "1"}) is False

    config.write_text("plugins:\n  enabled: true\n", encoding="utf-8")
    assert plugin_manager_enabled(config, {}) is True
    assert plugin_manager_enabled(config, {"OPENHOP_PLUGIN_MANAGER": "false"}) is False


def test_supervisor_restarts_failed_manager_and_stops_it_with_repeater():
    manager_one = FakeProcess(101, [1])
    manager_two = FakeProcess(102, [None, None])
    repeater = FakeProcess(201, [None, None, 0])
    processes = [manager_one, repeater, manager_two]
    commands = []
    signalled = []

    def popen(command, **kwargs):
        commands.append((command, kwargs))
        return processes.pop(0)

    def signal_group(process, signum):
        signalled.append((process.pid, signum))
        process.returncode = -signum

    supervisor = ContainerSupervisor(
        "/tmp/config.yaml",
        manager_enabled=True,
        popen_factory=popen,
        signal_process_group=signal_group,
        process_group_exists=lambda process: process.pid == manager_one.pid,
        sleep=lambda _seconds: None,
        manager_restart_delay=0,
    )

    assert supervisor.run() == 0
    assert [command for command, _kwargs in commands] == [
        ["python3", "-m", "repeater.plugins", "--config", "/tmp/config.yaml"],
        ["python3", "-m", "repeater.main", "--config", "/tmp/config.yaml"],
        ["python3", "-m", "repeater.plugins", "--config", "/tmp/config.yaml"],
    ]
    assert all(kwargs["start_new_session"] is True for _command, kwargs in commands)
    assert (101, signal.SIGTERM) in signalled
    assert (101, signal.SIGKILL) in signalled
    assert (102, signal.SIGTERM) in signalled
    assert len(manager_two.wait_calls) == 1


def test_shutdown_signal_is_forwarded_to_both_process_groups():
    manager = FakeProcess(301, [None])
    repeater = FakeProcess(302, [None])
    signalled = []

    supervisor = ContainerSupervisor(
        "/tmp/config.yaml",
        manager_enabled=True,
        signal_process_group=lambda process, signum: signalled.append((process.pid, signum)),
    )
    supervisor.manager_process = manager
    supervisor.repeater_process = repeater

    supervisor.request_shutdown(signal.SIGTERM)

    assert signalled == [(302, signal.SIGTERM), (301, signal.SIGTERM)]
