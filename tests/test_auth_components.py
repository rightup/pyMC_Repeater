from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import jwt
import pytest

from repeater.web.auth.api_tokens import APITokenManager
from repeater.web.auth.cherrypy_tool import check_auth
from repeater.web.auth.jwt_handler import JWTHandler
from repeater.web.auth.middleware import require_auth


def test_jwt_handler_create_and_verify_and_invalid_cases():
    secret = "test-secret-key-minimum-32-bytes!!"
    h = JWTHandler(secret, expiry_minutes=15)
    token = h.create_jwt("admin", "client-1")

    payload = h.verify_jwt(token)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload["client_id"] == "client-1"

    expired = jwt.encode(
        {"sub": "admin", "client_id": "c", "iat": 1, "exp": 1}, secret, algorithm="HS256"
    )
    assert h.verify_jwt(expired) is None
    assert h.verify_jwt("not-a-token") is None


def test_api_token_manager_happy_paths_and_revoke_false():
    db = SimpleNamespace(
        create_api_token=MagicMock(return_value=10),
        verify_api_token=MagicMock(return_value={"id": 10, "name": "n1"}),
        revoke_api_token=MagicMock(side_effect=[True, False]),
        list_api_tokens=MagicMock(return_value=[{"id": 10, "name": "n1"}]),
    )

    mgr = APITokenManager(sqlite_handler=db, secret_key="k")

    token_id, plaintext = mgr.create_token("n1")
    assert token_id == 10
    assert isinstance(plaintext, str)
    assert len(plaintext) == 64

    verified = mgr.verify_token(plaintext)
    assert verified["id"] == 10

    assert mgr.revoke_token(10) is True
    assert mgr.revoke_token(11) is False
    assert mgr.list_tokens()[0]["name"] == "n1"


def _set_cp(monkeypatch, method="GET", path="/api/private", headers=None, params=None, cfg=None):
    req = SimpleNamespace(
        method=method,
        path_info=path,
        headers=headers or {},
        params=params or {},
        user=None,
    )
    resp = SimpleNamespace(status=200, headers={})
    monkeypatch.setattr(cherrypy, "request", req, raising=False)
    monkeypatch.setattr(cherrypy, "response", resp, raising=False)
    monkeypatch.setattr(cherrypy, "config", cfg or {}, raising=False)
    return req, resp


def test_check_auth_skips_options_and_login(monkeypatch):
    _set_cp(monkeypatch, method="OPTIONS")
    assert check_auth() is None

    _set_cp(monkeypatch, method="GET", path="/auth/login")
    assert check_auth() is None


def test_check_auth_missing_handlers_raises_http_500(monkeypatch):
    _set_cp(monkeypatch, cfg={})
    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 500


def test_check_auth_accepts_bearer_token(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: {"sub": "admin", "client_id": "c1"})
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    req, _resp = _set_cp(
        monkeypatch,
        headers={"Authorization": "Bearer abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt"


def test_check_auth_accepts_query_token_and_removes_it(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: {"sub": "admin", "client_id": "c2"})
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    req, _resp = _set_cp(
        monkeypatch,
        params={"token": "xyz", "x": "1"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt_query"
    assert "token" not in req.params


def test_check_auth_accepts_api_key(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: {"id": 3, "name": "svc"})
    req, _resp = _set_cp(
        monkeypatch,
        headers={"X-API-Key": "k"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "api_token"


def test_check_auth_unauthorized_raises_http_error(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    _set_cp(monkeypatch, cfg={"jwt_handler": jwt_handler, "token_manager": token_manager})

    with pytest.raises(cherrypy.HTTPError):
        check_auth()


def test_check_auth_accepts_bearer_api_token(monkeypatch):
    # Bearer value fails JWT verification but matches a device API token --
    # the design doc always allowed Bearer as a transport for API tokens.
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=lambda k: {"id": 7, "name": "svc", "scope": "read"} if k == "abc" else None
    )
    req, _resp = _set_cp(
        monkeypatch,
        headers={"Authorization": "Bearer abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "api_token"
    assert req.user["scope"] == "read"
    assert req.user["token_id"] == 7


def test_check_auth_bearer_invalid_jwt_and_token_still_401s(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    _set_cp(
        monkeypatch,
        headers={"Authorization": "Bearer not-a-real-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()
    assert exc_info.value.status == 401


def test_check_auth_bearer_jwt_wins_over_token_lookup(monkeypatch):
    # A valid JWT must never fall through to the API-token lookup.
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: {"sub": "admin", "client_id": "c1"})
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value={"id": 1, "name": "should-not-be-called"})
    )
    req, _resp = _set_cp(
        monkeypatch,
        headers={"Authorization": "Bearer good-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt"
    token_manager.verify_token.assert_not_called()


def _call_require_auth(monkeypatch, **kwargs):
    """Invoke require_auth's wrapper directly (mirrors _set_cp for check_auth)."""
    req, resp = _set_cp(monkeypatch, **kwargs)

    @require_auth
    def handler():
        return {"success": True}

    return handler, req, resp


def test_require_auth_accepts_bearer_api_token(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=lambda k: {"id": 9, "name": "svc", "scope": "read"} if k == "tok" else None
    )
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        headers={"Authorization": "Bearer tok"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()
    assert result == {"success": True}
    assert req.user["auth_type"] == "api_token"
    assert req.user["scope"] == "read"


def test_require_auth_bearer_invalid_jwt_and_token_still_401s(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    handler, _req, resp = _call_require_auth(
        monkeypatch,
        headers={"Authorization": "Bearer nope"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()
    assert resp.status == 401
    assert result["success"] is False


def test_require_auth_bearer_jwt_wins_over_token_lookup(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: {"sub": "admin", "client_id": "c1"})
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value={"id": 1, "name": "should-not-be-called"})
    )
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        headers={"Authorization": "Bearer good-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()
    assert result == {"success": True}
    assert req.user["auth_type"] == "jwt"
    token_manager.verify_token.assert_not_called()


def test_require_auth_accepts_api_key_unchanged(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: {"id": 3, "name": "svc"})
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        headers={"X-API-Key": "k"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()
    assert result == {"success": True}
    assert req.user["auth_type"] == "api_token"
