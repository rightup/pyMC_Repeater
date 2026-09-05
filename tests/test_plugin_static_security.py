"""Public plugin serving must never expose manager/runtime release files."""

from types import SimpleNamespace

import cherrypy
import pytest

from repeater.plugins.storage import PluginStorage
from repeater.web.http_server import StatsApp


@pytest.fixture
def plugin_ui(tmp_path, monkeypatch):
    storage = PluginStorage(tmp_path / "plugins")
    paths = storage.ensure_plugin_layout("audit.ui", "1.0.0")
    release = paths.release_dir("1.0.0")
    storage.set_current("audit.ui", "1.0.0")
    storage.write_state("audit.ui", {"version": "1.0.0", "enabled": True})
    (release / "config.default.json").write_text("PRIVATE-DEFAULT-FIXTURE")
    (release / "venv").mkdir()
    (release / "venv" / "pyvenv.cfg").write_text("PRIVATE-RUNTIME-FIXTURE")
    manifest = SimpleNamespace(ui=SimpleNamespace(entry="ui/index.html"))
    # Exercise serving's own boundary independently of manifest validation.
    monkeypatch.setattr(PluginStorage, "load_current_manifest", lambda *_: manifest)
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}))
    app = object.__new__(StatsApp)
    app.config = {"plugins": {"root": str(storage.root)}}
    return app, release, manifest


@pytest.mark.parametrize("entry", ["index.html", "./index.html", "venv/index.html"])
def test_public_plugin_rejects_release_or_runtime_document_root(plugin_ui, entry):
    app, _, manifest = plugin_ui
    manifest.ui.entry = entry
    requested = "pyvenv.cfg" if entry.startswith("venv/") else "config.default.json"
    with pytest.raises(cherrypy.HTTPError) as exc:
        app._serve_plugin_ui("audit.ui", (requested,))
    assert exc.value.status == 404


def test_public_plugin_rejects_ui_symlink_to_runtime(plugin_ui):
    app, release, _ = plugin_ui
    (release / "ui").symlink_to("venv", target_is_directory=True)
    with pytest.raises(cherrypy.HTTPError) as exc:
        app._serve_plugin_ui("audit.ui", ("pyvenv.cfg",))
    assert exc.value.status == 404


def test_public_plugin_keeps_assets_and_spa_fallback(plugin_ui):
    app, release, _ = plugin_ui
    ui = release / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("PUBLIC-INDEX")
    (ui / "app.js").write_text("PUBLIC-SCRIPT")
    assert app._serve_plugin_ui("audit.ui", ("app.js",)) == b"PUBLIC-SCRIPT"
    assert app._serve_plugin_ui("audit.ui", ("client-route",)) == b"PUBLIC-INDEX"
    assert app._serve_plugin_ui("audit.ui", ("config.default.json",)) == b"PUBLIC-INDEX"
    with pytest.raises(cherrypy.HTTPError) as exc:
        app._serve_plugin_ui("audit.ui", ("..", "config.default.json"))
    assert exc.value.status == 400
