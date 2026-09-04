"""Virtualenv installation and process supervision for plugins."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess  # nosec B404 - argument arrays only, never shell=True
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .manifest import PluginManifest
from .storage import PluginPaths, PluginStorage

logger = logging.getLogger("PluginRuntime")

STOP_TIMEOUT_SECONDS = 5.0
CRASH_WINDOW_SECONDS = 60.0
CRASH_MAX_EXITS = 5
LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate simply by truncating when oversized


class PluginState(str, Enum):
    DISABLED = "DISABLED"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass
class ProcessHandle:
    plugin_id: str
    process: subprocess.Popen
    log_fp: object
    started_at: float = field(default_factory=time.monotonic)


class PluginRuntime:
    """Installs wheels into isolated venvs and supervises plugin processes."""

    def __init__(
        self,
        storage: PluginStorage,
        *,
        python_executable: Optional[str] = None,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        crash_window: float = CRASH_WINDOW_SECONDS,
        crash_max_exits: int = CRASH_MAX_EXITS,
        popen_factory: Optional[Callable[..., subprocess.Popen]] = None,
        run_factory: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ):
        self.storage = storage
        self.python_executable = python_executable or sys.executable
        self.stop_timeout = stop_timeout
        self.crash_window = crash_window
        self.crash_max_exits = crash_max_exits
        self._popen = popen_factory or subprocess.Popen
        self._run = run_factory or subprocess.run
        self._lock = threading.RLock()
        self._handles: dict[str, ProcessHandle] = {}
        self._runtime_state: dict[str, PluginState] = {}
        self._crash_times: dict[str, deque[float]] = {}
        self._exit_codes: dict[str, Optional[int]] = {}
        self._supervise_stop = threading.Event()
        self._supervise_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # Installation
    # ------------------------------------------------------------------ #

    def install_wheel(self, wheel_path: Path | str, manifest: PluginManifest) -> dict:
        """Create release dir + venv and pip-install the wheel. Does not enable/start."""
        wheel_path = Path(wheel_path).resolve()
        if not wheel_path.is_file():
            raise FileNotFoundError(f"wheel not found: {wheel_path}")
        if not wheel_path.name.endswith(".whl"):
            raise ValueError("install source must be a .whl file")

        plugin_id = manifest.id
        version = manifest.version
        paths = self.storage.ensure_plugin_layout(plugin_id, version)
        self.storage.write_manifest(plugin_id, version, manifest)

        # Keep the exact install artifact with the release. Container data
        # volumes outlive the image, so this lets us rebuild the isolated venv
        # after a future base-image Python minor-version change.
        release_dir = paths.release_dir(version)
        archived_wheel = release_dir / wheel_path.name
        if archived_wheel.resolve() != wheel_path:
            for previous_wheel in release_dir.glob("*.whl"):
                previous_wheel.unlink(missing_ok=True)
            shutil.copy2(wheel_path, archived_wheel)
        else:
            archived_wheel = wheel_path

        # Extract optional UI assets from the wheel into the release dir
        self._extract_ui_assets(archived_wheel, release_dir, manifest)
        # Also extract optional config.default.json next to the manifest if present
        self._extract_config_default_file(archived_wheel, release_dir)

        venv_path = paths.venv_dir(version)
        if manifest.runtime is not None:
            self._create_venv(venv_path)
            self._pip_install(venv_path, archived_wheel)

        # Seed data/config.json from defaults when the user has no saved config yet
        self._seed_config_if_missing(plugin_id, manifest, paths.release_dir(version))

        # Preserve enabled flag across reinstall
        prev = self.storage.read_state(plugin_id) or {}
        enabled = bool(prev.get("enabled", False))
        self.storage.set_current(plugin_id, version)
        self.storage.write_state(plugin_id, {"version": version, "enabled": enabled})

        with self._lock:
            if plugin_id not in self._runtime_state:
                self._runtime_state[plugin_id] = (
                    PluginState.DISABLED if not enabled else PluginState.STOPPED
                )

        return {
            "id": plugin_id,
            "version": version,
            "enabled": enabled,
            "release_dir": str(paths.release_dir(version)),
            "venv_dir": str(venv_path) if manifest.runtime else None,
            "data_dir": str(paths.data_dir),
        }

    def ensure_venv_compatible(self, plugin_id: str) -> None:
        """Rebuild a persisted plugin venv when Python's minor version changes."""
        state = self.storage.read_state(plugin_id)
        manifest = self.storage.load_current_manifest(plugin_id)
        if state is None or manifest is None or manifest.runtime is None:
            return

        version = str(state.get("version") or manifest.version)
        paths = self.storage.paths_for(plugin_id)
        venv_path = paths.venv_dir(version)
        config_path = venv_path / "pyvenv.cfg"
        if not config_path.is_file():
            # Legacy/test venvs have no reliable version marker. Let normal
            # entrypoint validation report a useful error instead of deleting.
            return

        persisted_version = None
        try:
            for line in config_path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip().lower() == "version":
                    parts = value.strip().split(".")
                    if len(parts) >= 2:
                        persisted_version = (int(parts[0]), int(parts[1]))
                    break
        except (OSError, ValueError):
            return

        current_version = (sys.version_info.major, sys.version_info.minor)
        if persisted_version is None or persisted_version == current_version:
            return

        wheels = sorted(paths.release_dir(version).glob("*.whl"))
        if not wheels:
            raise RuntimeError(
                f"plugin venv uses Python {persisted_version[0]}.{persisted_version[1]}, "
                f"but the container uses {current_version[0]}.{current_version[1]}; "
                "reinstall the plugin to rebuild its environment"
            )

        logger.warning(
            "Rebuilding %s venv for Python %s.%s (was %s.%s)",
            plugin_id,
            current_version[0],
            current_version[1],
            persisted_version[0],
            persisted_version[1],
        )
        shutil.rmtree(venv_path)
        self._create_venv(venv_path)
        self._pip_install(venv_path, wheels[0])

    def _create_venv(self, venv_path: Path) -> None:
        if venv_path.exists():
            # Reinstall into existing venv is ok; recreate if broken
            py = self._venv_python(venv_path)
            if py.is_file():
                return
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            [self.python_executable, "-m", "venv", str(venv_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"venv creation failed ({result.returncode}): {result.stderr or result.stdout}"
            )

    def _pip_install(self, venv_path: Path, wheel_path: Path) -> None:
        py = self._venv_python(venv_path)
        if not py.is_file():
            raise RuntimeError(f"venv python missing: {py}")
        install_path = self._ensure_pip_wheel_filename(wheel_path)
        try:
            result = self._run(
                [str(py), "-m", "pip", "install", "--upgrade", str(install_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            if install_path != wheel_path and install_path.exists():
                try:
                    install_path.unlink()
                except OSError:
                    pass
        if result.returncode != 0:
            raise RuntimeError(
                f"pip install failed ({result.returncode}): {result.stderr or result.stdout}"
            )

    @staticmethod
    def _looks_like_wheel_filename(name: str) -> bool:
        # PEP 427: {distribution}-{version}(-{build})?-{py}-{abi}-{plat}.whl
        if not name.endswith(".whl"):
            return False
        stem = name[: -len(".whl")]
        return stem.count("-") >= 4

    @classmethod
    def _ensure_pip_wheel_filename(cls, wheel_path: Path) -> Path:
        """Return a path whose basename pip will accept as a wheel filename."""
        if cls._looks_like_wheel_filename(wheel_path.name):
            return wheel_path

        import re
        import shutil
        import zipfile

        name = None
        version = None
        try:
            with zipfile.ZipFile(wheel_path) as zf:
                for member in zf.namelist():
                    if member.endswith(".dist-info/METADATA") and member.count("/") == 1:
                        text = zf.read(member).decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            if line.startswith("Name:"):
                                name = line.split(":", 1)[1].strip()
                            elif line.startswith("Version:"):
                                version = line.split(":", 1)[1].strip()
                        break
        except zipfile.BadZipFile:
            name = None

        def _normalize(value: str) -> str:
            value = value.strip().replace("-", "_")
            value = re.sub(r"[^A-Za-z0-9._]", "_", value)
            return value or "package"

        dist = _normalize(name or "package")
        ver = _normalize(version or "0.0.0")
        # py3-none-any is valid for pure-Python wheels and accepted by pip.
        fixed_name = f"{dist}-{ver}-py3-none-any.whl"
        fixed_path = wheel_path.with_name(fixed_name)
        if fixed_path.exists() and fixed_path != wheel_path:
            fixed_path.unlink()
        shutil.copy2(wheel_path, fixed_path)
        logger.info(
            "Rewrote wheel filename for pip: %s -> %s",
            wheel_path.name,
            fixed_path.name,
        )
        return fixed_path

    @staticmethod
    def _venv_python(venv_path: Path) -> Path:
        if os.name == "nt":
            return venv_path / "Scripts" / "python.exe"
        return venv_path / "bin" / "python"

    @staticmethod
    def _venv_bin_dir(venv_path: Path) -> Path:
        if os.name == "nt":
            return venv_path / "Scripts"
        return venv_path / "bin"

    def resolve_entrypoint(self, paths: PluginPaths, version: str, entrypoint: str) -> Path:
        bin_dir = self._venv_bin_dir(paths.venv_dir(version))
        candidate = (bin_dir / entrypoint).resolve()
        bin_root = bin_dir.resolve()
        try:
            candidate.relative_to(bin_root)
        except ValueError as exc:
            raise RuntimeError("entrypoint path escapes plugin venv") from exc
        if not candidate.is_file():
            # Windows may use .exe
            exe = Path(str(candidate) + ".exe")
            if exe.is_file():
                return exe
            raise FileNotFoundError(f"entrypoint not found in venv: {entrypoint}")
        return candidate

    def _extract_ui_assets(
        self, wheel_path: Path, release_dir: Path, manifest: PluginManifest
    ) -> None:
        if manifest.ui is None:
            return
        import zipfile

        entry = manifest.ui.entry.replace("\\", "/")
        # Copy any archive members under the entry's top-level directory (e.g. ui/)
        top = entry.split("/", 1)[0]
        prefixes = (f"{top}/",)
        try:
            with zipfile.ZipFile(wheel_path) as zf:
                for name in zf.namelist():
                    # Also copy bare openhop-plugin.json already handled
                    rel = None
                    for prefix in prefixes:
                        # Match ".../ui/..." or "ui/..."
                        idx = name.find(prefix)
                        if idx >= 0:
                            rel = name[idx:]
                            break
                    if rel is None:
                        continue
                    if rel.endswith("/"):
                        continue
                    # Reject traversal in archive member names
                    if ".." in Path(rel).parts:
                        continue
                    dest = release_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"invalid wheel while extracting UI: {exc}") from exc

        # Ensure entry file exists if packaged at package root differently
        entry_path = release_dir / entry
        if not entry_path.is_file():
            logger.warning(
                "UI entry %s not found in release dir after wheel extract for %s",
                entry,
                manifest.id,
            )

    def _extract_config_default_file(self, wheel_path: Path, release_dir: Path) -> None:
        """Copy optional config.default.json from the wheel into the release dir."""
        import zipfile

        try:
            with zipfile.ZipFile(wheel_path) as zf:
                candidates = [
                    name
                    for name in zf.namelist()
                    if name.endswith("/config.default.json") or name == "config.default.json"
                ]
                if not candidates:
                    return
                preferred = [c for c in candidates if "share/openhop/plugins/" in c]
                chosen = preferred[0] if preferred else candidates[0]
                if ".." in Path(chosen).parts:
                    return
                dest = release_dir / "config.default.json"
                with zf.open(chosen) as src, open(dest, "wb") as out:
                    out.write(src.read())
        except zipfile.BadZipFile:
            return

    def resolve_config_defaults(
        self, plugin_id: str, manifest: Optional[PluginManifest] = None
    ) -> dict:
        """Load defaults from manifest and/or release config.default.json."""
        if manifest is None:
            manifest = self.storage.load_current_manifest(plugin_id)
        defaults: dict = {}
        if manifest and manifest.config_defaults:
            defaults.update(manifest.config_defaults)

        paths = self.storage.paths_for(plugin_id)
        candidates: list[Path] = []
        if paths.current_link.exists() or paths.current_link.is_symlink():
            candidates.append(paths.current_link / "config.default.json")
        version = self.storage.current_version(plugin_id)
        if version:
            try:
                candidates.append(paths.release_dir(version) / "config.default.json")
            except ValueError:
                pass
        candidates.append(paths.manifest_path().parent / "config.default.json")

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if not candidate.is_file():
                continue
            try:
                import json

                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    defaults.update(data)
                    break
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Ignoring invalid %s: %s", candidate, exc)
        return defaults

    def _seed_config_if_missing(
        self, plugin_id: str, manifest: PluginManifest, release_dir: Path
    ) -> None:
        """Write data/config.json from defaults when the user has no saved file yet."""
        config_path = self.storage.config_path(plugin_id)
        if config_path.is_file():
            return
        defaults = {}
        if manifest.config_defaults:
            defaults.update(manifest.config_defaults)
        default_file = release_dir / "config.default.json"
        if default_file.is_file():
            try:
                import json

                data = json.loads(default_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    defaults.update(data)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Could not read config.default.json for %s: %s", plugin_id, exc)
        if not defaults:
            return
        try:
            self.storage.write_config(plugin_id, defaults)
            logger.info("Seeded default config.json for plugin %s", plugin_id)
        except ValueError as exc:
            logger.warning("Could not seed config for %s: %s", plugin_id, exc)

    # ------------------------------------------------------------------ #
    # Process lifecycle
    # ------------------------------------------------------------------ #

    def get_state(self, plugin_id: str) -> PluginState:
        with self._lock:
            persisted = self.storage.read_state(plugin_id)
            if persisted is not None and not persisted.get("enabled", False):
                # Keep FAILED visible even when disabled? Spec: disable → DISABLED
                current = self._runtime_state.get(plugin_id)
                if current in (PluginState.STOPPING, PluginState.STARTING):
                    return current
                return PluginState.DISABLED
            if plugin_id in self._handles:
                proc = self._handles[plugin_id].process
                if proc.poll() is None:
                    return PluginState.RUNNING
            return self._runtime_state.get(plugin_id, PluginState.STOPPED)

    def start(self, plugin_id: str) -> PluginState:
        with self._lock:
            state = self.storage.read_state(plugin_id)
            if state is None:
                raise KeyError(f"plugin not installed: {plugin_id}")
            if not state.get("enabled", False):
                # Allow explicit start only when enabled — callers enable first
                raise RuntimeError(f"plugin is disabled: {plugin_id}")

            # Persisted Docker volumes can outlive the image's Python minor
            # version. Rebuild before resolving the installed entrypoint.
            self.ensure_venv_compatible(plugin_id)

            existing = self._handles.get(plugin_id)
            if existing and existing.process.poll() is None:
                self._runtime_state[plugin_id] = PluginState.RUNNING
                return PluginState.RUNNING

            manifest = self.storage.load_current_manifest(plugin_id)
            if manifest is None:
                raise RuntimeError(f"manifest missing for {plugin_id}")
            if manifest.runtime is None:
                # UI-only: nothing to start
                self._runtime_state[plugin_id] = PluginState.STOPPED
                return PluginState.STOPPED

            version = str(state.get("version") or manifest.version)
            paths = self.storage.paths_for(plugin_id)
            exe = self.resolve_entrypoint(paths, version, manifest.runtime.entrypoint)

            self._runtime_state[plugin_id] = PluginState.STARTING
            paths.logs_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_log_if_needed(paths.log_file)
            log_fp = open(paths.log_file, "a", encoding="utf-8", buffering=1)

            env = os.environ.copy()
            # Manager-only credentials must never become plugin credentials.
            env.pop("OPENHOP_PLUGIN_GITHUB_TOKEN", None)
            env["OPENHOP_PLUGIN_ID"] = plugin_id
            env["OPENHOP_PLUGIN_DATA"] = str(paths.data_dir)

            try:
                proc = self._popen(
                    [str(exe)],
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=str(paths.data_dir),
                    shell=False,
                )
            except Exception:
                log_fp.close()
                self._runtime_state[plugin_id] = PluginState.FAILED
                raise

            self._handles[plugin_id] = ProcessHandle(
                plugin_id=plugin_id, process=proc, log_fp=log_fp
            )
            self._runtime_state[plugin_id] = PluginState.RUNNING
            self._exit_codes[plugin_id] = None
            logger.info("Started plugin %s pid=%s", plugin_id, proc.pid)
            return PluginState.RUNNING

    def stop(self, plugin_id: str, *, mark_disabled: bool = False) -> PluginState:
        with self._lock:
            handle = self._handles.get(plugin_id)
            self._runtime_state[plugin_id] = (
                PluginState.DISABLED if mark_disabled else PluginState.STOPPING
            )
            if handle is None:
                self._runtime_state[plugin_id] = (
                    PluginState.DISABLED if mark_disabled else PluginState.STOPPED
                )
                return self._runtime_state[plugin_id]

            proc = handle.process
            if proc.poll() is not None:
                self._cleanup_handle(plugin_id)
                self._runtime_state[plugin_id] = (
                    PluginState.DISABLED if mark_disabled else PluginState.STOPPED
                )
                return self._runtime_state[plugin_id]

            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.debug("SIGTERM failed for %s: %s", plugin_id, exc)

        # Wait outside lock
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

        with self._lock:
            self._cleanup_handle(plugin_id)
            self._runtime_state[plugin_id] = (
                PluginState.DISABLED if mark_disabled else PluginState.STOPPED
            )
            logger.info("Stopped plugin %s", plugin_id)
            return self._runtime_state[plugin_id]

    def restart(self, plugin_id: str) -> PluginState:
        self.stop(plugin_id)
        return self.start(plugin_id)

    def _cleanup_handle(self, plugin_id: str) -> None:
        handle = self._handles.pop(plugin_id, None)
        if handle is None:
            return
        try:
            if handle.process.poll() is not None:
                self._exit_codes[plugin_id] = handle.process.returncode
        except Exception:
            pass
        try:
            handle.log_fp.close()
        except Exception:
            pass

    @staticmethod
    def _rotate_log_if_needed(log_file: Path) -> None:
        try:
            if log_file.is_file() and log_file.stat().st_size > LOG_MAX_BYTES:
                rotated = log_file.with_suffix(".log.1")
                if rotated.exists():
                    rotated.unlink()
                log_file.rename(rotated)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Supervision
    # ------------------------------------------------------------------ #

    def start_supervisor(self) -> None:
        if self._supervise_thread and self._supervise_thread.is_alive():
            return
        self._supervise_stop.clear()
        self._supervise_thread = threading.Thread(
            target=self._supervise_loop, name="plugin-supervisor", daemon=True
        )
        self._supervise_thread.start()

    def stop_supervisor(self) -> None:
        self._supervise_stop.set()
        thread = self._supervise_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._supervise_thread = None

    def _supervise_loop(self) -> None:
        while not self._supervise_stop.wait(0.5):
            try:
                self._check_crashes()
            except Exception as exc:
                logger.debug("supervisor tick error: %s", exc)

    def _check_crashes(self) -> None:
        with self._lock:
            finished: list[tuple[str, int]] = []
            for plugin_id, handle in list(self._handles.items()):
                code = handle.process.poll()
                if code is None:
                    continue
                finished.append((plugin_id, code))

            for plugin_id, code in finished:
                self._cleanup_handle(plugin_id)
                state = self.storage.read_state(plugin_id) or {}
                if not state.get("enabled", False):
                    self._runtime_state[plugin_id] = PluginState.DISABLED
                    continue
                # Expected stop paths set STOPPING/DISABLED already
                current = self._runtime_state.get(plugin_id)
                if current in (PluginState.STOPPING, PluginState.DISABLED, PluginState.STOPPED):
                    if current != PluginState.DISABLED:
                        self._runtime_state[plugin_id] = PluginState.STOPPED
                    continue

                logger.warning("Plugin %s exited unexpectedly code=%s", plugin_id, code)
                self._exit_codes[plugin_id] = code
                times = self._crash_times.setdefault(plugin_id, deque())
                now = time.monotonic()
                times.append(now)
                while times and (now - times[0]) > self.crash_window:
                    times.popleft()
                if len(times) >= self.crash_max_exits:
                    logger.error(
                        "Plugin %s crash-looped (%d exits in %ss) → FAILED",
                        plugin_id,
                        len(times),
                        self.crash_window,
                    )
                    self._runtime_state[plugin_id] = PluginState.FAILED
                    continue

                # Restart
                try:
                    self._runtime_state[plugin_id] = PluginState.STOPPED
                    self.start(plugin_id)
                except Exception as exc:
                    logger.error("Failed to restart plugin %s: %s", plugin_id, exc)
                    self._runtime_state[plugin_id] = PluginState.FAILED

    def status_dict(self, plugin_id: str) -> dict:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise KeyError(plugin_id)
        manifest = self.storage.load_current_manifest(plugin_id)
        paths = self.storage.paths_for(plugin_id)
        runtime_state = self.get_state(plugin_id)
        pid = None
        with self._lock:
            handle = self._handles.get(plugin_id)
            if handle and handle.process.poll() is None:
                pid = handle.process.pid
            last_exit = self._exit_codes.get(plugin_id)
        return {
            "id": plugin_id,
            "name": manifest.name if manifest else plugin_id,
            "version": state.get("version"),
            "enabled": bool(state.get("enabled", False)),
            "state": runtime_state.value,
            "pid": pid,
            "last_exit_code": last_exit,
            "has_runtime": bool(manifest and manifest.runtime),
            "has_ui": bool(manifest and manifest.ui),
            "ui_entry": manifest.ui.entry if manifest and manifest.ui else None,
            "data_dir": str(paths.data_dir),
            "description": manifest.description if manifest else "",
            "source": state.get("source") or "local",
            "repository": state.get("repository") or None,
        }
