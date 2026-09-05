"""Plugin manifest parsing and validation (schema 1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:[-+][0-9A-Za-z.-]+)?$"
)
ENTRYPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
SUPPORTED_SCHEMA = 1
SUPPORTED_RUNTIME_TYPES = frozenset({"python"})
SUPPORTED_UI_TYPES = frozenset({"application"})
MANIFEST_FILENAME = "openhop-plugin.json"
ZIP_METADATA_MAX_BYTES = 1024 * 1024
ZIP_MEMBER_MAX_BYTES = 16 * 1024 * 1024
ZIP_TOTAL_MAX_BYTES = 256 * 1024 * 1024
ZIP_MAX_MEMBERS = 4096


def validate_archive_limits(zf) -> None:
    """Bound expanded wheel size before reading or handing it to pip."""
    members = zf.infolist()
    if len(members) > ZIP_MAX_MEMBERS:
        raise ManifestError("wheel exceeds archive member count limit")
    if sum(member.file_size for member in members) > ZIP_TOTAL_MAX_BYTES:
        raise ManifestError("wheel exceeds expanded archive size limit")
    if any(member.file_size > ZIP_MEMBER_MAX_BYTES for member in members):
        raise ManifestError("wheel member exceeds expanded size limit")


def read_archive_member(zf, member, *, metadata: bool = False) -> bytes:
    """Check advertised and actual expanded lengths; never use an unbounded read."""
    limit = ZIP_METADATA_MAX_BYTES if metadata else ZIP_MEMBER_MAX_BYTES
    info = zf.getinfo(member) if isinstance(member, str) else member
    if info.file_size > limit:
        raise ManifestError("wheel member exceeds expanded size limit")
    with zf.open(info) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ManifestError("wheel member exceeds expanded size limit")
    return data


def ui_subtree(entry: str) -> str:
    """Return a dedicated public directory, never the release or runtime root."""
    _reject_path_traversal(entry, "ui.entry")
    parts = entry.split("/")
    if (
        len(parts) < 2
        or any(p in {"", ".", ".."} for p in parts)
        or parts[0].startswith(".")
        or ":" in entry
        or parts[0].lower() in {"venv", "data", "logs", "releases", "current"}
    ):
        raise ManifestError("ui.entry must be inside a dedicated, non-reserved UI subtree")
    return parts[0]


class ManifestError(ValueError):
    """Raised when a plugin manifest is invalid."""


@dataclass(frozen=True)
class RuntimeSpec:
    type: str
    entrypoint: str


@dataclass(frozen=True)
class UISpec:
    type: str
    entry: str


@dataclass(frozen=True)
class PluginManifest:
    schema: int
    id: str
    name: str
    version: str
    description: str = ""
    runtime: Optional[RuntimeSpec] = None
    ui: Optional[UISpec] = None
    # Optional default values for data/config.json (opaque object).
    config_defaults: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "version": self.version,
        }
        if self.description:
            data["description"] = self.description
        if self.runtime is not None:
            data["runtime"] = {
                "type": self.runtime.type,
                "entrypoint": self.runtime.entrypoint,
            }
        if self.ui is not None:
            data["ui"] = {
                "type": self.ui.type,
                "entry": self.ui.entry,
            }
        if self.config_defaults is not None:
            data["config"] = {"defaults": self.config_defaults}
        return data


def _reject_path_traversal(value: str, field: str) -> None:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        raise ManifestError(f"{field} must be a relative path")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ManifestError(f"{field} must not contain parent-directory segments")
    if "\x00" in value:
        raise ManifestError(f"{field} contains a null byte")


def _validate_plugin_id(plugin_id: str) -> str:
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ManifestError("id is required")
    plugin_id = plugin_id.strip()
    if ".." in plugin_id or "/" in plugin_id or "\\" in plugin_id:
        raise ManifestError("id must not contain path separators or '..'")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ManifestError(
            "id must match ^[a-z0-9][a-z0-9._-]*$ (lowercase letters, digits, . _ -)"
        )
    if len(plugin_id) > 128:
        raise ManifestError("id is too long")
    return plugin_id


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not version.strip():
        raise ManifestError("version is required")
    version = version.strip()
    if not VERSION_RE.match(version):
        raise ManifestError("version must look like a semantic version (e.g. 1.2.3)")
    return version


def _validate_runtime(raw: Any) -> RuntimeSpec:
    if not isinstance(raw, dict):
        raise ManifestError("runtime must be an object")
    rtype = raw.get("type")
    if rtype not in SUPPORTED_RUNTIME_TYPES:
        raise ManifestError(
            f"unsupported runtime.type {rtype!r}; supported: {sorted(SUPPORTED_RUNTIME_TYPES)}"
        )
    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ManifestError("runtime.entrypoint is required")
    entrypoint = entrypoint.strip()
    if "/" in entrypoint or "\\" in entrypoint or entrypoint in {".", ".."}:
        raise ManifestError("runtime.entrypoint must be a bare console-script name")
    if not ENTRYPOINT_RE.match(entrypoint):
        raise ManifestError("runtime.entrypoint contains unsafe characters")
    return RuntimeSpec(type=str(rtype), entrypoint=entrypoint)


def _validate_ui(raw: Any) -> UISpec:
    if not isinstance(raw, dict):
        raise ManifestError("ui must be an object")
    utype = raw.get("type")
    if utype not in SUPPORTED_UI_TYPES:
        raise ManifestError(
            f"unsupported ui.type {utype!r}; supported: {sorted(SUPPORTED_UI_TYPES)}"
        )
    entry = raw.get("entry")
    if not isinstance(entry, str) or not entry.strip():
        raise ManifestError("ui.entry is required")
    entry = entry.strip().replace("\\", "/")
    _reject_path_traversal(entry, "ui.entry")
    ui_subtree(entry)
    return UISpec(type=str(utype), entry=entry)


def parse_manifest(data: Any) -> PluginManifest:
    """Validate and parse a schema-1 plugin manifest object."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    schema = data.get("schema")
    if schema != SUPPORTED_SCHEMA:
        raise ManifestError(f"unsupported manifest schema {schema!r}; expected {SUPPORTED_SCHEMA}")

    plugin_id = _validate_plugin_id(data.get("id"))
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("name is required")
    name = name.strip()
    version = _validate_version(data.get("version"))

    description = data.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ManifestError("description must be a string")

    runtime_raw = data.get("runtime")
    ui_raw = data.get("ui")
    if runtime_raw is None and ui_raw is None:
        raise ManifestError("manifest must declare runtime and/or ui")

    runtime = _validate_runtime(runtime_raw) if runtime_raw is not None else None
    ui = _validate_ui(ui_raw) if ui_raw is not None else None
    config_defaults = _validate_config_defaults(data.get("config"))

    return PluginManifest(
        schema=SUPPORTED_SCHEMA,
        id=plugin_id,
        name=name,
        version=version,
        description=description.strip(),
        runtime=runtime,
        ui=ui,
        config_defaults=config_defaults,
    )


def _validate_config_defaults(raw: Any) -> Optional[dict[str, Any]]:
    """Parse optional config.defaults object from the manifest.

    Expected shape:
      "config": { "defaults": { ... } }
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("config must be an object")
    defaults = raw.get("defaults")
    if defaults is None:
        return None
    if not isinstance(defaults, dict):
        raise ManifestError("config.defaults must be a JSON object")
    try:
        encoded = json.dumps(defaults)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"config.defaults is not JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ManifestError("config.defaults exceeds 256 KiB")
    return dict(defaults)


def load_manifest_file(path: Path | str) -> PluginManifest:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"unable to read manifest: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in manifest: {exc}") from exc
    return parse_manifest(data)


def load_manifest_from_wheel(wheel_path: Path | str) -> PluginManifest:
    """Extract and validate openhop-plugin.json from a local wheel."""
    import zipfile

    wheel_path = Path(wheel_path)
    if not wheel_path.is_file():
        raise ManifestError(f"wheel not found: {wheel_path}")

    try:
        with zipfile.ZipFile(wheel_path) as zf:
            validate_archive_limits(zf)
            candidates = [
                name
                for name in zf.namelist()
                if name.endswith(f"/{MANIFEST_FILENAME}") or name == MANIFEST_FILENAME
            ]
            if not candidates:
                raise ManifestError(f"wheel does not contain {MANIFEST_FILENAME}")
            # Prefer share/openhop/plugins/... layout, else first match
            preferred = [c for c in candidates if "share/openhop/plugins/" in c]
            chosen = preferred[0] if preferred else candidates[0]
            raw = read_archive_member(zf, chosen, metadata=True)
    except zipfile.BadZipFile as exc:
        raise ManifestError(f"invalid wheel archive: {exc}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid manifest JSON in wheel: {exc}") from exc
    return parse_manifest(data)
