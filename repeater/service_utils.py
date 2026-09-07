"""
Service management utilities for openHop Repeater.
Provides functions for service control operations like restart.
"""

import logging
import os
import shutil
import subprocess  # nosec B404
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("ServiceUtils")
INIT_SCRIPT = "/etc/init.d/S80openhop-repeater"
BUILDROOT_METADATA_PATH = "/etc/pymc-image-build-id"
_CONTAINER_RESTART_DELAY_SECONDS = 1.0
_SH_BIN = shutil.which("sh") or "sh"
_SYSTEMCTL_BIN = shutil.which("systemctl") or "systemctl"
_SUDO_BIN = shutil.which("sudo") or "sudo"


def is_buildroot() -> bool:
    if os.path.exists(BUILDROOT_METADATA_PATH):
        return True
    if os.path.exists("/etc/os-release"):
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as handle:
                return any(line.strip() == "ID=buildroot" for line in handle)
        except OSError:
            return False
    return False


def get_buildroot_image_info() -> Dict[str, str]:
    info: Dict[str, str] = {}

    try:
        with open(BUILDROOT_METADATA_PATH, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                info[key.strip()] = value.strip()
    except OSError:
        return {}

    return info


def get_buildroot_image_version() -> Optional[str]:
    return get_buildroot_image_info().get("image_version")


def is_container() -> bool:
    """Detect common Docker/LXC/containerized environments."""
    if os.path.exists("/.dockerenv") or os.environ.get("container"):
        return True

    try:
        with open("/proc/1/environ", "rb") as handle:
            if b"container=" in handle.read():
                return True
    except (OSError, PermissionError):
        pass

    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as handle:
            cgroup_data = handle.read()
            if any(token in cgroup_data for token in ("docker", "containerd", "kubepods", "lxc")):
                return True
    except OSError:
        pass

    return os.path.exists("/run/host/container-manager")


def _schedule_container_exit(delay_seconds: float = _CONTAINER_RESTART_DELAY_SECONDS) -> None:
    """Exit the current process shortly after returning success to the caller."""

    def _exit_process() -> None:
        time.sleep(delay_seconds)
        logger.warning("Exiting repeater process to trigger container restart")
        os._exit(0)

    threading.Thread(target=_exit_process, name="container-restart-exit", daemon=True).start()


def get_container_restart_message() -> str:
    """Return the user-facing restart message for containerized installs."""
    return (
        "Container restart initiated. "
        "If you are running openHop Repeater via Docker or Home Assistant, pull or rebuild "
        "a newer image for packaged image updates to take effect."
    )


def ensure_plugin_manager_service() -> Tuple[bool, str]:
    """Ensure the packaged plugin-manager systemd unit is installed and running.

    This is intentionally idempotent. It runs as root when already privileged,
    or via sudo when the app is started under a service account, so a single
    upgrade can heal older installs that pre-date the plugin-manager system
    service without requiring a second update.
    """
    if is_container() or is_buildroot():
        return True, "Plugin-manager bootstrap skipped in container/buildroot environment"

    venv_python = "/opt/openhop_repeater/venv/bin/python"
    if not os.path.isfile(venv_python):
        return False, "Plugin-manager bootstrap skipped: venv not present"

    sudo_cmd: list[str] = []
    if os.geteuid() != 0:
        sudo_bin = shutil.which("sudo")
        if not sudo_bin:
            logger.info(
                "Plugin-manager bootstrap skipped: root privileges required for systemd provisioning"
            )
            return (
                True,
                "Plugin-manager bootstrap skipped: root privileges required for systemd provisioning",
            )
        sudo_cmd = [sudo_bin, "--non-interactive"]

    install_cmd = [
        "/usr/bin/install",
        "-o",
        "root",
        "-g",
        "root",
        "-m",
        "0644",
    ]
    daemon_reload = ["/bin/systemctl", "daemon-reload"]
    enable_cmd = ["/bin/systemctl", "enable", "openhop-plugin-manager"]
    is_active_cmd = ["/bin/systemctl", "is-active", "--quiet", "openhop-plugin-manager"]
    start_cmd = ["/bin/systemctl", "start", "openhop-plugin-manager"]
    restart_cmd = ["/bin/systemctl", "restart", "openhop-plugin-manager"]

    package_unit = None
    code = """
from importlib.metadata import distribution
from pathlib import Path
import sys
try:
    dist = distribution('openhop_repeater')
except Exception:
    sys.exit(1)
try:
    path = Path(dist.locate_file('repeater/plugins/openhop-plugin-manager.service'))
except Exception:
    sys.exit(1)
if path.is_file():
    print(path)
    sys.exit(0)
sys.exit(1)
"""
    try:
        result = subprocess.run(  # nosec B603
            [venv_python, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            package_unit = result.stdout.strip().splitlines()[-1]
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Plugin-manager bootstrap lookup failed: %s", exc)
        return False, f"Plugin-manager bootstrap lookup failed: {exc}"

    if not package_unit:
        return (
            False,
            "Plugin-manager bootstrap skipped: packaged service not found in installed package",
        )

    unit_path = "/etc/systemd/system/openhop-plugin-manager.service"
    changed = False
    try:
        if not os.path.exists(unit_path):
            logger.info("Plugin-manager unit missing; installing packaged service")
            changed = True
        else:
            with open(package_unit, "rb") as src, open(unit_path, "rb") as dst:
                if src.read() != dst.read():
                    logger.info("Plugin-manager unit differs from packaged version; updating")
                    changed = True

        if changed:
            subprocess.run(  # nosec B603
                sudo_cmd + install_cmd + [package_unit, unit_path],
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(  # nosec B603
                sudo_cmd + daemon_reload,
                check=False,
                capture_output=True,
                text=True,
            )
            logger.info("Plugin-manager unit installed or refreshed")
        else:
            logger.info("Plugin-manager unit already matches packaged version")

        subprocess.run(  # nosec B603
            sudo_cmd + enable_cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        active = subprocess.run(  # nosec B603
            sudo_cmd + is_active_cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if active.returncode != 0:
            logger.info("Starting plugin-manager service")
            subprocess.run(  # nosec B603
                sudo_cmd + start_cmd,
                check=False,
                capture_output=True,
                text=True,
            )
        elif changed:
            logger.info("Restarting plugin-manager service after unit refresh")
            subprocess.run(  # nosec B603
                sudo_cmd + restart_cmd,
                check=False,
                capture_output=True,
                text=True,
            )

        return True, "Plugin-manager service is installed and active"
    except Exception as exc:
        logger.warning("Plugin-manager bootstrap failed: %s", exc)
        return False, f"Plugin-manager bootstrap failed: {exc}"


def restart_service() -> Tuple[bool, str]:
    """
    Restart the openhop-repeater service.

    On Buildroot/Luckfox, use the shipped init script directly.
    On systemd hosts, try polkit-based restart first (plain systemctl), then
    fall back to sudo-based restart (requires sudoers.d rule installed by
    manage.sh).

    Returns:
        Tuple[bool, str]: (success, message)
    """
    if is_container():
        _schedule_container_exit()
        logger.info("Container environment detected; scheduled process exit for container restart")
        return True, get_container_restart_message()

    if is_buildroot():
        if not os.path.exists(INIT_SCRIPT):
            logger.error("Buildroot init script not found: %s", INIT_SCRIPT)
            return False, f"init script not found: {INIT_SCRIPT}"

        try:
            subprocess.Popen(
                [_SH_BIN, "-c", f"sleep 1; exec {INIT_SCRIPT} restart >/dev/null 2>&1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )  # nosec B603
            logger.info("Service restart scheduled via Buildroot init script")
            return True, "Service restart initiated"
        except Exception as exc:
            logger.error(f"Buildroot restart failed: {exc}")
            return False, f"Restart failed: {exc}"

    # Try polkit-based restart first (works on bare metal / VMs with polkit running)
    try:
        result = subprocess.run(
            [_SYSTEMCTL_BIN, "restart", "openhop-repeater"],
            capture_output=True,
            text=True,
            timeout=5,
        )  # nosec B603

        if result.returncode == 0:
            logger.info("Service restart via polkit succeeded")
            return True, "Service restart initiated"

        stderr = result.stderr or ""
        if "Access denied" in stderr or "authorization" in stderr.lower():
            logger.info("Polkit denied restart, trying sudo fallback...")
        else:
            # Some other error, still try sudo
            logger.warning(f"systemctl restart failed ({result.returncode}): {stderr.strip()}")

    except subprocess.TimeoutExpired:
        # Timeout likely means it's restarting - that's success
        logger.warning("Service restart command timed out (service may be restarting)")
        return True, "Service restart initiated (timeout - likely restarting)"
    except FileNotFoundError:
        logger.error("systemctl not found")
        return False, "systemctl not available"
    except Exception as e:
        logger.warning(f"Polkit restart attempt failed: {e}")

    # Fallback: use sudo (requires /etc/sudoers.d/openhop-repeater rule)
    try:
        result = subprocess.run(
            [_SUDO_BIN, "--non-interactive", _SYSTEMCTL_BIN, "restart", "openhop-repeater"],
            capture_output=True,
            text=True,
            timeout=5,
        )  # nosec B603

        if result.returncode == 0:
            logger.info("Service restart via sudo succeeded")
            return True, "Service restart initiated"
        else:
            error_msg = result.stderr or "Unknown error"
            logger.error(f"Service restart via sudo failed: {error_msg}")
            return False, f"Restart failed: {error_msg}"

    except subprocess.TimeoutExpired:
        logger.warning("Sudo restart timed out (service likely restarting)")
        return True, "Service restart initiated (timeout - likely restarting)"
    except FileNotFoundError:
        logger.error("sudo not found - cannot restart service")
        return False, "Neither polkit nor sudo available for service restart"
    except Exception as e:
        logger.error(f"Error executing sudo restart: {e}")
        return False, f"Restart command failed: {str(e)}"


def _is_cherrypy_engine_running() -> Optional[bool]:
    """Return CherryPy engine running state when available."""
    try:
        import cherrypy
        from cherrypy.process import wspbus

        state = cherrypy.engine.state
        return state in (wspbus.states.STARTING, wspbus.states.STARTED)
    except Exception:
        return None


def stop_http_server(daemon_instance) -> Tuple[bool, str]:
    """Stop the in-process HTTP stats server."""
    if not daemon_instance:
        return False, "Daemon instance not available"

    http_server = getattr(daemon_instance, "http_server", None)
    if not http_server:
        return False, "HTTP server not initialized"

    running = _is_cherrypy_engine_running()
    if running is False:
        return True, "HTTP server already stopped"

    try:
        http_server.stop()
        return True, "HTTP server stopped"
    except Exception as exc:
        logger.error(f"Failed to stop HTTP server: {exc}", exc_info=True)
        return False, f"Failed to stop HTTP server: {exc}"


def start_http_server(daemon_instance) -> Tuple[bool, str]:
    """Start the in-process HTTP stats server."""
    if not daemon_instance:
        return False, "Daemon instance not available"

    http_server = getattr(daemon_instance, "http_server", None)
    if not http_server:
        return False, "HTTP server not initialized"

    running = _is_cherrypy_engine_running()
    if running is True:
        return True, "HTTP server already running"

    try:
        http_server.start()
        return True, "HTTP server started"
    except Exception as exc:
        logger.error(f"Failed to start HTTP server: {exc}", exc_info=True)
        return False, f"Failed to start HTTP server: {exc}"
