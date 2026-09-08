"""web_frontends listing for primary UI switcher."""

from __future__ import annotations

import json
from pathlib import Path

from repeater.web.api_endpoints import APIEndpoints


def test_read_ui_version_from_version_file(tmp_path: Path):
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    assert APIEndpoints._read_ui_version(str(tmp_path)) == "9.9.9"


def test_web_frontends_builtin_and_plugin(tmp_path: Path, monkeypatch):
    # Minimal plugin layout with UI
    plugins_root = tmp_path / "plugins"
    plugin_id = "openhop.demo"
    release = plugins_root / plugin_id / "releases" / "1.2.3"
    ui = release / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": "Demo UI",
        "version": "1.2.3",
        "description": "Demo",
        "ui": {"type": "application", "entry": "ui/index.html"},
    }
    (release / "openhop-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugins_root / plugin_id / "state.json").write_text(
        json.dumps({"id": plugin_id, "version": "1.2.3", "enabled": True, "source": "catalogue"}),
        encoding="utf-8",
    )
    current = plugins_root / plugin_id / "current"
    current.symlink_to(Path("releases/1.2.3"))

    cfg = {"plugins": {"root": str(plugins_root)}, "web": {"web_path": None}}
    api = APIEndpoints(config=cfg, config_path=str(tmp_path / "config.yaml"))

    # Avoid needing cherrypy request context for CORS; call internals
    items = api._plugin_ui_frontends()
    assert len(items) == 1
    assert items[0]["plugin_id"] == plugin_id
    assert items[0]["version"] == "1.2.3"
    assert items[0]["available"] is True
    assert items[0]["path"].endswith("/ui")
    assert "/current/" in items[0]["path"]


def test_normalize_web_path_for_plugin_frontend(tmp_path: Path):
    plugins_root = tmp_path / "plugins"
    plugin_id = "openhop.demo"
    release = plugins_root / plugin_id / "releases" / "1.2.3"
    ui = release / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": "Demo UI",
        "version": "1.2.3",
        "description": "Demo",
        "ui": {"type": "application", "entry": "ui/index.html"},
    }
    (release / "openhop-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugins_root / plugin_id / "state.json").write_text(
        json.dumps({"id": plugin_id, "version": "1.2.3", "enabled": True, "source": "local"}),
        encoding="utf-8",
    )
    (plugins_root / plugin_id / "current").symlink_to(Path("releases/1.2.3"))

    cfg = {"plugins": {"root": str(plugins_root)}, "web": {"web_path": None}}
    api = APIEndpoints(config=cfg, config_path=str(tmp_path / "config.yaml"))

    legacy_release_path = str(plugins_root / plugin_id / "releases" / "1.2.3" / "ui")
    stable_current_path = str(plugins_root / plugin_id / "current" / "ui")

    assert api._normalize_web_path_for_persistence(legacy_release_path) == f"plugin:{plugin_id}"
    assert api._normalize_web_path_for_persistence(stable_current_path) == f"plugin:{plugin_id}"
