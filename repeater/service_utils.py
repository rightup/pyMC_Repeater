"""
Service management utilities for pyMC Repeater.
Provides functions for service control operations like restart.
"""

import logging
import os
import subprocess
from typing import Dict, Optional, Tuple

logger = logging.getLogger("ServiceUtils")
INIT_SCRIPT = "/etc/init.d/S80pymc-repeater"
BUILDROOT_METADATA_PATH = "/etc/pymc-image-build-id"


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


def restart_service() -> Tuple[bool, str]:
    """
    Restart the pymc-repeater service.
    
    On Buildroot/Luckfox, use the shipped init script directly.
    On systemd hosts, try polkit-based restart first (plain systemctl), then
    fall back to sudo-based restart (requires sudoers.d rule installed by
    manage.sh).
    
    Returns:
        Tuple[bool, str]: (success, message)
    """
    if is_buildroot():
        if not os.path.exists(INIT_SCRIPT):
            logger.error("Buildroot init script not found: %s", INIT_SCRIPT)
            return False, f"init script not found: {INIT_SCRIPT}"

        try:
            subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 1; exec {INIT_SCRIPT} restart >/dev/null 2>&1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("Service restart scheduled via Buildroot init script")
            return True, "Service restart initiated"
        except Exception as exc:
            logger.error(f"Buildroot restart failed: {exc}")
            return False, f"Restart failed: {exc}"

    # Try polkit-based restart first (works on bare metal / VMs with polkit running)
    try:
        result = subprocess.run(
            ["systemctl", "restart", "pymc-repeater"], capture_output=True, text=True, timeout=5
        )

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
    
    # Fallback: use sudo (requires /etc/sudoers.d/pymc-repeater rule)
    try:
        result = subprocess.run(
            ['sudo', '--non-interactive', 'systemctl', 'restart', 'pymc-repeater'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
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
