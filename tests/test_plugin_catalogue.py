"""Unit tests for the curated plugin catalogue client."""

from __future__ import annotations

import hashlib
import io
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from repeater.plugins.catalogue import (
    DEFAULT_CATALOGUE_URL,
    CatalogueClient,
    CatalogueError,
    parse_catalogue,
)


VALID = {
    "schema": 1,
    "plugins": [
        {
            "id": "openhop.nomad",
            "name": "NOMAD Bridge",
            "description": "desc",
            "repository": "openhop-dev/openhop-nomad-plugin",
            "category": "integration",
        }
    ],
}

APPROVED_WHEEL = b"approved-wheel"
VALID_V2 = {
    "schema": 2,
    "plugins": [
        {
            "id": "openhop.nomad",
            "name": "NOMAD Bridge",
            "description": "desc",
            "repository": "openhop-dev/openhop-nomad-plugin",
            "version": "0.1.0",
            "wheel_url": (
                "https://repeater-plugins.openhop.dev/plugins/openhop.nomad/0.1.0/"
                "openhop_nomad_plugin-0.1.0-py3-none-any.whl"
            ),
            "sha256": hashlib.sha256(APPROVED_WHEEL).hexdigest(),
        }
    ],
}


def test_default_catalogue_uses_openhop_r2_endpoint():
    assert DEFAULT_CATALOGUE_URL == "https://repeater-plugins.openhop.dev/catalogue.json"


def test_parse_valid_catalogue():
    cat = parse_catalogue(VALID)
    assert cat.schema == 1
    assert len(cat.plugins) == 1
    assert cat.plugins[0].id == "openhop.nomad"
    assert cat.get("openhop.nomad").repository == "openhop-dev/openhop-nomad-plugin"


def test_parse_v2_approved_release_metadata():
    cat = parse_catalogue(VALID_V2)
    plugin = cat.get("openhop.nomad")

    assert cat.schema == 2
    assert plugin.version == "0.1.0"
    assert plugin.wheel_url.endswith("openhop_nomad_plugin-0.1.0-py3-none-any.whl")
    assert plugin.sha256 == hashlib.sha256(APPROVED_WHEEL).hexdigest()


def test_download_v2_wheel_verifies_checksum(tmp_path):
    def opener(request: Request, timeout: float = 30.0):
        assert request.full_url == VALID_V2["plugins"][0]["wheel_url"]
        return _FakeResp(APPROVED_WHEEL)

    plugin = parse_catalogue(VALID_V2).plugins[0]
    path = CatalogueClient(opener=opener).download_wheel(plugin, tmp_path)

    assert path.read_bytes() == APPROVED_WHEEL


def test_download_v2_wheel_rejects_checksum_mismatch(tmp_path):
    def opener(request: Request, timeout: float = 30.0):
        return _FakeResp(b"tampered")

    plugin = parse_catalogue(VALID_V2).plugins[0]
    with pytest.raises(CatalogueError, match="checksum"):
        CatalogueClient(opener=opener).download_wheel(plugin, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_v2_wheel_rejects_redirect_outside_approved_origin(tmp_path):
    def opener(request: Request, timeout: float = 30.0):
        return _FakeResp(APPROVED_WHEEL, final_url="https://example.com/plugin.whl")

    plugin = parse_catalogue(VALID_V2).plugins[0]
    with pytest.raises(CatalogueError, match="approved R2"):
        CatalogueClient(opener=opener).download_wheel(plugin, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_unsupported_schema():
    with pytest.raises(CatalogueError, match="unsupported catalogue schema"):
        parse_catalogue({"schema": 99, "plugins": []})


def test_invalid_repository():
    bad = {
        "schema": 1,
        "plugins": [
            {
                "id": "a.b",
                "name": "A",
                "description": "",
                "repository": "not-a-repo",
            }
        ],
    }
    with pytest.raises(CatalogueError, match="owner/repo"):
        parse_catalogue(bad)


def test_duplicate_plugin_id():
    data = {
        "schema": 1,
        "plugins": [
            {
                "id": "openhop.a",
                "name": "A",
                "description": "",
                "repository": "org/a",
            },
            {
                "id": "openhop.a",
                "name": "B",
                "description": "",
                "repository": "org/b",
            },
        ],
    }
    with pytest.raises(CatalogueError, match="duplicate plugin id"):
        parse_catalogue(data)


def test_duplicate_repository():
    data = {
        "schema": 1,
        "plugins": [
            {
                "id": "openhop.a",
                "name": "A",
                "description": "",
                "repository": "org/same",
            },
            {
                "id": "openhop.b",
                "name": "B",
                "description": "",
                "repository": "org/same",
            },
        ],
    }
    with pytest.raises(CatalogueError, match="duplicate repository"):
        parse_catalogue(data)


class _FakeResp(io.BytesIO):
    def __init__(self, payload: bytes, *, final_url: str | None = None):
        super().__init__(payload)
        self._final_url = final_url

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_and_cache():
    calls = {"n": 0}

    def opener(request: Request, timeout: float = 30.0):
        calls["n"] += 1
        assert "catalogue.json" in request.full_url or request.full_url.endswith("x")
        return _FakeResp(json.dumps(VALID).encode())

    client = CatalogueClient("https://example.test/catalogue.json", cache_ttl=60, opener=opener)
    a = client.fetch()
    client.fetch()  # cached
    assert a.plugins[0].id == "openhop.nomad"
    assert calls["n"] == 1
    c = client.fetch(force_refresh=True)
    assert c.plugins[0].id == "openhop.nomad"
    assert calls["n"] == 2


def test_lookup():
    def opener(request: Request, timeout: float = 30.0):
        return _FakeResp(json.dumps(VALID).encode())

    client = CatalogueClient("https://example.test/c.json", opener=opener)
    plugin = client.get_plugin("openhop.nomad")
    assert plugin.name == "NOMAD Bridge"
    with pytest.raises(CatalogueError, match="not in catalogue"):
        client.get_plugin("missing.plugin")


def test_catalogue_unavailable():
    def opener(request: Request, timeout: float = 30.0):
        raise URLError("offline")

    client = CatalogueClient("https://example.test/c.json", opener=opener)
    with pytest.raises(CatalogueError, match="unavailable"):
        client.fetch()


def test_http_error():
    def opener(request: Request, timeout: float = 30.0):
        raise HTTPError(request.full_url, 500, "err", hdrs=None, fp=None)

    client = CatalogueClient("https://example.test/c.json", opener=opener)
    with pytest.raises(CatalogueError, match="unavailable"):
        client.fetch()
