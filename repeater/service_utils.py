"""
Service management utilities for pyMC Repeater.
Provides functions for service control operations like restart.
"""

import logging
import subprocess
from typing import Tuple

logger = logging.getLogger("ServiceUtils")


def restart_service() -> Tuple[bool, str]:
    """
    Restart the pymc-repeater service via systemctl.
    
    Tries polkit-based restart first (plain systemctl), then falls back
    to sudo-based restart (requires sudoers.d rule installed by manage.sh).
    
    Returns:
        Tuple[bool, str]: (success, message)
    """
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
