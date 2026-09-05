"""Plugin lifecycle orchestration."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from functools import wraps


from pathlib import Path
from typing import Any, Optional

from .catalogue import DEFAULT_CATALOGUE_URL, MAX_WHEEL_BYTES, CatalogueClient, CatalogueError
from .github_releases import (
    GitHubReleaseClient,
    GitHubReleasesError,
    make_download_temp_dir,
    normalize_version,
)
from .manifest import ManifestError, load_manifest_from_wheel
from .runtime import PluginRuntime, PluginState
from .storage import PluginStorage

logger = logging.getLogger("PluginManager")


def _serialized_plugin(fn):
    """Keep a complete lifecycle transaction exclusive, without queuing callers."""

    @wraps(fn)
    def wrapped(self, plugin_id, *args, **kwargs):
        with self._operation(plugin_id):
            return fn(self, plugin_id, *args, **kwargs)

    return wrapped


class PluginManagerError(Exception):
    """Base error for plugin manager operations."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


class PluginManager:
    """High-level install / enable / start / stop / uninstall API."""

    def __init__(
        self,
        storage: PluginStorage,
        runtime: Optional[PluginRuntime] = None,
        *,
        catalogue_client: Optional[CatalogueClient] = None,
        github_client: Optional[GitHubReleaseClient] = None,
        catalogue_url: Optional[str] = None,
    ):
        self.storage = storage
        self.runtime = runtime or PluginRuntime(storage)
        self._lock = threading.RLock()
        self._operations: dict[str, tuple[int, int]] = {}
        self.catalogue = catalogue_client or CatalogueClient(catalogue_url or DEFAULT_CATALOGUE_URL)
        self.github = github_client or GitHubReleaseClient()

    @contextmanager
    def _operation(self, plugin_id: str):
        owner = threading.get_ident()
        with self._lock:
            active = self._operations.get(plugin_id)
            if active and active[0] != owner:
                raise PluginManagerError(f"plugin operation already in progress: {plugin_id}", 409)
            self._operations[plugin_id] = (owner, active[1] + 1 if active else 1)
        try:
            yield
        finally:
            with self._lock:
                depth = self._operations[plugin_id][1]
                if depth == 1:
                    del self._operations[plugin_id]
                else:
                    self._operations[plugin_id] = (owner, depth - 1)

    @contextmanager
    def stage_upload(self, wheel_path: str):
        """Own a bounded regular-file copy before IPC acknowledges processing.

        Read through one opened descriptor, so unlinking an HTTP upload cannot
        invalidate an in-progress copy. Never consume the caller's path later.
        """
        root = self.storage.root / ".ipc-staging"
        root.mkdir(parents=True, exist_ok=True)
        limit = MAX_WHEEL_BYTES
        deadline = time.monotonic() + 30
        with tempfile.TemporaryDirectory(prefix="install-", dir=root) as directory:
            source = Path(wheel_path)
            staged = Path(directory) / source.name
            fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as incoming:
                metadata = os.fstat(incoming.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise PluginManagerError("wheel must be a regular file", 400)
                if metadata.st_size > limit:
                    raise PluginManagerError("wheel exceeds 100 MiB limit", 413)
                total = 0
                with staged.open("wb") as outgoing:
                    while chunk := incoming.read(1024 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise PluginManagerError("wheel exceeds 100 MiB limit", 413)
                        if time.monotonic() > deadline:
                            raise PluginManagerError("wheel staging deadline exceeded", 504)
                        outgoing.write(chunk)
            yield staged

    def start(self) -> None:
        """Load state and start enabled service plugins."""
        self.storage.ensure_root()
        self.runtime.start_supervisor()
        for plugin_id in self.storage.list_plugin_ids():
            state = self.storage.read_state(plugin_id) or {}
            if not state.get("enabled", False):
                self.runtime._runtime_state[plugin_id] = PluginState.DISABLED
                continue
            manifest = self.storage.load_current_manifest(plugin_id)
            if manifest is None or manifest.runtime is None:
                self.runtime._runtime_state[plugin_id] = PluginState.STOPPED
                continue
            try:
                self.runtime.start(plugin_id)
            except Exception as exc:
                logger.error("Failed to start enabled plugin %s: %s", plugin_id, exc)
                self.runtime._runtime_state[plugin_id] = PluginState.FAILED

    def stop_all(self) -> None:
        for plugin_id in list(self.storage.list_plugin_ids()):
            try:
                self.runtime.stop(plugin_id)
            except Exception as exc:
                logger.debug("stop_all %s: %s", plugin_id, exc)
        self.runtime.stop_supervisor()

    def list_plugins(self) -> list[dict[str, Any]]:
        result = []
        for plugin_id in self.storage.list_plugin_ids():
            try:
                result.append(self.runtime.status_dict(plugin_id))
            except KeyError:
                continue
        return result

    def status(self, plugin_id: str) -> dict[str, Any]:
        try:
            return self.runtime.status_dict(plugin_id)
        except KeyError as exc:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404) from exc

    def install(self, wheel_path: str | Path) -> dict[str, Any]:
        wheel_path = Path(wheel_path)
        try:
            manifest = load_manifest_from_wheel(wheel_path)
        except ManifestError as exc:
            raise PluginManagerError(str(exc), 400) from exc
        with self._operation(manifest.id):
            try:
                info = self.runtime.install_wheel(wheel_path, manifest)
            except Exception as exc:
                raise PluginManagerError(f"install failed: {exc}", 500) from exc
        logger.info("Installed plugin %s@%s", info["id"], info["version"])
        # Return full status
        return self.status(info["id"])

    @_serialized_plugin
    def enable(self, plugin_id: str) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        state = dict(state)
        state["enabled"] = True
        self.storage.write_state(plugin_id, state)
        manifest = self.storage.load_current_manifest(plugin_id)
        if manifest and manifest.runtime is not None:
            try:
                self.runtime.start(plugin_id)
            except Exception as exc:
                raise PluginManagerError(f"enable start failed: {exc}", 500) from exc
        else:
            self.runtime._runtime_state[plugin_id] = PluginState.STOPPED
        return self.status(plugin_id)

    @_serialized_plugin
    def disable(self, plugin_id: str) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        try:
            self.runtime.stop(plugin_id, mark_disabled=True)
        except Exception as exc:
            logger.warning("disable stop error for %s: %s", plugin_id, exc)
        state = dict(state)
        state["enabled"] = False
        self.storage.write_state(plugin_id, state)
        self.runtime._runtime_state[plugin_id] = PluginState.DISABLED
        return self.status(plugin_id)

    @_serialized_plugin
    def start_plugin(self, plugin_id: str) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        if not state.get("enabled", False):
            raise PluginManagerError(
                f"plugin is disabled; enable it before starting: {plugin_id}", 409
            )
        try:
            self.runtime.start(plugin_id)
        except Exception as exc:
            raise PluginManagerError(f"start failed: {exc}", 500) from exc
        return self.status(plugin_id)

    @_serialized_plugin
    def stop_plugin(self, plugin_id: str) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        try:
            self.runtime.stop(plugin_id)
        except Exception as exc:
            raise PluginManagerError(f"stop failed: {exc}", 500) from exc
        return self.status(plugin_id)

    @_serialized_plugin
    def restart_plugin(self, plugin_id: str) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        if not state.get("enabled", False):
            raise PluginManagerError(
                f"plugin is disabled; enable it before restarting: {plugin_id}", 409
            )
        try:
            self.runtime.restart(plugin_id)
        except Exception as exc:
            raise PluginManagerError(f"restart failed: {exc}", 500) from exc
        return self.status(plugin_id)

    def logs(self, plugin_id: str, tail: int = 200) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        lines = self.storage.tail_log(plugin_id, lines=tail)
        return {"id": plugin_id, "lines": lines, "tail": tail}

    def get_config(self, plugin_id: str) -> dict[str, Any]:
        """Return plugin config for the editor.

        ``config`` is the value to show in the UI: saved data/config.json if
        present, otherwise plugin-declared defaults (manifest / config.default.json).
        ``saved`` is only what is on disk; ``defaults`` is the plugin template.
        """
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        paths = self.storage.paths_for(plugin_id)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            saved = self.storage.read_config(plugin_id)
        except ValueError as exc:
            raise PluginManagerError(str(exc), 500) from exc
        defaults = self.runtime.resolve_config_defaults(plugin_id)
        exists = self.storage.config_path(plugin_id).is_file()
        # Editor payload: prefer saved file; fall back to defaults when empty/missing
        if exists and saved:
            config = saved
        elif exists and not saved and not defaults:
            config = {}
        elif not exists:
            config = dict(defaults)
        else:
            # Empty saved file — still show defaults as a helpful starting point
            config = dict(defaults) if defaults else saved
        return {
            "id": plugin_id,
            "path": str(self.storage.config_path(plugin_id)),
            "exists": exists,
            "defaults": defaults,
            "saved": saved,
            "config": config,
        }

    @_serialized_plugin
    def set_config(
        self,
        plugin_id: str,
        config: dict[str, Any],
        *,
        restart: bool = False,
    ) -> dict[str, Any]:
        """Write plugin-owned data/config.json. Optionally restart a service plugin."""
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        if not isinstance(config, dict):
            raise PluginManagerError("config must be a JSON object", 400)
        # Soft size guard — prevents accidental huge payloads
        try:
            encoded = json.dumps(config)
        except (TypeError, ValueError) as exc:
            raise PluginManagerError(f"config is not JSON-serializable: {exc}", 400) from exc
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise PluginManagerError("config exceeds 256 KiB limit", 413)

        try:
            self.storage.write_config(plugin_id, config)
        except ValueError as exc:
            raise PluginManagerError(str(exc), 400) from exc

        restarted = False
        if restart and state.get("enabled", False):
            manifest = self.storage.load_current_manifest(plugin_id)
            if manifest and manifest.runtime is not None:
                try:
                    self.runtime.restart(plugin_id)
                    restarted = True
                except Exception as exc:
                    raise PluginManagerError(
                        f"config saved but restart failed: {exc}", 500
                    ) from exc

        result = self.get_config(plugin_id)
        result["restarted"] = restarted
        return result

    def get_runtime(self, plugin_id: str) -> dict[str, Any]:
        """Return plugin runtime snapshot from data/runtime.json for UIs."""
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        paths = self.storage.paths_for(plugin_id)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            runtime = self.storage.read_runtime(plugin_id)
        except ValueError as exc:
            raise PluginManagerError(str(exc), 500) from exc
        path = self.storage.runtime_path(plugin_id)
        return {
            "id": plugin_id,
            "path": str(path),
            "exists": path.is_file(),
            "runtime": runtime,
        }

    @_serialized_plugin
    def uninstall(self, plugin_id: str, *, delete_data: bool = False) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        try:
            self.runtime.stop(plugin_id, mark_disabled=True)
        except Exception as exc:
            logger.warning("uninstall stop error for %s: %s", plugin_id, exc)
        self.storage.remove_release_code(plugin_id, keep_data=not delete_data)
        self.runtime._runtime_state.pop(plugin_id, None)
        self.runtime._crash_times.pop(plugin_id, None)
        self.runtime._exit_codes.pop(plugin_id, None)
        return {
            "id": plugin_id,
            "uninstalled": True,
            "data_deleted": bool(delete_data),
        }

    def _version_gt(self, left: str, right: str) -> bool:
        """Return True if left > right using packaging.version when available."""
        try:
            from packaging.version import Version

            return Version(normalize_version(left)) > Version(normalize_version(right))
        except Exception:
            return normalize_version(left) > normalize_version(right)

    def _download_dir(self) -> Path:
        return self.storage.root / ".download"

    def list_catalogue(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Fetch catalogue entries and annotate with install/update info."""
        try:
            catalogue = self.catalogue.fetch(force_refresh=force_refresh)
        except CatalogueError as exc:
            raise PluginManagerError(str(exc), int(exc.code)) from exc

        plugins_out: list[dict[str, Any]] = []
        for entry in catalogue.plugins:
            installed_state = self.storage.read_state(entry.id)
            installed = installed_state is not None
            installed_version = (
                str(installed_state.get("version") or "") if installed_state else None
            )
            row: dict[str, Any] = {
                **entry.to_dict(),
                "installed": installed,
                "installedVersion": installed_version if installed else None,
            }
            # Schema 2 carries the openHop-approved version directly. Schema 1
            # remains readable for compatibility with custom legacy catalogues.
            latest_version = entry.version
            update_available = False
            if entry.has_approved_wheel:
                if installed and installed_version and latest_version:
                    update_available = self._version_gt(latest_version, installed_version)
            else:
                try:
                    latest = self.github.latest_stable(
                        entry.repository, force_refresh=force_refresh
                    )
                    if latest is not None:
                        latest_version = latest.version
                        if installed and installed_version:
                            update_available = self._version_gt(latest_version, installed_version)
                except GitHubReleasesError as exc:
                    row["releasesError"] = str(exc)
            row["latestVersion"] = latest_version
            row["updateAvailable"] = bool(update_available)
            plugins_out.append(row)
        return {"schema": catalogue.schema, "plugins": plugins_out}

    @_serialized_plugin
    def install_from_catalogue(
        self,
        plugin_id: str,
        *,
        version: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Download an approved catalogue wheel and install it."""
        try:
            entry = self.catalogue.get_plugin(plugin_id, force_refresh=force_refresh)
        except CatalogueError as exc:
            raise PluginManagerError(str(exc), int(exc.code)) from exc

        download_root = self._download_dir()
        staging = make_download_temp_dir(download_root)
        try:
            if entry.has_approved_wheel:
                if version and normalize_version(version) != normalize_version(entry.version or ""):
                    raise PluginManagerError(
                        f"version {version!r} is not approved for {entry.id}; "
                        f"approved version is {entry.version}",
                        400,
                    )
                try:
                    wheel_path = self.catalogue.download_wheel(entry, staging)
                except CatalogueError as exc:
                    raise PluginManagerError(str(exc), int(exc.code)) from exc
            else:
                try:
                    _release, wheel_path = self.github.download_latest_wheel(
                        entry.repository,
                        staging,
                        version=version,
                        force_refresh=force_refresh,
                    )
                except GitHubReleasesError as exc:
                    raise PluginManagerError(str(exc), int(exc.code)) from exc

            try:
                manifest = load_manifest_from_wheel(wheel_path)
            except ManifestError as exc:
                raise PluginManagerError(str(exc), 400) from exc

            if manifest.id != entry.id:
                raise PluginManagerError(
                    f"manifest id {manifest.id!r} does not match catalogue id {entry.id!r}",
                    400,
                )
            if entry.has_approved_wheel and manifest.version != entry.version:
                raise PluginManagerError(
                    f"manifest version {manifest.version!r} does not match approved "
                    f"catalogue version {entry.version!r}",
                    400,
                )

            try:
                info = self.runtime.install_wheel(wheel_path, manifest)
            except Exception as exc:
                raise PluginManagerError(f"install failed: {exc}", 500) from exc
            # Record catalogue provenance (repository locked at install time)
            st = self.storage.read_state(info["id"]) or {}
            st = dict(st)
            st["version"] = info["version"]
            st["source"] = "catalogue"
            st["repository"] = entry.repository
            # Keep enabled flag from install_wheel; enable next
            self.storage.write_state(info["id"], st)

            # Catalogue installs are enabled by default
            return self.enable(info["id"])
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def check_update(self, plugin_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        installed_version = str(state.get("version") or "")
        repository = state.get("repository")

        if state.get("source") == "catalogue":
            try:
                entry = self.catalogue.get_plugin(plugin_id, force_refresh=force_refresh)
            except CatalogueError as exc:
                raise PluginManagerError(str(exc), int(exc.code)) from exc
            if entry.has_approved_wheel:
                latest_version = entry.version
                update_available = bool(
                    latest_version
                    and installed_version
                    and self._version_gt(latest_version, installed_version)
                )
                return {
                    "id": plugin_id,
                    "installedVersion": installed_version or None,
                    "latestVersion": latest_version,
                    "updateAvailable": update_available,
                    "repository": entry.repository,
                    "releaseNotes": None,
                    "releaseUrl": None,
                    "releaseTag": None,
                }

        if not repository:
            return {
                "id": plugin_id,
                "installedVersion": installed_version or None,
                "latestVersion": None,
                "updateAvailable": False,
                "repository": None,
                "updatesAvailable": False,
                "reason": "repository unknown (local install); update check unavailable",
            }

        try:
            latest = self.github.latest_stable(str(repository), force_refresh=force_refresh)
        except GitHubReleasesError as exc:
            raise PluginManagerError(str(exc), int(exc.code)) from exc

        latest_version = latest.version if latest else None
        update_available = bool(
            latest_version
            and installed_version
            and self._version_gt(latest_version, installed_version)
        )
        notes = latest.body if latest and update_available else None
        return {
            "id": plugin_id,
            "installedVersion": installed_version or None,
            "latestVersion": latest_version,
            "updateAvailable": update_available,
            "repository": str(repository),
            "releaseNotes": notes,
            "releaseUrl": latest.html_url if latest else None,
            "releaseTag": latest.tag if latest else None,
        }

    @_serialized_plugin
    def update_plugin(
        self,
        plugin_id: str,
        *,
        version: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Install the newer approved catalogue version when available."""
        state = self.storage.read_state(plugin_id)
        if state is None:
            raise PluginManagerError(f"plugin not found: {plugin_id}", 404)
        repository = state.get("repository")
        if not repository:
            raise PluginManagerError(
                "update unavailable: plugin has no repository metadata "
                "(reinstall from catalogue or set repository)",
                400,
            )
        repository = str(repository)
        installed_version = str(state.get("version") or "")
        was_enabled = bool(state.get("enabled", False))

        approved_entry = None
        release = None
        if state.get("source") == "catalogue":
            try:
                candidate = self.catalogue.get_plugin(plugin_id, force_refresh=force_refresh)
            except CatalogueError as exc:
                raise PluginManagerError(str(exc), int(exc.code)) from exc
            if candidate.has_approved_wheel:
                approved_entry = candidate
                if version and normalize_version(version) != normalize_version(
                    approved_entry.version or ""
                ):
                    raise PluginManagerError(
                        f"version {version!r} is not approved for {plugin_id}; "
                        f"approved version is {approved_entry.version}",
                        400,
                    )
                if installed_version and not self._version_gt(
                    approved_entry.version or "", installed_version
                ):
                    status = self.status(plugin_id)
                    status["updateAvailable"] = False
                    status["latestVersion"] = approved_entry.version
                    status["updated"] = False
                    return status

        if approved_entry is None:
            # Legacy schema-1 catalogues continue to resolve GitHub Releases.
            try:
                if version:
                    release = self.github.find_release(
                        repository, version, force_refresh=force_refresh
                    )
                else:
                    release = self.github.latest_stable(repository, force_refresh=force_refresh)
                    if release is None:
                        raise PluginManagerError(f"no stable releases found for {repository}", 404)
                    if installed_version and not self._version_gt(
                        release.version, installed_version
                    ):
                        status = self.status(plugin_id)
                        status["updateAvailable"] = False
                        status["latestVersion"] = release.version
                        status["updated"] = False
                        return status
            except GitHubReleasesError as exc:
                raise PluginManagerError(str(exc), int(exc.code)) from exc

        download_root = self._download_dir()
        staging = make_download_temp_dir(download_root)
        try:
            if approved_entry is not None:
                try:
                    wheel_path = self.catalogue.download_wheel(approved_entry, staging)
                except CatalogueError as exc:
                    raise PluginManagerError(str(exc), int(exc.code)) from exc
                target_version = approved_entry.version
            else:
                if release is None:
                    raise PluginManagerError("could not resolve plugin release", 500)
                try:
                    wheel_path = self.github.download_wheel(release, staging)
                except GitHubReleasesError as exc:
                    raise PluginManagerError(str(exc), int(exc.code)) from exc
                target_version = release.version

            try:
                manifest = load_manifest_from_wheel(wheel_path)
            except ManifestError as exc:
                raise PluginManagerError(str(exc), 400) from exc
            if manifest.id != plugin_id:
                raise PluginManagerError(
                    f"manifest id {manifest.id!r} does not match installed id {plugin_id!r}",
                    400,
                )
            if approved_entry is not None and manifest.version != approved_entry.version:
                raise PluginManagerError(
                    f"manifest version {manifest.version!r} does not match approved "
                    f"catalogue version {approved_entry.version!r}",
                    400,
                )

            # Stop running process before swapping release
            try:
                self.runtime.stop(plugin_id)
            except Exception as exc:
                logger.debug("update stop %s: %s", plugin_id, exc)
            try:
                info = self.runtime.install_wheel(wheel_path, manifest)
            except Exception as exc:
                raise PluginManagerError(f"update install failed: {exc}", 500) from exc
            st = self.storage.read_state(info["id"]) or {}
            st = dict(st)
            st["version"] = info["version"]
            st["source"] = st.get("source") or "catalogue"
            st["repository"] = repository  # never silently retarget
            st["enabled"] = was_enabled
            self.storage.write_state(info["id"], st)

            if was_enabled:
                return self.enable(plugin_id)
            result = self.status(plugin_id)
            result["updated"] = True
            result["latestVersion"] = target_version
            return result
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def ui_root_for(self, plugin_id: str) -> Optional[Path]:
        """Return the release directory for an enabled UI application plugin."""
        state = self.storage.read_state(plugin_id)
        if state is None or not state.get("enabled", False):
            return None
        manifest = self.storage.load_current_manifest(plugin_id)
        if manifest is None or manifest.ui is None:
            return None
        paths = self.storage.paths_for(plugin_id)
        if paths.current_link.exists() or paths.current_link.is_symlink():
            return paths.current_link.resolve()
        version = state.get("version")
        if version:
            return paths.release_dir(str(version))
        return None
