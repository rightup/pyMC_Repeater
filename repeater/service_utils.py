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
