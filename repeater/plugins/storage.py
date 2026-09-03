"""Plugin filesystem layout and manager-owned state."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .manifest import MANIFEST_FILENAME, PluginManifest, load_manifest_file

logger = logging.getLogger("PluginStorage")

STATE_FILENAME = "state.json"
DEFAULT_PLUGINS_SUBDIR = "plugins"
DEFAULT_SOCKET_NAME = "plugin-manager.sock"


@dataclass(frozen=True)
class PluginPaths:
    root: Path
    plugin_id: str

    @property
    def plugin_dir(self) -> Path:
        return self.root / self.plugin_id

    @property
    def releases_dir(self) -> Path:
        return self.plugin_dir / "releases"

    @property
    def current_link(self) -> Path:
        return self.plugin_dir / "current"

    @property
    def data_dir(self) -> Path:
        return self.plugin_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.plugin_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "plugin.log"

    @property
    def state_file(self) -> Path:
        return self.plugin_dir / STATE_FILENAME

    def release_dir(self, version: str) -> Path:
        _assert_safe_segment(version, "version")
        return self.releases_dir / version

    def venv_dir(self, version: str) -> Path:
        return self.release_dir(version) / "venv"

    def manifest_path(self, version: Optional[str] = None) -> Path:
        if version is None:
            return self.current_link / MANIFEST_FILENAME
        return self.release_dir(version) / MANIFEST_FILENAME


def _assert_safe_segment(value: str, field: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe {field}: {value!r}")


def safe_join(root: Path, *parts: str) -> Path:
    """Join path parts under root, rejecting traversal."""
    root = root.resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {candidate}") from exc
    return candidate


class PluginStorage:
    """Manages plugin directories and manager-owned state.json files."""

    def __init__(self, plugins_root: Path | str):
        self.root = Path(plugins_root).expanduser().resolve()

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def paths_for(self, plugin_id: str) -> PluginPaths:
        _assert_safe_segment(plugin_id, "plugin_id")
        if ".." in plugin_id:
            raise ValueError(f"unsafe plugin_id: {plugin_id!r}")
        return PluginPaths(root=self.root, plugin_id=plugin_id)

    def list_plugin_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        ids = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and (child / STATE_FILENAME).is_file():
                ids.append(child.name)
        return ids

    def read_state(self, plugin_id: str) -> Optional[dict[str, Any]]:
        path = self.paths_for(plugin_id).state_file
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read state for %s: %s", plugin_id, exc)
            return None
        if not isinstance(data, dict):
            return None
        return data

    def write_state(self, plugin_id: str, state: dict[str, Any]) -> None:
        paths = self.paths_for(plugin_id)
        paths.plugin_dir.mkdir(parents=True, exist_ok=True)
        # Preserve existing optional fields unless explicitly overwritten.
        prev = self.read_state(plugin_id) or {}
        payload = {
            "id": plugin_id,
            "version": str(state.get("version", prev.get("version", ""))),
            "enabled": bool(state.get("enabled", prev.get("enabled", False))),
        }
        # Optional install provenance (catalogue vs local wheel)
        source = state.get("source", prev.get("source"))
        if source is not None and str(source).strip():
            payload["source"] = str(source).strip()
        repository = state.get("repository", prev.get("repository"))
        if repository is not None and str(repository).strip():
            payload["repository"] = str(repository).strip()
        self._atomic_write_json(paths.state_file, payload)

    def ensure_plugin_layout(self, plugin_id: str, version: str) -> PluginPaths:
        paths = self.paths_for(plugin_id)
        paths.plugin_dir.mkdir(parents=True, exist_ok=True)
        paths.releases_dir.mkdir(parents=True, exist_ok=True)
        paths.release_dir(version).mkdir(parents=True, exist_ok=True)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        return paths

    def set_current(self, plugin_id: str, version: str) -> None:
        paths = self.paths_for(plugin_id)
        target = paths.release_dir(version)
        if not target.is_dir():
            raise FileNotFoundError(f"release directory missing: {target}")
        link = paths.current_link
        # Relative symlink keeps layout portable
        rel = Path("releases") / version
        tmp = paths.plugin_dir / f".current.tmp.{os.getpid()}"
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(rel)
        os.replace(tmp, link)

    def current_version(self, plugin_id: str) -> Optional[str]:
        state = self.read_state(plugin_id)
        if state and state.get("version"):
            return str(state["version"])
        paths = self.paths_for(plugin_id)
        if paths.current_link.is_symlink() or paths.current_link.exists():
            try:
                resolved = paths.current_link.resolve()
                return resolved.name
            except OSError:
                return None
        return None

    def load_current_manifest(self, plugin_id: str) -> Optional[PluginManifest]:
        paths = self.paths_for(plugin_id)
        manifest_path = paths.manifest_path()
        if not manifest_path.is_file():
            version = self.current_version(plugin_id)
            if version:
                manifest_path = paths.manifest_path(version)
        if not manifest_path.is_file():
            return None
        return load_manifest_file(manifest_path)

    def write_manifest(self, plugin_id: str, version: str, manifest: PluginManifest) -> Path:
        paths = self.ensure_plugin_layout(plugin_id, version)
        dest = paths.manifest_path(version)
        self._atomic_write_json(dest, manifest.to_dict())
        return dest

    def remove_release_code(self, plugin_id: str, keep_data: bool = True) -> None:
        paths = self.paths_for(plugin_id)
        if paths.current_link.exists() or paths.current_link.is_symlink():
            try:
                paths.current_link.unlink()
            except OSError as exc:
                logger.debug("current unlink failed for %s: %s", plugin_id, exc)
        if paths.releases_dir.exists():
            shutil.rmtree(paths.releases_dir, ignore_errors=True)
        if paths.state_file.exists():
            try:
                paths.state_file.unlink()
            except OSError as exc:
                logger.debug("state unlink failed for %s: %s", plugin_id, exc)
        if not keep_data:
            if paths.data_dir.exists():
                shutil.rmtree(paths.data_dir, ignore_errors=True)
            if paths.logs_dir.exists():
                shutil.rmtree(paths.logs_dir, ignore_errors=True)
            # Remove plugin dir if empty-ish
            if paths.plugin_dir.exists():
                try:
                    remaining = list(paths.plugin_dir.iterdir())
                    if not remaining:
                        paths.plugin_dir.rmdir()
                    else:
                        shutil.rmtree(paths.plugin_dir, ignore_errors=True)
                except OSError:
                    shutil.rmtree(paths.plugin_dir, ignore_errors=True)

    def config_path(self, plugin_id: str) -> Path:
        """Path to the plugin-owned config.json under data/."""
        return self.paths_for(plugin_id).data_dir / "config.json"

    def read_config(self, plugin_id: str) -> dict[str, Any]:
        """Read plugin-owned data/config.json. Missing file → {}."""
        path = self.config_path(plugin_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid plugin config.json: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("plugin config.json must be a JSON object")
        return data

    def write_config(self, plugin_id: str, config: dict[str, Any]) -> Path:
        """Atomically write plugin-owned data/config.json (opaque object)."""
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        paths = self.paths_for(plugin_id)
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_path(plugin_id)
        self._atomic_write_json(path, config)
        return path

    def tail_log(self, plugin_id: str, lines: int = 200) -> list[str]:
        paths = self.paths_for(plugin_id)
        log_path = paths.log_file
        if not log_path.is_file():
            return []
        lines = max(1, min(int(lines), 5000))
        try:
            # Simple tail: read whole file if small; otherwise scan from end
            data = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        all_lines = data.splitlines()
        return all_lines[-lines:]

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


def resolve_plugins_root(
    config: Optional[dict[str, Any]] = None,
    *,
    storage_dir: Optional[Path | str] = None,
) -> Path:
    """Resolve plugins root from config or storage_dir."""
    config = config or {}
    plugins_cfg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    configured = plugins_cfg.get("root") or plugins_cfg.get("plugins_dir")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    if storage_dir is None:
        from repeater.config import resolve_storage_dir

        storage_dir = resolve_storage_dir(config)
    return Path(storage_dir).expanduser().resolve() / DEFAULT_PLUGINS_SUBDIR


def resolve_plugin_socket_path(
    config: Optional[dict[str, Any]] = None,
    *,
    storage_dir: Optional[Path | str] = None,
) -> Path:
    config = config or {}
    plugins_cfg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    configured = plugins_cfg.get("socket") or plugins_cfg.get("socket_path")
    if configured:
        return Path(str(configured)).expanduser().resolve()
    if storage_dir is None:
        from repeater.config import resolve_storage_dir

        storage_dir = resolve_storage_dir(config)
    return Path(storage_dir).expanduser().resolve() / DEFAULT_SOCKET_NAME


def resolve_catalogue_url(config: Optional[dict] = None) -> str:
    """Return configured catalogue URL or the built-in default."""
    from .catalogue import DEFAULT_CATALOGUE_URL

    config = config or {}
    plugins_cfg = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins_cfg, dict):
        return DEFAULT_CATALOGUE_URL
    raw = plugins_cfg.get("catalogue_url") or plugins_cfg.get("catalogue")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_CATALOGUE_URL
