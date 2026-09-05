"""Tests for plugin API endpoints and static UI serving."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import cherrypy
import pytest

from repeater.plugins.manifest import PluginManifest, UISpec
from repeater.plugins.storage import PluginStorage
from repeater.web.http_server import StatsApp
from repeater.web.plugin_endpoints import PluginAPIEndpoints


class _Resp:
    def __init__(self):
        self.status = 200
        self.headers = {}


@pytest.fixture
def cherrypy_request():
    cherrypy.request.method = "GET"
    cherrypy.request.json = {}
    cherrypy.response = _Resp()
    yield
    cherrypy.response = _Resp()


def test_list_returns_503_when_manager_missing(tmp_path, cherrypy_request):
    cfg = {"storage": {"storage_dir": str(tmp_path)}}
    api = PluginAPIEndpoints(cfg)
    cherrypy.request.method = "GET"
    result = api.index()
    assert result["success"] is False
    assert cherrypy.response.status == 503


def test_list_and_status_via_client_mock(tmp_path, cherrypy_request):
    cfg = {"storage": {"storage_dir": str(tmp_path)}}
    api = PluginAPIEndpoints(cfg)
    mock_client = MagicMock()
    mock_client.available.return_value = True
    mock_client.list_plugins.return_value = [
        {"id": "openhop.demo", "state": "STOPPED", "enabled": False}
    ]
    mock_client.status.return_value = {
        "id": "openhop.demo",
        "state": "RUNNING",
        "enabled": True,
    }
    mock_client.enable.return_value = {"id": "openhop.demo", "enabled": True, "state": "RUNNING"}
    mock_client.get_runtime.return_value = {
        "id": "openhop.demo",
        "exists": True,
        "runtime": {"schema": 1, "running": True},
    }

    with patch.object(api, "_client_or_raise", return_value=mock_client):
        cherrypy.request.method = "GET"
        listed = api.index()
        assert listed["success"] is True
        assert listed["plugins"][0]["id"] == "openhop.demo"

        status = api.default("openhop.demo")
        assert status["success"] is True
        assert status["state"] == "RUNNING"

        cherrypy.request.method = "POST"
        cherrypy.request.json = {"id": "openhop.demo"}
        enabled = api.enable()
        assert enabled["success"] is True

        cherrypy.request.method = "GET"
        runtime = api.runtime(id="openhop.demo")
        assert runtime["success"] is True
        assert runtime["runtime"]["schema"] == 1


def test_ui_serving_index_assets_spa_and_disabled(tmp_path):
    plugins_root = tmp_path / "plugins"
    storage = PluginStorage(plugins_root)
    plugin_id = "openhop.console"
    version = "1.0.0"
    storage.ensure_plugin_layout(plugin_id, version)
    storage.write_manifest(
        plugin_id,
        version,
        PluginManifest(
            schema=1,
            id=plugin_id,
            name="Console",
            version=version,
            ui=UISpec(type="application", entry="ui/index.html"),
        ),
    )
    storage.set_current(plugin_id, version)
    release = storage.paths_for(plugin_id).release_dir(version)
    (release / "ui").mkdir(parents=True)
    (release / "ui" / "index.html").write_text("<html>ok</html>")
    (release / "ui" / "assets").mkdir()
    (release / "ui" / "assets" / "app.js").write_text("console.log(1)")

    app = StatsApp(config={"storage": {"storage_dir": str(tmp_path)}})

    # Disabled → 404
    storage.write_state(plugin_id, {"version": version, "enabled": False})
    with pytest.raises(cherrypy.HTTPError):
        # NotFound is HTTPError subclass in cherrypy
        try:
            app._serve_plugin_ui(plugin_id, ())
        except cherrypy.NotFound:
            raise cherrypy.HTTPError(404)

    storage.write_state(plugin_id, {"version": version, "enabled": True})

    index = app._serve_plugin_ui(plugin_id, ())
    assert b"ok" in index
    assert cherrypy.response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"

    # Paths are relative to the entry directory (ui/), matching Vite base './'
    asset = app._serve_plugin_ui(plugin_id, ("assets", "app.js"))
    assert b"console.log" in asset

    # SPA fallback
    spa = app._serve_plugin_ui(plugin_id, ("dashboard", "settings"))
    assert b"ok" in spa
    assert cherrypy.response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"

    # Path traversal rejected
    with pytest.raises(cherrypy.HTTPError):
        app._serve_plugin_ui(plugin_id, ("..", "..", "etc", "passwd"))
