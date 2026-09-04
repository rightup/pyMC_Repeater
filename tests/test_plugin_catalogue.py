"""Unit tests for the curated plugin catalogue client."""

from __future__ import annotations

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


def test_default_catalogue_uses_openhop_r2_endpoint():
    assert DEFAULT_CATALOGUE_URL == "https://repeater-plugins.openhop.dev/catalogue.json"


def test_parse_valid_catalogue():
    cat = parse_catalogue(VALID)
    assert cat.schema == 1
    assert len(cat.plugins) == 1
    assert cat.plugins[0].id == "openhop.nomad"
    assert cat.get("openhop.nomad").repository == "openhop-dev/openhop-nomad-plugin"


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
