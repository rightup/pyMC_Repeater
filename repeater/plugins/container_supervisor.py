"""Container supervision for Repeater and its plugin manager."""

from __future__ import annotations

import argparse
import logging
import os
import signal

# Subprocess calls below use fixed argument arrays and never shell=True.
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("ContainerSupervisor")
_FALSE_VALUES = {"0", "false", "no", "off"}


def plugin_manager_enabled(
    config_path: Path | str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the container should run the plugin manager."""
    env = os.environ if environ is None else environ
    if str(env.get("OPENHOP_PLUGIN_MANAGER", "1")).strip().lower() in _FALSE_VALUES:
        return False

    try:
        with open(config_path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
    except (OSError, yaml.YAMLError):
        return True

    if not isinstance(config, dict):
        return True
    plugins = config.get("plugins")
    return not (isinstance(plugins, dict) and plugins.get("enabled") is False)


def _signal_process_group(process: subprocess.Popen, signum: int) -> None:
    """Signal a child session, falling back to the direct process."""
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            return


def _process_group_exists(process: subprocess.Popen) -> bool:
    """Return whether any process remains in a child session."""
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ContainerSupervisor:
    """Run, restart, and gracefully stop container child processes."""

    def __init__(
        self,
        config_path: Path | str,
        *,
        manager_enabled: bool | None = None,
        python_executable: str = "python3",
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        signal_process_group: Callable[[subprocess.Popen, int], None] = _signal_process_group,
        process_group_exists: Callable[[subprocess.Popen], bool] = _process_group_exists,
        sleep: Callable[[float], None] = time.sleep,
        manager_restart_delay: float = 2.0,
        shutdown_timeout: float = 8.0,
    ):
        self.config_path = str(config_path)
        self.manager_enabled = (
            plugin_manager_enabled(config_path) if manager_enabled is None else manager_enabled
        )
        self.python_executable = python_executable
        self._popen = popen_factory
        self._signal_process_group = signal_process_group
        self._process_group_exists = process_group_exists
        self._sleep = sleep
        self.manager_restart_delay = manager_restart_delay
        self.shutdown_timeout = shutdown_timeout
        self.manager_process: subprocess.Popen | None = None
        self.repeater_process: subprocess.Popen | None = None
        self._stopping = False
        self._shutdown_signal = signal.SIGTERM

    def _start(self, module: str) -> subprocess.Popen:
        command = [
            self.python_executable,
            "-m",
            module,
            "--config",
            self.config_path,
        ]
        return self._popen(command, start_new_session=True)

    def _start_manager(self) -> None:
        logger.info("Starting plugin manager")
        self.manager_process = self._start("repeater.plugins")

    def request_shutdown(self, signum: int = signal.SIGTERM) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._shutdown_signal = signum
        logger.info("Signal %s received; stopping container services", signum)
        for process in (self.repeater_process, self.manager_process):
            if process is not None:
                self._signal_process_group(process, signum)

    def _wait_or_kill(self, process: subprocess.Popen | None) -> None:
        if process is None:
            return
        try:
            process.wait(timeout=self.shutdown_timeout)
            return
        except subprocess.TimeoutExpired:
            logger.warning("Process group %s did not stop in time; killing it", process.pid)
        self._signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, ChildProcessError):
            pass

    def _finish(self) -> None:
        if not self._stopping:
            self.request_shutdown(signal.SIGTERM)
        self._wait_or_kill(self.repeater_process)
        self._wait_or_kill(self.manager_process)

    def run(self) -> int:
        if self.manager_enabled:
            self._start_manager()
        else:
            logger.info("Plugin manager disabled")

        self.repeater_process = self._start("repeater.main")

        try:
            while not self._stopping:
                repeater_code = self.repeater_process.poll()
                if repeater_code is not None:
                    return int(repeater_code)

                if self.manager_enabled and self.manager_process is not None:
                    manager_code = self.manager_process.poll()
                    if manager_code is not None:
                        logger.error(
                            "Plugin manager exited unexpectedly with status %s; restarting",
                            manager_code,
                        )
                        # Terminate any plugin descendants left in the old process group.
                        self._signal_process_group(self.manager_process, signal.SIGTERM)
                        self._sleep(self.manager_restart_delay)
                        if self._process_group_exists(self.manager_process):
                            logger.warning(
                                "Plugin descendants did not stop with manager %s; killing group",
                                self.manager_process.pid,
                            )
                            self._signal_process_group(self.manager_process, signal.SIGKILL)
                        if not self._stopping and self.repeater_process.poll() is None:
                            self._start_manager()

                self._sleep(0.2)
        finally:
            self._finish()

        code = self.repeater_process.poll()
        if code is None:
            return 128 + int(self._shutdown_signal)
        return int(code)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="openHop Docker process supervisor")
    parser.add_argument("--config", required=True, help="Path to Repeater config.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    supervisor = ContainerSupervisor(args.config)

    def handle_signal(signum: int, _frame: Any) -> None:
        supervisor.request_shutdown(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)

    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
