"""Offline resource-boundary regressions for both plugin download paths."""

import hashlib
import http.client
import io
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from repeater.plugins import catalogue, github_releases

WHEEL = b"test wheel"
URL = "https://github.com/org/repo/releases/download/v1.0/pkg-1.0-py3-none-any.whl"


def operation(kind, opener, tmp_path):
    if kind.startswith("catalogue"):
        client = catalogue.CatalogueClient(opener=opener)
        if kind.endswith("json"):
            return client.fetch()
        plugin = catalogue.CataloguePlugin(
            "test",
            "Test",
            "",
            "org/repo",
            version="1.0",
            wheel_url=URL,
            sha256=hashlib.sha256(WHEEL).hexdigest(),
        )
        return client.download_wheel(plugin, tmp_path)
    client = github_releases.GitHubReleaseClient(opener=opener)
    if kind.endswith("json"):
        return client.list_releases("org/repo")
    release = client._parse_release(
        {
            "tag_name": "v1.0",
            "assets": [{"name": "pkg-1.0-py3-none-any.whl", "browser_download_url": URL}],
        }
    )
    return client.download_wheel(release, tmp_path)


KINDS = ["catalogue-json", "releases-json", "catalogue-wheel", "releases-wheel"]


@pytest.mark.parametrize("kind", KINDS)
def test_truncated_content_length_is_rejected(kind, tmp_path):
    payload = (
        b'{"schema": 1, "plugins": []}'
        if kind == "catalogue-json"
        else b"[]"
        if kind == "releases-json"
        else WHEEL
    )
    reader, writer = socket.socketpair()
    try:
        writer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + payload)
        writer.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(reader)
        response.begin()

        def opener(request, timeout):
            response.url = request.full_url
            return response

        error = (
            catalogue.CatalogueError
            if kind.startswith("catalogue")
            else github_releases.GitHubReleasesError
        )
        with pytest.raises(error):
            operation(kind, opener, tmp_path)
        assert response.closed
        assert list(tmp_path.iterdir()) == []
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/catalogue.json",
        "file:///tmp/catalogue.json",
        "ftp://example.test/catalogue.json",
    ],
)
def test_initial_catalogue_url_requires_https(url):
    with pytest.raises(catalogue.CatalogueError, match="HTTPS"):
        catalogue.CatalogueClient(url=url)


def test_custom_https_catalogue_url_is_preserved():
    url = "https://custom.example.test:8443/custom.json?channel=beta"
    assert catalogue.CatalogueClient(url=url).url == url


@pytest.mark.parametrize("kind", KINDS)
def test_response_size_is_bounded_without_content_length(kind, monkeypatch, tmp_path):
    module = catalogue if kind.startswith("catalogue") else github_releases
    cap = "MAX_JSON_BYTES" if kind.endswith("json") else "MAX_WHEEL_BYTES"
    monkeypatch.setattr(module, cap, 128, raising=False)
    response = io.BytesIO(b" " * 129 if kind.endswith("json") else WHEEL * 20)
    error = catalogue.CatalogueError if module is catalogue else github_releases.GitHubReleasesError
    with pytest.raises(error, match="exceeds"):
        operation(kind, lambda *a, **kw: response, tmp_path)
    assert response.closed
    assert list(tmp_path.iterdir()) == []


@contextmanager
def slow_response(mode):
    """Real stdlib HTTPResponse over local sockets, never the public network."""
    reader, writer = socket.socketpair()
    stop = threading.Event()

    def serve():
        try:
            if mode == "chunk-header":
                writer.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;")
            else:
                writer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            for _ in range(30):
                if stop.wait(0.02):
                    return
                if mode != "idle":
                    writer.sendall(b" ")
        except OSError:
            pass
        finally:
            writer.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    response = http.client.HTTPResponse(reader)
    response.begin()

    def opener(request, timeout):
        response.url = request.full_url
        reader.settimeout(timeout)
        return response

    try:
        yield opener, response
    finally:
        stop.set()
        reader.close()
        thread.join(timeout=2)
        response.close()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ["trickle", "idle", "chunk-header"])
def test_total_body_deadline_interrupts_real_http_response(kind, mode, monkeypatch, tmp_path):
    module = catalogue if kind.startswith("catalogue") else github_releases
    setting = (
        "JSON_DOWNLOAD_TIMEOUT_SECONDS"
        if kind.endswith("json")
        else "WHEEL_DOWNLOAD_TIMEOUT_SECONDS"
    )
    monkeypatch.setattr(module, setting, 0.15, raising=False)
    error = catalogue.CatalogueError if module is catalogue else github_releases.GitHubReleasesError
    with slow_response(mode) as (opener, response):
        started = time.monotonic()
        with pytest.raises(error):
            operation(kind, opener, tmp_path)
        elapsed = time.monotonic() - started
        assert elapsed < 0.45, f"deadline did not interrupt {mode}: {elapsed:.3f}s"
        assert response.closed
    assert list(tmp_path.iterdir()) == []
