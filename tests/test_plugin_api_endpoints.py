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


@pytest.mark.parametrize("upload_safe", [False, True])
def test_upload_timeout_reports_unknown_and_preserves_unacknowledged_file(
    tmp_path, cherrypy_request, upload_safe
):
    from io import BytesIO
    from pathlib import Path
    from types import SimpleNamespace
    from repeater.plugins.ipc import PluginIPCOutcomeUnknown

    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    uploaded = []

    def install(path):
        uploaded.append(Path(path))
        raise PluginIPCOutcomeUnknown(upload_safe=upload_safe)

    client = SimpleNamespace(install=install)
    cherrypy.request.method = "POST"
    wheel = SimpleNamespace(filename="demo-1-py3-none-any.whl", file=BytesIO(b"wheel"))
    with patch.object(api, "_client_or_raise", return_value=client):
        result = api.install(wheel=wheel)
    assert cherrypy.response.status == 504
    assert result["outcome"] == "unknown"
    assert "may still complete" in result["error"]
    assert uploaded[0].exists() is (not upload_safe)


def test_upload_completed_install_preserves_sync_contract(tmp_path, cherrypy_request):
    from io import BytesIO
    from pathlib import Path
    from types import SimpleNamespace

    api = PluginAPIEndpoints({"storage": {"storage_dir": str(tmp_path)}})
    uploaded = []

    def install(path):
        uploaded.append(Path(path))
        assert uploaded[0].read_bytes() == b"wheel"
        return {"id": "demo", "version": "1"}

    cherrypy.request.method = "POST"
    wheel = SimpleNamespace(filename="demo-1-py3-none-any.whl", file=BytesIO(b"wheel"))
    with patch.object(api, "_client_or_raise", return_value=SimpleNamespace(install=install)):
        result = api.install(wheel=wheel)
    assert result == {"success": True, "plugin": {"id": "demo", "version": "1"}}
    assert not uploaded[0].exists()


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

    # Paths are relative to the entry directory (ui/), matching Vite base './'
    asset = app._serve_plugin_ui(plugin_id, ("assets", "app.js"))
    assert b"console.log" in asset

    # SPA fallback
    spa = app._serve_plugin_ui(plugin_id, ("dashboard", "settings"))
    assert b"ok" in spa

    # Path traversal rejected
    with pytest.raises(cherrypy.HTTPError):
        app._serve_plugin_ui(plugin_id, ("..", "..", "etc", "passwd"))
