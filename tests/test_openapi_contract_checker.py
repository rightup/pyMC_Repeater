"""Regression tests for the static OpenAPI/CherryPy route checker."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_openapi_contract.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("openapi_contract_checker", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _function(source: str) -> ast.FunctionDef:
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_mobile_v1_dynamic_routes_and_methods_are_collected_exactly():
    routes = checker._collect_routes()
    v1 = {
        path: info.methods
        for path, info in routes.items()
        if path == "/v1" or path.startswith("/v1/")
    }

    assert v1 == {
        "/v1/server_info": {"GET"},
        "/v1/pair": {"POST"},
        "/v1/pair/start": {"POST"},
        "/v1/devices": {"GET"},
        "/v1/devices/{}": {"DELETE"},
        "/v1/devices/{}/push": {"POST", "DELETE"},
        "/v1/companions": {"GET"},
        "/v1/companions/{}/snapshot": {"GET"},
        "/v1/companions/{}/sync": {"GET"},
        "/v1/companions/{}/messages": {"GET", "POST"},
        "/v1/companions/{}/advert": {"POST"},
        "/v1/companions/{}/events": {"GET"},
        "/v1/companions/{}/contacts/{}": {"POST", "DELETE"},
        "/v1/companions/{}/channels/{}": {"PUT", "DELETE"},
        "/v1/companions/{}/contacts/{}/login": {"POST"},
        "/v1/companions/{}/contacts/{}/connection": {"GET"},
        "/v1/companions/{}/contacts/{}/logout": {"POST"},
        "/v1/companions/{}/contacts/{}/status_request": {"POST"},
        "/v1/companions/{}/contacts/{}/telemetry_request": {"POST"},
        "/v1/companions/{}/contacts/{}/reset_path": {"POST"},
        "/v1/companions/{}/messages/{}/receptions": {"GET"},
        "/v1/companions/{}/contacts/{}/paths": {"GET"},
        "/v1/companions/{}/transmissions/{}/repeats": {"GET"},
    }
    assert all(info.confident for path, info in routes.items() if path in v1)


def test_method_inference_understands_helpers_and_local_aliases():
    require_get = _function(
        """
def events(self):
    self._require_get()
    return stream()
"""
    )
    alias_branches = _function(
        """
def messages(self):
    method = cherrypy.request.method
    if method == "POST":
        return send()
    if method in ("GET", "OPTIONS"):
        return history()
    raise cherrypy.HTTPError(405)
"""
    )

    assert checker._infer_methods(require_get) == ({"GET"}, True)
    assert checker._infer_methods(alias_branches) == ({"GET", "POST"}, True)


def test_post_only_guard_is_not_mistaken_for_implicit_get():
    guarded_post = _function(
        """
def send(self):
    if cherrypy.request.method != "POST":
        raise cherrypy.HTTPError(405)
    return transmit()
"""
    )
    post_with_get_fallthrough = _function(
        """
def status(self):
    method = cherrypy.request.method
    if method == "POST":
        return update()
    return read()
"""
    )

    assert checker._infer_methods(guarded_post) == ({"POST"}, True)
    assert checker._infer_methods(post_with_get_fallthrough) == ({"GET", "POST"}, True)


def test_auth_routes_use_their_actual_mount_points():
    routes = checker._collect_routes()

    assert "/auth/tokens" in routes
    assert "/api/auth/tokens" not in routes

    document = yaml.safe_load(checker.OPENAPI_PATH.read_text(encoding="utf-8"))
    assert document["servers"] == [
        {
            "url": "/api",
            "description": "Current server (relative URLs with /api prefix)",
        }
    ]
    for path, method in (
        ("/auth/login", "post"),
        ("/auth/refresh", "post"),
        ("/auth/verify", "get"),
        ("/auth/change_password", "post"),
    ):
        assert document["paths"][path][method]["servers"] == [{"url": "/"}]
    assert "/api/auth/tokens" not in document["paths"]
