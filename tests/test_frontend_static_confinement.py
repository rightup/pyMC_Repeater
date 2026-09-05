"""Static frontend roots must use path components, not string prefixes."""

from http.client import HTTPConnection
from threading import Thread
from types import SimpleNamespace
from wsgiref.simple_server import make_server

import cherrypy
import pytest

from repeater.web.http_server import StatsApp


@pytest.mark.parametrize("directory", ["assets", "_next"])
@pytest.mark.parametrize("symlink", [False, True])
def test_static_rejects_prefix_sharing_sibling(tmp_path, monkeypatch, directory, symlink):
    root = tmp_path / directory
    root.mkdir()
    private = tmp_path / (directory + "-private")
    private.mkdir()
    (private / "probe.txt").write_text("OUTSIDE-ROOT")
    parts = ("..", private.name, "probe.txt")
    if symlink:
        (root / "linked").symlink_to(private, target_is_directory=True)
        parts = ("linked", "probe.txt")
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}))
    app = object.__new__(StatsApp)
    with pytest.raises(cherrypy.NotFound):
        app._serve_static_file(str(root), parts)


def test_static_keeps_nested_assets(tmp_path, monkeypatch):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "app.js").write_text("PUBLIC-SCRIPT")
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}))
    app = object.__new__(StatsApp)
    assert app._serve_static_file(str(tmp_path), ("nested", "app.js")) == b"PUBLIC-SCRIPT"


@pytest.mark.parametrize("directory", ["assets", "_next"])
def test_public_dispatch_rejects_encoded_sibling_paths(tmp_path, monkeypatch, directory):
    root = tmp_path / directory
    root.mkdir()
    (root / "app.js").write_text("PUBLIC-SCRIPT")
    private = tmp_path / (directory + "-private")
    private.mkdir()
    (private / "probe.txt").write_text("OUTSIDE-ROOT")
    monkeypatch.setattr(cherrypy, "request", cherrypy._ThreadLocalProxy("request"))
    monkeypatch.setattr(cherrypy, "response", cherrypy._ThreadLocalProxy("response"))
    app = object.__new__(StatsApp)
    app.html_dir = str(tmp_path)
    monkeypatch.setattr(app, "_resolve_html_dir", lambda: app.html_dir)
    application = cherrypy.Application(app, script_name="")
    with make_server("127.0.0.1", 0, application) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for path in (
                f"/{directory}/../{directory}-private/probe.txt",
                f"/{directory}/..%2f{directory}-private%2fprobe.txt",
                f"/{directory}/%2e%2e/{directory}-private/probe.txt",
            ):
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    assert response.status == 404, path
                    assert b"OUTSIDE-ROOT" not in response.read()
                finally:
                    connection.close()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", f"/{directory}/app.js")
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == b"PUBLIC-SCRIPT"
            finally:
                connection.close()
        finally:
            server.shutdown()
            worker.join(timeout=3)
