"""
OTA Update endpoints for pyMC Repeater.

Provides server-side GitHub version checks, background pip-based upgrades with
SSE progress streaming, and release-channel switching.

Endpoints (mounted at /api/update/):
    GET  /api/update/status        – current + latest version, channel, update state
    POST /api/update/check         – force a fresh GitHub version check
    POST /api/update/install       – start the upgrade in a background thread
    GET  /api/update/progress      – SSE stream of live install log lines
    GET  /api/update/channels      – list available branches/channels from GitHub
    POST /api/update/set_channel   – switch the active release channel
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import List, Optional

import cherrypy

logger = logging.getLogger("HTTPServer")

# ---------------------------------------------------------------------------
# Repository constants
# ---------------------------------------------------------------------------
GITHUB_OWNER = "rightup"
GITHUB_REPO = "pyMC_Repeater"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
PACKAGE_NAME = "pymc_repeater"

# How long (seconds) before a cached check result expires
CHECK_CACHE_TTL = 600  # 10 minutes


class _RateLimitError(Exception):
    """Raised when GitHub returns HTTP 403 due to rate limiting."""
    def __init__(self, msg: str, reset_at: Optional[datetime] = None):
        super().__init__(msg)
        self.reset_at = reset_at


def _get_installed_version() -> str:
    """
    Return the highest dist-info version found for pymc_repeater across all
    directories the running interpreter actually uses.

    Search strategy (union of all three to cover venvs, system, dist-packages):
      1. site.getsitepackages() / getusersitepackages()
      2. sys.path entries that look like package directories
      3. Fallback: importlib.metadata, then the in-process __version__

    Sanity check: the running process __version__ is used as a version floor.
    If the disk scan returns something older than what is actually running, we
    return __version__ instead – an older dist-info is definitively stale.
    """
    import glob
    import site as _site
    import sys

    # -- 1. Collect candidate directories ---------------------------------- #
    dirs: list = []
    try:
        dirs.extend(_site.getsitepackages())
    except AttributeError:
        pass
    try:
        dirs.append(_site.getusersitepackages())
    except AttributeError:
        pass
    # Also include every sys.path entry that looks like a package directory so
    # venvs and Debian dist-packages paths are covered.
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p) and p not in dirs:
            dirs.append(p)

    # -- 2. Scan for dist-info METADATA files ------------------------------ #
    pkg_glob = PACKAGE_NAME.replace("-", "_") + "-*.dist-info"
    candidates: list = []
    for site_dir in dirs:
        for meta_dir in glob.glob(os.path.join(site_dir, pkg_glob)):
            metadata_path = os.path.join(meta_dir, "METADATA")
            try:
                with open(metadata_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("Version:"):
                            candidates.append(line.split(":", 1)[1].strip())
                            break
            except OSError:
                continue

    # -- 3. Pick the highest candidate ------------------------------------- #
    disk_version: Optional[str] = None
    if candidates:
        if len(candidates) == 1:
            disk_version = candidates[0]
        else:
            try:
                from packaging.version import Version
                disk_version = str(max(candidates, key=lambda v: Version(v)))
            except Exception:
                # packaging unavailable – sort lexicographically as best-effort
                disk_version = sorted(candidates)[-1]

    # -- 4. Fallbacks when disk scan found nothing ------------------------- #
    if disk_version is None:
        try:
            from importlib.metadata import version as _pkg_ver
            disk_version = _pkg_ver(PACKAGE_NAME)
        except Exception:
            pass

    if disk_version is None:
        try:
            from repeater import __version__
            return __version__
        except Exception:
            return "unknown"

    # -- 5. Sanity check: never return a version older than what's running -- #
    # If the running process is already on a higher version than anything found
    # on disk, the dist-info dirs are stale leftovers and __version__ is truth.
    try:
        from repeater import __version__ as _running
        from packaging.version import Version
        if Version(_running) > Version(disk_version):
            logger.debug(
                f"[Update] Disk version {disk_version!r} < running {_running!r};"
                " using running __version__ as installed version."
            )
            # Strip PEP 440 local identifier (+gXXXXXX) – it only encodes
            # the git hash and causes spurious mismatches with GitHub versions.
            return re.sub(r'\+[a-zA-Z0-9.]+$', '', _running)
    except Exception:
        pass

    return re.sub(r'\+[a-zA-Z0-9.]+$', '', disk_version)

# Channels file – persisted so the choice survives daemon restarts
_CHANNELS_FILE = "/var/lib/pymc_repeater/.update_channel"


def _detect_channel_from_dist_info() -> Optional[str]:

    import glob
    import site as _site

    dirs: list = []
    try:
        dirs.extend(_site.getsitepackages())
    except AttributeError:
        pass
    try:
        dirs.append(_site.getusersitepackages())
    except AttributeError:
        pass

    pkg_glob = PACKAGE_NAME.replace("-", "_") + "-*.dist-info"

    # Collect all candidates so we can pick the highest version's direct_url
    candidates: list = []  # list of (version_str, direct_url_path)
    for site_dir in dirs:
        for meta_dir in glob.glob(os.path.join(site_dir, pkg_glob)):
            direct_url_path = os.path.join(meta_dir, "direct_url.json")
            if not os.path.isfile(direct_url_path):
                continue
            metadata_path = os.path.join(meta_dir, "METADATA")
            ver = None
            try:
                with open(metadata_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("Version:"):
                            ver = line.split(":", 1)[1].strip()
                            break
            except OSError:
                continue
            if ver:
                candidates.append((ver, direct_url_path))

    if not candidates:
        return None

    # Use the highest-version dist-info so a stale old one doesn't win
    try:
        from packaging.version import Version
        candidates.sort(key=lambda t: Version(t[0]), reverse=True)
    except Exception:
        pass

    _, best_url_path = candidates[0]
    try:
        with open(best_url_path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        vcs_info = data.get("vcs_info", {})
        # ``requested_revision`` is only present when the user explicitly named
        # a branch/tag; absent means HEAD of the default branch.
        revision = vcs_info.get("requested_revision")
        if revision and re.match(r'^[a-zA-Z0-9_./\-]+$', revision):
            return revision
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Module-level state (one update at a time)
# ---------------------------------------------------------------------------
class _UpdateState:
    """Singleton-style mutable state shared between all endpoint calls."""

    def __init__(self):
        self._lock = threading.Lock()
        # version info
        self.current_version: str = _get_installed_version()
        self.latest_version: Optional[str] = None
        self.has_update: bool = False
        self.channel: str = self._load_channel()
        self.last_checked: Optional[datetime] = None
        # progress / install state
        self.state: str = "idle"           # idle | checking | installing | complete | error
        self.error_message: Optional[str] = None
        self.progress_lines: List[str] = []
        self._install_thread: Optional[threading.Thread] = None
        # rate-limit backoff: don't call GitHub before this time (UTC)
        self.rate_limit_until: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # Channel persistence                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_channel() -> str:
        try:
            if os.path.isfile(_CHANNELS_FILE):
                with open(_CHANNELS_FILE) as fh:
                    ch = fh.read().strip()
                    if ch:
                        return ch
        except OSError:
            pass
        # No saved channel file – try to detect from the dist-info written
        # by pip at install time so the initial default is correct.
        detected = _detect_channel_from_dist_info()
        if detected:
            logger.info(f"[Update] Detected install channel from dist-info: '{detected}'")
            # Persist it so future startups don't need to detect again
            try:
                os.makedirs(os.path.dirname(_CHANNELS_FILE), exist_ok=True)
                with open(_CHANNELS_FILE, "w") as fh:
                    fh.write(detected)
            except OSError as exc:
                logger.warning(f"Could not persist detected channel: {exc}")
            return detected
        return "main"

    def _save_channel(self, channel: str) -> None:
        try:
            os.makedirs(os.path.dirname(_CHANNELS_FILE), exist_ok=True)
            with open(_CHANNELS_FILE, "w") as fh:
                fh.write(channel)
        except OSError as exc:
            logger.warning(f"Could not persist channel choice: {exc}")

    # ------------------------------------------------------------------ #
    # Thread-safe accessors                                                #
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            # Always read installed version fresh so it reflects post-restart state
            fresh_current = _get_installed_version()
            if fresh_current != "unknown":
                self.current_version = fresh_current
                # Recompute has_update with fresh installed version
                if self.latest_version is not None:
                    self.has_update = _has_update(self.current_version, self.latest_version)
            return {
                "current_version": self.current_version,
                "latest_version": self.latest_version,
                "has_update": self.has_update,
                "channel": self.channel,
                "last_checked": self.last_checked.isoformat() if self.last_checked else None,
                "state": self.state,
                "error": self.error_message,
                "rate_limit_until": self.rate_limit_until.isoformat() if self.rate_limit_until else None,
            }

    def set_channel(self, channel: str) -> None:
        with self._lock:
            self.channel = channel
            self._save_channel(channel)
            # Invalidate cached check so next call re-checks against new channel
            self.last_checked = None
            self.latest_version = None
            self.has_update = False

    def _set_checking(self) -> bool:
        """Return True and move to 'checking' if currently idle."""
        with self._lock:
            if self.state not in ("idle", "complete", "error"):
                return False
            self.state = "checking"
            return True

    def _finish_check(self, latest: str) -> None:
        with self._lock:
            self.latest_version = latest
            fresh = _get_installed_version()
            if fresh != "unknown":
                self.current_version = fresh
            self.has_update = _has_update(self.current_version, latest)
            self.last_checked = datetime.utcnow()
            self.state = "idle"
            self.error_message = None

    def _fail_check(self, msg: str) -> None:
        with self._lock:
            self.state = "error"
            self.error_message = msg
            self.last_checked = datetime.utcnow()

    def _fail_check_ratelimit(self, msg: str, reset_at: Optional[datetime]) -> None:
        """Like _fail_check but keeps existing version data intact and records
        the reset time so we don't hammer GitHub until the window expires."""
        with self._lock:
            # Keep state as idle so the UI still shows version info
            self.state = "idle"
            self.error_message = msg
            self.last_checked = datetime.utcnow()
            self.rate_limit_until = reset_at

    def start_install(self, thread: threading.Thread) -> bool:
        with self._lock:
            if self.state == "installing":
                return False
            self.state = "installing"
            self.error_message = None
            self.progress_lines = ["[pyMC updater] Starting update…"]
            self._install_thread = thread
            return True

    def finish_install(self, success: bool, msg: str) -> None:
        with self._lock:
            self.state = "complete" if success else "error"
            self.error_message = None if success else msg
            if success:
                self.progress_lines.append(f"[pyMC updater] ✓ {msg}")
                self.has_update = False
                # current_version will be refreshed on next snapshot() call
            else:
                self.progress_lines.append(f"[pyMC updater] ✗ {msg}")

    def append_line(self, line: str) -> None:
        with self._lock:
            self.progress_lines.append(line)


_state = _UpdateState()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, timeout: int = 10) -> str:
    """Perform a simple GET and return text body, or raise on failure.

    Adds a GitHub token from the environment when available (raises the
    unauthenticated rate limit from 60 → 5 000 requests / hour).
    Raises _RateLimitError on HTTP 403 so callers can back off gracefully.
    """
    installed = _get_installed_version()
    headers = {"User-Agent": f"pymc-repeater/{installed}"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # Try to read the reset timestamp from the response headers
            reset_at: Optional[datetime] = None
            try:
                reset_ts = exc.headers.get("X-RateLimit-Reset")
                if reset_ts:
                    reset_at = datetime.utcfromtimestamp(int(reset_ts))
            except Exception:
                pass
            reset_str = reset_at.strftime("%H:%M UTC") if reset_at else "a short while"
            raise _RateLimitError(
                f"GitHub API rate limit exceeded — resets at {reset_str}. "
                "Set GITHUB_TOKEN env var to raise the limit to 5000 req/hr.",
                reset_at=reset_at,
            ) from exc
        raise


def _get_latest_tag() -> str:
    """Return the most recent semver tag from the repo, or raise."""
    tags_url = f"{GITHUB_API_BASE}/tags?per_page=10"
    body = _fetch_url(tags_url, timeout=8)
    tags = json.loads(body)
    for tag in tags:
        name = tag.get("name", "").lstrip("v")
        if re.match(r'^\d+\.\d+', name):
            return name
    raise RuntimeError("No semver tags found in repository")


def _branch_is_dynamic(channel: str) -> bool:
    """Return True if the branch uses setuptools_scm dynamic versioning."""
    try:
        toml_url = f"{GITHUB_RAW_BASE}/{channel}/pyproject.toml"
        toml_text = _fetch_url(toml_url, timeout=8)
        # Static pin looks like:  version = "1.0.5"
        if re.search(r'^version\s*=\s*["\'][0-9]', toml_text, re.MULTILINE):
            return False
        # Dynamic looks like:  dynamic = ["version"]
        if re.search(r'^dynamic\s*=', toml_text, re.MULTILINE):
            return True
    except Exception:
        pass
    return True  # assume dynamic if we can't tell


def _next_dev_version(base_tag: str, ahead_by: int) -> str:
    """
    Generate a display version string for a dynamic branch.
    e.g. base_tag="1.0.5", ahead_by=191  ->  "1.0.6.dev191"
    Mirrors what setuptools_scm guess-next-dev produces.
    """
    parts = base_tag.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts.append("1")
    return ".".join(parts) + f".dev{ahead_by}"


def _parse_dev_number(version_str: str) -> Optional[int]:
    """Extract the dev commit count from a setuptools_scm version like 1.0.6.dev118."""
    m = re.search(r'\.dev(\d+)', version_str)
    return int(m.group(1)) if m else None


def _cleanup_stale_dist_info() -> None:
    import glob
    import shutil
    import site as _site

    dirs: list = []
    try:
        dirs.extend(_site.getsitepackages())
    except AttributeError:
        pass
    try:
        dirs.append(_site.getusersitepackages())
    except AttributeError:
        pass

    pkg_glob = PACKAGE_NAME.replace("-", "_") + "-*.dist-info"

    # Collect {path: version_str} for every pymc_repeater dist-info we find
    found: dict = {}
    for site_dir in dirs:
        for meta_dir in glob.glob(os.path.join(site_dir, pkg_glob)):
            metadata_path = os.path.join(meta_dir, "METADATA")
            try:
                with open(metadata_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line.startswith("Version:"):
                            found[meta_dir] = line.split(":", 1)[1].strip()
                            break
            except OSError:
                continue

    if len(found) <= 1:
        return  # nothing to clean up

    try:
        from packaging.version import Version
        keep = max(found, key=lambda p: Version(found[p]))
    except Exception:
        return  # can't determine winner safely — leave everything alone

    for path, ver in found.items():
        if path == keep:
            continue
        try:
            shutil.rmtree(path)
            logger.info(f"[Update] Removed stale dist-info: {path} (version {ver})")
            _state.append_line(f"[pyMC updater] Removed stale dist-info: {os.path.basename(path)}")
        except Exception as exc:
            logger.warning(f"[Update] Could not remove stale dist-info {path}: {exc}")


def _has_update(installed: str, latest: str) -> bool:
    """

    """
    if installed == latest:
        return False
    try:
        from packaging.version import Version
        return Version(latest) > Version(installed)
    except Exception:
        pass
    # Fallback: dev-number comparison only when base version is identical
    installed_dev = _parse_dev_number(installed)
    latest_dev = _parse_dev_number(latest)
    if installed_dev is not None and latest_dev is not None:
        return latest_dev > installed_dev
    return installed != latest


def _fetch_latest_version(channel: str) -> str:

    base_tag = _get_latest_tag()  # always needed for dynamic branches

    if _branch_is_dynamic(channel):
        compare_url = f"{GITHUB_API_BASE}/compare/{base_tag}...{channel}"
        try:
            body = _fetch_url(compare_url, timeout=10)
            data = json.loads(body)
            ahead_by = int(data.get("ahead_by", 0))
            return _next_dev_version(base_tag, ahead_by)
        except Exception:
            return base_tag  # fallback: show the tag

    # Static version channel — read the pinned version from pyproject.toml on
    # that branch directly, so tags created on other branches don't affect it.
    try:
        toml_url = f"{GITHUB_RAW_BASE}/{channel}/pyproject.toml"
        toml_text = _fetch_url(toml_url, timeout=8)
        m = re.search(r'^version\s*=\s*["\']([0-9][^"\']*)["\']', toml_text, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return base_tag  # last-resort fallback


def _fetch_changelog(channel: str, installed: str, max_commits: int = 50) -> List[dict]:

    base_tag = _get_latest_tag()
    installed_dev = _parse_dev_number(installed)

    try:
        if _branch_is_dynamic(channel):
            compare_url = f"{GITHUB_API_BASE}/compare/{base_tag}...{channel}?per_page=100"
        else:
            # For static channels compare from the installed tag if we know it
            from_ref = installed if re.match(r'^\d+\.\d+', installed) else base_tag
            compare_url = f"{GITHUB_API_BASE}/compare/{from_ref}...{channel}?per_page=100"

        body = _fetch_url(compare_url, timeout=12)
        data = json.loads(body)
        commits = data.get("commits", [])

        # For dynamic branches: skip commits the user already has (first N)
        if installed_dev is not None and _branch_is_dynamic(channel):
            # commits are oldest-first; user has the first `installed_dev` of them
            commits = commits[installed_dev:]

        # Newest first
        commits = list(reversed(commits))[:max_commits]

        result = []
        for c in commits:
            commit_data = c.get("commit", {})
            full_msg = commit_data.get("message", "").strip()
            title = full_msg.split("\n")[0]
            body_text = "\n".join(full_msg.split("\n")[2:]).strip()
            author = (
                commit_data.get("author", {}).get("name")
                or c.get("author", {}).get("login")
                or "unknown"
            )
            date = commit_data.get("author", {}).get("date", "")
            sha = c.get("sha", "")
            result.append({
                "sha": sha,
                "short_sha": sha[:7],
                "title": title,
                "body": body_text,
                "author": author,
                "date": date,
                "url": c.get("html_url", ""),
            })
        return result
    except Exception as exc:
        logger.warning(f"[Update] Changelog fetch failed: {exc}")
        return []


def _fetch_branches() -> List[str]:
    """Return list of branch names from GitHub API."""
    try:
        body = _fetch_url(f"{GITHUB_API_BASE}/branches?per_page=30", timeout=8)
        data = json.loads(body)
        names = [b["name"] for b in data if isinstance(b, dict) and b.get("name")]
        # Prefer main/dev at the front
        priority = [n for n in ("main", "dev", "develop") if n in names]
        rest = [n for n in names if n not in priority]
        return priority + rest
    except Exception:
        return ["main"]


def _do_check() -> None:
    """Background thread: fetch latest version and update state."""
    channel = _state.channel
    try:
        latest = _fetch_latest_version(channel)
        # Successful fetch — clear any previous rate-limit hold
        with _state._lock:
            _state.rate_limit_until = None
        _state._finish_check(latest)
        logger.info(
            f"[Update] Check complete – installed={_state.current_version} "
            f"latest={latest} channel={channel} has_update={_state.has_update}"
        )
    except _RateLimitError as exc:
        logger.warning(f"[Update] {exc}")
        _state._fail_check_ratelimit(str(exc), exc.reset_at)
    except Exception as exc:
        msg = str(exc)
        _state._fail_check(msg)
        logger.warning(f"[Update] Version check failed: {msg}")


def _migrate_service_unit() -> None:
    """Strip legacy PYTHONPATH and fix WorkingDirectory in the systemd service unit.
    """
    import subprocess as _sp
    _SVC_UNIT = "/etc/systemd/system/pymc-repeater.service"
    try:
        _sp.run(["sed", "-i", "/^Environment=.*PYTHONPATH/d", _SVC_UNIT], check=False)
        _sp.run(
            ["sed", "-i",
             "s|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/pymc_repeater|",
             _SVC_UNIT],
            check=False,
        )
        _sp.run(["systemctl", "daemon-reload"], check=False)
        logger.info("[Update] Service unit migration applied (root path).")
    except Exception as exc:
        logger.warning(f"[Update] Service unit migration failed: {exc}")


def _do_install() -> None:

    channel = _state.channel

    def _run(cmd: List[str], env: Optional[dict] = None) -> bool:
        """Run command, streaming lines into progress_lines.  Returns success bool."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _state.append_line(line)
                    logger.debug(f"[pip] {line}")
            proc.wait()
            return proc.returncode == 0
        except Exception as exc:
            _state.append_line(f"[error] {exc}")
            logger.error(f"[Update] Subprocess error: {exc}")
            return False

    import os as _os
    env = _os.environ.copy()
    env["PIP_ROOT_USER_ACTION"] = "ignore"
    env["SETUPTOOLS_SCM_PRETEND_VERSION"] = _state.latest_version or "1.0.0"

    _state.append_line(f"[pyMC updater] Installing from channel '{channel}'…")

    _UPGRADE_WRAPPER = "/usr/local/bin/pymc-do-upgrade"
    is_root = (_os.geteuid() == 0)

    if is_root:
        _migrate_service_unit()
        install_spec = (
            f"pymc_repeater[hardware] @ git+https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}.git@{channel}"
        )
        _state.append_line(f"[pyMC updater] Running as root – direct pip install")
        _state.append_line(f"[pyMC updater] Target: {install_spec}")
        cmd = [
            "python3", "-m", "pip", "install",
            "--break-system-packages",
            "--no-cache-dir",
            "--force-reinstall",
            install_spec,
        ]
    elif _os.path.isfile(_UPGRADE_WRAPPER):
        _state.append_line(f"[pyMC updater] Using sudo wrapper: {_UPGRADE_WRAPPER}")
        # Pass the target version as $2 so the wrapper can set
        # SETUPTOOLS_SCM_PRETEND_VERSION (sudo strips our env).
        cmd = ["sudo", _UPGRADE_WRAPPER, channel, _state.latest_version or ""]
    else:
        msg = (
            f"Upgrade wrapper not found at {_UPGRADE_WRAPPER}. "
            "Re-run manage.sh install/upgrade to configure sudo permissions."
        )
        _state.finish_install(False, msg)
        return

    success = _run(cmd, env=env)

    if success:
        _cleanup_stale_dist_info()
        _state.append_line("[pyMC updater] Restarting service in 3 seconds…")
        time.sleep(3)
        restart_ok = False
        restart_msg = "Restart not attempted"
        try:
            from repeater.service_utils import restart_service
            restart_ok, restart_msg = restart_service()
            logger.info(f"[Update] Post-upgrade restart: {restart_msg}")
        except Exception as exc:
            restart_msg = str(exc)
            logger.warning(f"[Update] Could not restart service: {exc}")
        if restart_ok:
            _state.finish_install(True, f"Upgraded to latest on channel '{channel}' – service restarted")
        else:
            _state.finish_install(False, f"Upgrade succeeded but service restart failed: {restart_msg}")
    else:
        _state.finish_install(False, "pip install failed – see progress log for details")


# ---------------------------------------------------------------------------
# CherryPy Endpoint class
# ---------------------------------------------------------------------------

class UpdateAPIEndpoints:

    def _set_cors_headers(self, config: dict) -> None:
        if config.get("web", {}).get("cors_enabled", False):
            cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
            cherrypy.response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            cherrypy.response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

    def _require_post(self):
        if cherrypy.request.method != "POST":
            raise cherrypy.HTTPError(405, "Method Not Allowed")

    @staticmethod
    def _ok(data: dict) -> dict:
        return {"success": True, **data}

    @staticmethod
    def _err(msg: str, status: int = 400) -> dict:
        cherrypy.response.status = status
        return {"success": False, "error": str(msg)}

    # ------------------------------------------------------------------ #
    # GET /api/update/status                                               #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def status(self, **kwargs):

        if cherrypy.request.method == "OPTIONS":
            return ""

        snap = _state.snapshot()
        return self._ok(snap)

    # ------------------------------------------------------------------ #
    # POST /api/update/check                                               #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def check(self, **kwargs):

        if cherrypy.request.method == "OPTIONS":
            return ""

        if cherrypy.request.method not in ("POST", "GET"):
            raise cherrypy.HTTPError(405)

        # Allow caller to bypass cache with {"force": true}
        body = {}
        try:
            body = cherrypy.request.json or {}
        except Exception:
            pass
        force = bool(body.get("force", False))

        # Honour the cache to avoid hammering GitHub (unless forced)
        snap = _state.snapshot()
        if snap["state"] == "checking":
            return self._ok({"message": "Check already in progress", "state": "checking"})

        if force:
            # Invalidate cache so a stale in-memory result is never reused
            with _state._lock:
                _state.last_checked = None
                _state.latest_version = None
                _state.has_update = False
                _state.rate_limit_until = None  # force overrides backoff

        # Respect GitHub rate-limit backoff window
        if not force and _state.rate_limit_until is not None:
            remaining = (_state.rate_limit_until - datetime.utcnow()).total_seconds()
            if remaining > 0:
                reset_str = _state.rate_limit_until.strftime("%H:%M UTC")
                return self._ok({
                    "message": f"GitHub rate limit active — resets at {reset_str}",
                    "state": snap["state"],
                    **snap,
                })

        if not force and snap["last_checked"] is not None:
            age = (datetime.utcnow() - _state.last_checked).total_seconds()
            if age < CHECK_CACHE_TTL and snap["latest_version"] is not None:
                return self._ok({"message": "Using cached result", "state": snap["state"], **snap})

        if not _state._set_checking():
            return self._ok({"message": "Busy – try again shortly", "state": _state.state})

        t = threading.Thread(target=_do_check, daemon=True, name="update-check")
        t.start()

        logger.info("[Update] Version check initiated via API")
        return self._ok({"message": "Update check started", "state": "checking"})

    # ------------------------------------------------------------------ #
    # POST /api/update/install                                             #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def install(self, **kwargs):

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
        except cherrypy.HTTPError:
            raise

        body = {}
        try:
            body = cherrypy.request.json or {}
        except Exception:
            pass

        snap = _state.snapshot()

        if snap["state"] == "installing":
            return self._err("An update is already in progress", 409)

        force = bool(body.get("force", False))
        if not force and not snap["has_update"]:
            # Still allow install if no check has been done yet
            if snap["latest_version"] is not None:
                return self._err(
                    f"Already up to date ({snap['current_version']}). "
                    "Pass {\"force\": true} to reinstall anyway.",
                    409,
                )

        t = threading.Thread(target=_do_install, daemon=True, name="update-install")
        started = _state.start_install(t)
        if not started:
            return self._err("Could not start install thread – check state", 409)

        t.start()
        logger.warning(
            f"[Update] Install triggered via API – channel={_state.channel}"
        )
        return self._ok({
            "message": f"Update started on channel '{_state.channel}'. "
                       "Watch /api/update/progress for live output.",
            "state": "installing",
        })

    # ------------------------------------------------------------------ #
    # GET /api/update/progress  (SSE)                                     #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    def progress(self, **kwargs):

        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"
        cherrypy.response.headers["Connection"] = "keep-alive"

        def generate():
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to update progress stream'})}\n\n"

            last_idx = 0
            last_state = None

            while True:
                try:
                    snap = _state.snapshot()
                    current_state = snap["state"]

                    # Emit any new log lines
                    current_lines = _state.progress_lines
                    new_lines = current_lines[last_idx:]
                    for line in new_lines:
                        payload = json.dumps({"type": "line", "line": line})
                        yield f"data: {payload}\n\n"
                    last_idx = len(current_lines)

                    # Emit state transitions
                    if current_state != last_state:
                        payload = json.dumps({"type": "status", "state": current_state})
                        yield f"data: {payload}\n\n"
                        last_state = current_state

                    # Terminate stream when install completes or errors
                    if current_state in ("complete", "error") and last_idx >= len(current_lines):
                        done_payload = json.dumps({
                            "type": "done",
                            "state": current_state,
                            "error": snap.get("error"),
                        })
                        yield f"data: {done_payload}\n\n"
                        return

                    # Keepalive
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                    time.sleep(1.5)

                except GeneratorExit:
                    return
                except Exception as exc:
                    logger.debug(f"[Update SSE] stream error: {exc}")
                    return

        return generate()

    progress._cp_config = {"response.stream": True}

    # ------------------------------------------------------------------ #
    # GET /api/update/channels                                             #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def channels(self, **kwargs):
        """
        Return available GitHub branches as selectable channels.

        Response:
            {success, channels: [str], current_channel: str}
        """
        if cherrypy.request.method == "OPTIONS":
            return ""

        branch_list = _fetch_branches()
        return self._ok({
            "channels": branch_list,
            "current_channel": _state.channel,
        })

    # ------------------------------------------------------------------ #
    # POST /api/update/set_channel                                         #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def set_channel(self, **kwargs):

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
        except cherrypy.HTTPError:
            raise

        body = {}
        try:
            body = cherrypy.request.json or {}
        except Exception:
            pass

        channel = str(body.get("channel", "")).strip()
        if not channel:
            return self._err("'channel' field is required")

        if _state.state == "installing":
            return self._err("Cannot change channel while an install is in progress", 409)

        _state.set_channel(channel)
        logger.info(f"[Update] Channel changed to '{channel}' via API")
        return self._ok({
            "channel": channel,
            "message": f"Channel switched to '{channel}'. Run /api/update/check to verify.",
        })

    # ------------------------------------------------------------------ #
    # GET /api/update/changelog                                            #
    # ------------------------------------------------------------------ #
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def changelog(self, **kwargs):

        if cherrypy.request.method == "OPTIONS":
            return ""

        channel = kwargs.get("channel", _state.channel).strip() or _state.channel
        try:
            max_commits = min(int(kwargs.get("max", 40)), 100)
        except (ValueError, TypeError):
            max_commits = 40

        snap = _state.snapshot()
        installed = snap["current_version"]
        latest = snap["latest_version"] or ""

        commits = _fetch_changelog(channel, installed, max_commits)
        return self._ok({
            "channel": channel,
            "installed": installed,
            "latest": latest,
            "commits": commits,
        })
