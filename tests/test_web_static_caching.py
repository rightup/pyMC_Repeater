import gzip
import os
import time
from pathlib import Path
from types import SimpleNamespace

import cherrypy
import pytest
from cherrypy.lib.httputil import HeaderMap

from repeater.web.http_server import StatsApp
from tests.test_plugin_static_security import plugin_ui  # noqa: F401

JS = "index-DKwPA4Vw.js"
IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture
def http(monkeypatch):
    request = SimpleNamespace(headers=HeaderMap())
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request)
    monkeypatch.setattr(cherrypy, "response", response)
    return request, response


def _bundle(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (assets / JS).write_bytes(b"raw")
    (assets / f"{JS}.br").write_bytes(b"BR")
    (assets / f"{JS}.gz").write_bytes(gzip.compress(b"raw"))
    (assets / "knob.svg").write_bytes(b"<svg/>")
    return assets


def _serve(assets, request, response, name=JS, **headers):
    request.headers = HeaderMap(headers)
    response.headers.clear()
    response.status = 200
    return object.__new__(StatsApp)._serve_static_file(str(assets), (name,))


def test_precompressed_siblings_by_accept_encoding(tmp_path, http):
    request, response = http
    assets = _bundle(tmp_path)

    assert _serve(assets, request, response, **{"Accept-Encoding": "gzip, deflate, br"}) == b"BR"
    assert response.headers["Content-Encoding"] == "br"
    assert response.headers["Content-Type"] == "text/javascript"
    assert response.headers["Vary"] == "Accept-Encoding"
    assert response.headers["Cache-Control"] == IMMUTABLE
    assert response.headers["ETag"].endswith('-br"')

    body = _serve(assets, request, response, **{"Accept-Encoding": "gzip"})
    assert gzip.decompress(body) == b"raw"
    assert response.headers["Content-Encoding"] == "gzip"

    assert _serve(assets, request, response, **{"Accept-Encoding": "br;q=0, gzip;q=0"}) == b"raw"
    assert "Content-Encoding" not in response.headers


def test_only_hashed_names_are_immutable(tmp_path, http):
    request, response = http
    assets = _bundle(tmp_path)
    _serve(assets, request, response, "knob.svg")
    assert response.headers["Cache-Control"] == "no-cache"
    assert StatsApp._is_hashed("login-bunny-CdOv7mzf.webm")
    assert StatsApp._is_hashed("ChartCard-DvNDqE-i.js")
    assert not StatsApp._is_hashed("red-btn-down.svg")
    assert not StatsApp._is_hashed("index-DKwPA4Vw.js.map")


def test_bundled_frontend_matches_the_hash_rule():
    assets = Path(__file__).parents[1] / "repeater" / "web" / "html" / "assets"
    if not assets.is_dir():
        pytest.skip("no bundled frontend")
    built = [p.name for p in assets.iterdir() if p.suffix in (".js", ".css")]
    assert built and all(StatsApp._is_hashed(name) for name in built)


def test_matching_etag_answers_304(tmp_path, http):
    request, response = http
    assets = _bundle(tmp_path)
    _serve(assets, request, response, **{"Accept-Encoding": "br"})
    etag = response.headers["ETag"]
    assert (
        _serve(assets, request, response, **{"Accept-Encoding": "br", "If-None-Match": etag}) == b""
    )
    assert response.status == 304
    assert (
        _serve(assets, request, response, **{"Accept-Encoding": "gzip", "If-None-Match": etag})
        != b""
    )
    assert response.status == 200


def test_symlinked_or_stale_siblings_are_skipped(tmp_path, http):
    request, response = http
    assets = _bundle(tmp_path)
    (assets / f"{JS}.br").unlink()
    (tmp_path / "outside").write_bytes(b"OUTSIDE")
    (assets / f"{JS}.br").symlink_to(tmp_path / "outside")
    assert _serve(assets, request, response, **{"Accept-Encoding": "br, gzip"}) != b"OUTSIDE"
    assert response.headers["Content-Encoding"] == "gzip"
    old = time.time() - 3600
    os.utime(assets / f"{JS}.gz", (old, old))
    assert _serve(assets, request, response, **{"Accept-Encoding": "gzip"}) == b"raw"
    assert "Content-Encoding" not in response.headers


def test_plugin_ui_takes_the_same_path(plugin_ui, http):  # noqa: F811
    request, response = http
    app, release, _manifest = plugin_ui
    _bundle(release / "ui")
    (release / "ui" / "index.html").write_text("<html>ok</html>")
    request.headers["Accept-Encoding"] = "br"
    assert app._serve_plugin_ui("audit.ui", ("assets", JS)) == b"BR"
    assert response.headers["Cache-Control"] == IMMUTABLE
    response.headers.clear()
    assert b"ok" in app._serve_plugin_ui("audit.ui", ("messages", "companion"))
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Type"] == "text/html"
