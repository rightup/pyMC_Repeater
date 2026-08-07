import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import jwt
import pytest

from repeater.data_acquisition.sqlite_handler import CompanionStorageError
from repeater.web.auth.api_tokens import APITokenManager, safe_api_token_name
from repeater.web.auth.cherrypy_tool import check_auth
from repeater.web.auth.jwt_handler import JWTHandler
from repeater.web.auth.middleware import require_admin, require_auth
from repeater.web.auth.policy import (
    allows_query_jwt,
    api_token_scope,
    bearer_token_from_header,
    is_known_scope,
    scope_allows_api_path,
)

_JWT_SECRET = "test-secret-key-minimum-32-bytes!!"


def test_jwt_handler_create_and_verify_and_invalid_cases():
    secret = _JWT_SECRET
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


def test_jwt_handler_quiet_probe_suppresses_only_expected_rejection_logs(caplog):
    handler = JWTHandler(_JWT_SECRET)
    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": "admin",
            "client_id": "client",
            "iat": 1,
            "exp": 1,
        },
        _JWT_SECRET,
        algorithm="HS256",
    )
    invalid_claim = jwt.encode(
        {
            "sub": "admin\nroot",
            "client_id": "client",
            "iat": now,
            "exp": now + 300,
        },
        _JWT_SECRET,
        algorithm="HS256",
    )

    with caplog.at_level(logging.WARNING):
        for token in ("not-a-jwt", expired, invalid_claim):
            assert handler.verify_jwt(token, quiet=True) is None

    assert caplog.records == []

    with caplog.at_level(logging.WARNING):
        assert handler.verify_jwt("not-a-jwt") is None

    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage().startswith("Invalid JWT token:")


@pytest.mark.parametrize("secret", [None, 123, "", " ", "x" * 31, "é" * 15])
def test_jwt_handler_constructor_rejects_weak_secret(secret):
    with pytest.raises(ValueError, match="jwt_secret"):
        JWTHandler(secret)


@pytest.mark.parametrize("expiry_minutes", [True, "60", 1.0, 0, 10_081])
def test_jwt_handler_constructor_rejects_invalid_expiry(expiry_minutes):
    with pytest.raises(ValueError, match="jwt_expiry_minutes"):
        JWTHandler("x" * 32, expiry_minutes=expiry_minutes)


@pytest.mark.parametrize("missing_claim", ["exp", "iat", "sub", "client_id"])
def test_jwt_handler_requires_every_server_claim(missing_claim):
    secret = "test-secret-key-minimum-32-bytes!!"
    now = int(__import__("time").time())
    payload = {
        "sub": "admin",
        "client_id": "client",
        "iat": now - 1,
        "exp": now + 300,
    }
    payload.pop(missing_claim)
    token = jwt.encode(payload, secret, algorithm="HS256")

    assert JWTHandler(secret).verify_jwt(token) is None


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("sub", ""),
        ("sub", " admin"),
        ("sub", "a" * 65),
        ("sub", "admin\nroot"),
        ("sub", "admin\u202eroot"),
        ("client_id", ""),
        ("client_id", "client "),
        ("client_id", "c" * 129),
        ("client_id", "client\tother"),
        ("client_id", "client\u0085other"),
        ("client_id", "client\u200bother"),
        ("client_id", "client\u2028other"),
    ],
)
def test_jwt_handler_rejects_invalid_text_claims(claim, value):
    secret = "test-secret-key-minimum-32-bytes!!"
    now = int(__import__("time").time())
    payload = {
        "sub": "admin",
        "client_id": "client",
        "iat": now,
        "exp": now + 300,
    }
    payload[claim] = value
    token = jwt.encode(payload, secret, algorithm="HS256")

    assert JWTHandler(secret).verify_jwt(token) is None


def test_api_token_manager_happy_paths_and_revoke_false():
    db = SimpleNamespace(
        create_api_token_strict=MagicMock(return_value=10),
        verify_api_token_strict=MagicMock(return_value={"id": 10, "name": "n1"}),
        get_api_token_by_id_strict=MagicMock(return_value={"id": 10, "name": "n1"}),
        revoke_api_token_strict=MagicMock(side_effect=[True, False]),
        list_api_tokens_strict=MagicMock(return_value=[{"id": 10, "name": "n1"}]),
    )

    mgr = APITokenManager(sqlite_handler=db, secret_key="k")

    token_id, plaintext = mgr.create_token("n1")
    assert token_id == 10
    assert isinstance(plaintext, str)
    assert len(plaintext) == 64

    verified = mgr.verify_token(plaintext)
    assert verified["id"] == 10
    assert mgr.get_token(10)["id"] == 10

    assert mgr.revoke_token(10) is True
    assert mgr.revoke_token(11) is False
    assert mgr.list_tokens()[0]["name"] == "n1"


def test_api_token_manager_propagates_storage_failures():
    unavailable = CompanionStorageError("private database detail")
    db = SimpleNamespace(
        create_api_token_strict=MagicMock(side_effect=unavailable),
        verify_api_token_strict=MagicMock(side_effect=unavailable),
        get_api_token_by_id_strict=MagicMock(side_effect=unavailable),
        revoke_api_token_strict=MagicMock(side_effect=unavailable),
        list_api_tokens_strict=MagicMock(side_effect=unavailable),
    )
    manager = APITokenManager(sqlite_handler=db, secret_key="k")

    with pytest.raises(CompanionStorageError):
        manager.create_token("operator")
    with pytest.raises(CompanionStorageError):
        manager.verify_token("presented-token")
    with pytest.raises(CompanionStorageError):
        manager.get_token(1)
    with pytest.raises(CompanionStorageError):
        manager.revoke_token(1)
    with pytest.raises(CompanionStorageError):
        manager.list_tokens()


def test_api_token_manager_escapes_legacy_names_without_invalidating_tokens():
    stored = {
        "id": 10,
        "name": "legacy\n\u202eoperator",
        "scope": "admin",
    }
    db = SimpleNamespace(
        verify_api_token_strict=MagicMock(return_value=stored),
        list_api_tokens_strict=MagicMock(return_value=[stored]),
    )
    manager = APITokenManager(sqlite_handler=db, secret_key="k")

    verified = manager.verify_token("presented-token")
    listed = manager.list_tokens()

    assert verified["name"] == "legacy\\u000a\\u202eoperator"
    assert listed[0]["name"] == "legacy\\u000a\\u202eoperator"
    assert stored["name"] == "legacy\n\u202eoperator"
    assert manager.verify_token("presented-token")["scope"] == "admin"


def test_safe_api_token_name_is_printable_and_bounded():
    rendered = safe_api_token_name("x" * 2000)

    assert rendered.endswith("…")
    assert len(rendered.encode("utf-8")) <= 1024
    assert all(character.isprintable() for character in rendered)
    assert safe_api_token_name("") == "<unnamed>"


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


def test_check_auth_terminates_options_before_protected_handler(monkeypatch):
    req, resp = _set_cp(monkeypatch, method="OPTIONS")
    protected = MagicMock(return_value=b"unsafe")
    req.handler = protected

    assert check_auth() is None
    assert resp.status == 204
    assert req.handler() is None
    protected.assert_not_called()


def test_check_auth_skips_login(monkeypatch):
    _set_cp(monkeypatch, method="GET", path="/auth/login")
    assert check_auth() is None


def test_check_auth_missing_handlers_raises_http_500(monkeypatch):
    _set_cp(monkeypatch, cfg={})
    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 500


def test_check_auth_accepts_bearer_token(monkeypatch):
    expires_at = time.time() + 300
    jwt_handler = SimpleNamespace(
        verify_jwt=lambda _t, **_kwargs: {
            "sub": "admin",
            "client_id": "c1",
            "exp": expires_at,
        }
    )
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    req, _resp = _set_cp(
        monkeypatch,
        headers={"Authorization": "Bearer abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt"
    assert req._openhop_jwt_expires_at == expires_at


def test_check_auth_accepts_case_insensitive_bearer_scheme(monkeypatch):
    jwt_handler = SimpleNamespace(
        verify_jwt=MagicMock(return_value={"sub": "admin", "client_id": "c1"})
    )
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    req, _resp = _set_cp(
        monkeypatch,
        headers={"Authorization": "bearer abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt"
    jwt_handler.verify_jwt.assert_called_once_with("abc")


@pytest.mark.parametrize(
    ("headers", "params"),
    [
        (
            {
                "Authorization": "Bearer header-jwt",
                "X-API-Key": "device-token",
            },
            {},
        ),
        (
            {"Authorization": "Bearer header-jwt"},
            {"token": "query-jwt"},
        ),
        (
            {"X-API-Key": "device-token"},
            {"token": "query-jwt"},
        ),
    ],
)
def test_check_auth_rejects_multiple_v1_credential_transports(
    monkeypatch,
    headers,
    params,
):
    verify_jwt = MagicMock(return_value={"sub": "admin", "client_id": "c1"})
    verify_token = MagicMock(return_value=None)
    jwt_handler = SimpleNamespace(
        verify_jwt=verify_jwt,
    )
    token_manager = SimpleNamespace(verify_token=verify_token)
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/v1/companions/home/events",
        headers=headers,
        params=params,
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 400
    assert "token" not in req.params
    assert req.user is None
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()


def test_check_auth_keeps_legacy_credential_precedence(monkeypatch):
    verify_token = MagicMock(return_value=None)
    jwt_handler = SimpleNamespace(
        verify_jwt=lambda _t, **_kwargs: {"sub": "admin", "client_id": "c1"}
    )
    token_manager = SimpleNamespace(verify_token=verify_token)
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/private",
        headers={
            "Authorization": "Bearer operator-jwt",
            "X-API-Key": "legacy-api-token",
        },
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt"
    verify_token.assert_not_called()


def test_check_auth_accepts_query_token_and_removes_it(monkeypatch):
    verify_jwt = MagicMock(return_value={"sub": "admin", "client_id": "c2"})
    jwt_handler = SimpleNamespace(verify_jwt=verify_jwt)
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/v1/companions/home/events",
        params={"token": "xyz", "x": "1"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt_query"
    assert "token" not in req.params
    verify_jwt.assert_called_once_with("xyz")


def test_check_auth_preserves_exact_legacy_sse_query_jwt(monkeypatch):
    jwt_handler = SimpleNamespace(
        verify_jwt=lambda token: (
            {"sub": "admin", "client_id": "legacy-sse"} if token == "operator-jwt" else None
        )
    )
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/companion/events",
        params={"token": "operator-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "jwt_query"
    assert req.user["scope"] == "admin"
    assert "token" not in req.params
    token_manager.verify_token.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/companions/home/sync"),
        ("GET", "/api/v1/companions/home/events/"),
        ("GET", "/api/v1/companions/bad%2Fname/events"),
        ("GET", "/api/companion/events/"),
        ("POST", "/api/companion/events"),
        ("GET", "/api/companion/snapshot"),
        ("GET", "/ws/packets"),
        ("GET", "/ws/companion_frame"),
        ("GET", "/auth/me"),
        ("POST", "/api/v1/companions/home/events"),
    ],
)
def test_check_auth_rejects_query_jwt_outside_exact_mobile_sse(
    monkeypatch,
    method,
    path,
):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value={"sub": "admin"}))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    req, _resp = _set_cp(
        monkeypatch,
        method=method,
        path=path,
        params={"token": "query-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 401
    assert "token" not in req.params
    jwt_handler.verify_jwt.assert_not_called()
    token_manager.verify_token.assert_not_called()


def test_check_auth_accepts_api_key(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: {"id": 3, "name": "svc"})
    req, _resp = _set_cp(
        monkeypatch,
        headers={"X-API-Key": "k"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["auth_type"] == "api_token"


def test_check_auth_unauthorized_raises_http_error(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: None)
    _set_cp(monkeypatch, cfg={"jwt_handler": jwt_handler, "token_manager": token_manager})

    with pytest.raises(cherrypy.HTTPError):
        check_auth()


def test_check_auth_accepts_bearer_api_token_without_false_jwt_warning(
    monkeypatch,
    caplog,
):
    # Bearer value fails JWT verification but matches a device API token --
    # the design doc always allowed Bearer as a transport for API tokens.
    jwt_handler = JWTHandler(_JWT_SECRET)
    token_manager = SimpleNamespace(
        verify_token=lambda k: (
            {"id": 7, "name": "phone", "scope": "companion:home"} if k == "abc" else None
        )
    )
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/v1/companions/home/sync",
        headers={"Authorization": "Bearer abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with caplog.at_level(logging.WARNING):
        assert check_auth() is None

    assert req.user["auth_type"] == "api_token"
    assert req.user["scope"] == "companion:home"
    assert req.user["token_id"] == 7
    assert caplog.records == []


def test_check_auth_invalid_bearer_logs_one_sanitized_failure(
    monkeypatch,
    caplog,
):
    credential = "not-a-real-token"
    jwt_handler = JWTHandler(_JWT_SECRET)
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    _set_cp(
        monkeypatch,
        headers={"Authorization": f"Bearer {credential}"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with caplog.at_level(logging.WARNING), pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 401
    assert [record.getMessage() for record in caplog.records] == [
        "Unauthorized access attempt to /api/private"
    ]
    assert credential not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer unavailable"},
        {"X-API-Key": "unavailable"},
    ],
)
def test_check_auth_reports_api_token_storage_failure_as_503(monkeypatch, headers):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(
        verify_token=MagicMock(side_effect=CompanionStorageError("private database detail"))
    )
    _set_cp(
        monkeypatch,
        headers=headers,
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 503
    assert exc_info.value.args[1] == "Authentication storage unavailable"
    assert "private database detail" not in str(exc_info.value.args)


def test_check_auth_bearer_jwt_wins_over_token_lookup(monkeypatch):
    # A valid JWT must never fall through to the API-token lookup.
    jwt_handler = SimpleNamespace(
        verify_jwt=lambda _t, **_kwargs: {"sub": "admin", "client_id": "c1"}
    )
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


def test_require_auth_options_never_calls_handler(monkeypatch):
    req, resp = _set_cp(monkeypatch, method="OPTIONS")
    protected = MagicMock(return_value={"success": True})
    handler = require_auth(protected)

    assert handler() is None
    assert resp.status == 204
    assert req.user is None
    protected.assert_not_called()


def test_require_auth_accepts_bearer_api_token_without_false_jwt_warning(
    monkeypatch,
    caplog,
):
    jwt_handler = JWTHandler(_JWT_SECRET)
    token_manager = SimpleNamespace(
        verify_token=lambda k: (
            {"id": 9, "name": "phone", "scope": "companion:home"} if k == "tok" else None
        )
    )
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        path="/api/v1/companions/home/snapshot",
        headers={"Authorization": "Bearer tok"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with caplog.at_level(logging.WARNING):
        result = handler()

    assert result == {"success": True}
    assert req.user["auth_type"] == "api_token"
    assert req.user["scope"] == "companion:home"
    assert caplog.records == []


def test_require_auth_accepts_case_insensitive_bearer_scheme(monkeypatch):
    jwt_handler = SimpleNamespace(
        verify_jwt=MagicMock(return_value={"sub": "admin", "client_id": "c1"})
    )
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        headers={"Authorization": "bEaReR abc"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert handler() == {"success": True}
    assert req.user["auth_type"] == "jwt"
    jwt_handler.verify_jwt.assert_called_once_with("abc")


def test_require_auth_invalid_bearer_logs_one_sanitized_failure(
    monkeypatch,
    caplog,
):
    credential = "nope"
    jwt_handler = JWTHandler(_JWT_SECRET)
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    handler, _req, resp = _call_require_auth(
        monkeypatch,
        headers={"Authorization": f"Bearer {credential}"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with caplog.at_level(logging.WARNING):
        result = handler()

    assert resp.status == 401
    assert result["success"] is False
    assert [record.getMessage() for record in caplog.records] == [
        "Unauthorized access attempt to /api/private"
    ]
    assert credential not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer unavailable"},
        {"X-API-Key": "unavailable"},
    ],
)
def test_require_auth_reports_api_token_storage_failure_as_json_503(
    monkeypatch,
    headers,
):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(
        verify_token=MagicMock(side_effect=CompanionStorageError("private database detail"))
    )
    handler, req, response = _call_require_auth(
        monkeypatch,
        headers=headers,
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()

    assert response.status == 503
    assert response.headers["Content-Type"] == "application/json"
    assert result == {
        "success": False,
        "error": "Authentication storage unavailable",
    }
    assert req.user is None


def test_require_auth_bearer_jwt_wins_over_token_lookup(monkeypatch):
    jwt_handler = SimpleNamespace(
        verify_jwt=lambda _t, **_kwargs: {"sub": "admin", "client_id": "c1"}
    )
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


def test_require_auth_rejects_multiple_v1_credential_transports(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value=None))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    protected = MagicMock(return_value={"success": True})
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/v1/companions/home/snapshot",
        headers={
            "Authorization": "Bearer operator-jwt",
            "X-API-Key": "device-token",
        },
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )
    handler = require_auth(protected)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        handler()

    assert exc_info.value.status == 400
    assert req.user is None
    protected.assert_not_called()
    jwt_handler.verify_jwt.assert_not_called()
    token_manager.verify_token.assert_not_called()


def test_require_auth_reuses_exact_api_tree_authentication(monkeypatch):
    jwt_handler = SimpleNamespace(
        verify_jwt=MagicMock(return_value={"sub": "admin", "client_id": "c1"})
    )
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/v1/companions/home/snapshot",
        headers={"Authorization": "Bearer good-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    @require_auth
    def handler():
        return {"success": True}

    assert check_auth() is None
    assert handler() == {"success": True}
    assert req.user["auth_type"] == "jwt"
    jwt_handler.verify_jwt.assert_called_once_with("good-jwt")
    token_manager.verify_token.assert_not_called()


def test_require_auth_does_not_trust_plain_request_user(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value=None))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    handler, req, resp = _call_require_auth(
        monkeypatch,
        path="/api/v1/companions/home/snapshot",
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )
    req.user = {
        "username": "forged",
        "auth_type": "jwt",
        "scope": "admin",
    }

    result = handler()

    assert result["success"] is False
    assert resp.status == 401
    jwt_handler.verify_jwt.assert_not_called()
    token_manager.verify_token.assert_not_called()


def test_require_auth_rejects_query_jwt_on_non_sse_http(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value={"sub": "admin"}))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    handler, req, resp = _call_require_auth(
        monkeypatch,
        path="/api/v1/companions/home/messages",
        params={"token": "query-jwt"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()

    assert resp.status == 401
    assert result["success"] is False
    assert "token" not in req.params
    jwt_handler.verify_jwt.assert_not_called()
    token_manager.verify_token.assert_not_called()


def test_require_auth_accepts_api_key_unchanged(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(verify_token=lambda _k: {"id": 3, "name": "svc"})
    handler, req, _resp = _call_require_auth(
        monkeypatch,
        headers={"X-API-Key": "k"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()
    assert result == {"success": True}
    assert req.user["auth_type"] == "api_token"


@pytest.mark.parametrize("api_key", ["has space", "has\tcontrol", "ü", "a" * 4097])
def test_check_auth_rejects_malformed_api_key_before_verification(
    monkeypatch,
    api_key,
):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value=None))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    _set_cp(
        monkeypatch,
        headers={"X-API-Key": api_key},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 401
    token_manager.verify_token.assert_not_called()


@pytest.mark.parametrize("api_key", ["has space", "has\ncontrol", "ü", "a" * 4097])
def test_require_auth_rejects_malformed_api_key_before_verification(
    monkeypatch,
    api_key,
):
    jwt_handler = SimpleNamespace(verify_jwt=MagicMock(return_value=None))
    token_manager = SimpleNamespace(verify_token=MagicMock(return_value=None))
    handler, _req, resp = _call_require_auth(
        monkeypatch,
        headers={"X-API-Key": api_key},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()

    assert resp.status == 401
    assert result["success"] is False
    token_manager.verify_token.assert_not_called()


@pytest.mark.parametrize("transport", ["bearer", "api_key"])
def test_require_auth_direct_decorator_enforces_api_token_path_scope(
    monkeypatch,
    transport,
):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t, **_kwargs: None)
    token_manager = SimpleNamespace(
        verify_token=lambda _k: {
            "id": 9,
            "name": "phone",
            "scope": "companion:home",
        }
    )
    headers = {"Authorization": "Bearer tok"} if transport == "bearer" else {"X-API-Key": "tok"}
    handler, req, resp = _call_require_auth(
        monkeypatch,
        path="/api/companion/send_text",
        headers=headers,
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()

    assert result["success"] is False
    assert resp.status == 403
    assert req.user is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/cli",
        "/api/config_export",
        "/api/auth/tokens",
        "/api/companion/send",
    ],
)
def test_check_auth_confines_companion_tokens_to_v1(monkeypatch, path):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=lambda _k: {"id": 7, "name": "phone", "scope": "companion:home"}
    )
    _set_cp(
        monkeypatch,
        path=path,
        headers={"X-API-Key": "device-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 403


@pytest.mark.parametrize("scope", ["admin", None])
def test_check_auth_preserves_admin_and_legacy_tokens(monkeypatch, scope):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_info = {"id": 1, "name": "operator"}
    if scope is not None:
        token_info["scope"] = scope
    token_manager = SimpleNamespace(verify_token=lambda _k: token_info)
    req, _resp = _set_cp(
        monkeypatch,
        path="/api/config_export",
        headers={"X-API-Key": "operator-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    assert check_auth() is None
    assert req.user["scope"] == "admin"


@pytest.mark.parametrize("scope", ["read", "companion:", "", 1])
def test_check_auth_fails_closed_on_unknown_scopes(monkeypatch, scope):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=lambda _k: {"id": 7, "name": "bad-scope", "scope": scope}
    )
    _set_cp(
        monkeypatch,
        path="/api/v1/companions",
        headers={"X-API-Key": "bad-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/companions/home/events",
        "/api/companion/events",
    ],
)
def test_check_auth_does_not_accept_api_tokens_from_query(monkeypatch, path):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value={"id": 7, "name": "phone", "scope": "companion:home"})
    )
    req, _resp = _set_cp(
        monkeypatch,
        path=path,
        params={"token": "device-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        check_auth()

    assert exc_info.value.status == 401
    assert "token" not in req.params
    token_manager.verify_token.assert_not_called()


def test_require_auth_fails_closed_on_unknown_scope(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_manager = SimpleNamespace(
        verify_token=lambda _k: {"id": 7, "name": "bad-scope", "scope": "read"}
    )
    handler, req, resp = _call_require_auth(
        monkeypatch,
        path="/api/v1/companions",
        headers={"X-API-Key": "bad-token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    result = handler()

    assert result["success"] is False
    assert resp.status == 403
    assert req.user is None


def test_require_admin_accepts_only_admin_scope(monkeypatch):
    jwt_handler = SimpleNamespace(verify_jwt=lambda _t: None)
    token_info = {"id": 7, "name": "token", "scope": "companion:home"}
    token_manager = SimpleNamespace(verify_token=lambda _k: token_info)
    req, resp = _set_cp(
        monkeypatch,
        path="/api/auth/tokens",
        headers={"X-API-Key": "token"},
        cfg={"jwt_handler": jwt_handler, "token_manager": token_manager},
    )

    @require_admin
    def handler():
        return {"success": True}

    result = handler()

    assert result["success"] is False
    assert resp.status == 403
    assert req.user is None


def test_require_admin_options_never_calls_handler(monkeypatch):
    _req, resp = _set_cp(monkeypatch, method="OPTIONS")
    protected = MagicMock(return_value={"success": True})
    handler = require_admin(protected)

    assert handler() is None
    assert resp.status == 204
    protected.assert_not_called()


def test_scope_policy_is_explicit_and_exact():
    assert api_token_scope({"scope": None}) == "admin"
    assert api_token_scope({}) == "admin"
    assert is_known_scope("admin")
    assert is_known_scope("companion:*")
    assert is_known_scope("companion:home")
    assert not is_known_scope("read")
    assert scope_allows_api_path("companion:home", "/api/v1")
    assert scope_allows_api_path("companion:home", "/api/v1/companions/home")
    assert not scope_allows_api_path("companion:home", "/api/v10")
    assert not scope_allows_api_path("companion:home", "/api/companion")
    assert not scope_allows_api_path("companion:home", "/api/v1/../cli")


@pytest.mark.parametrize(
    "scope",
    [
        "companion:",
        "companion:../x",
        "companion:two words",
        "companion:ümlaut",
        f"companion:{'x' * 65}",
    ],
)
def test_scope_policy_rejects_invalid_companion_registration_names(scope):
    assert not is_known_scope(scope)


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "Bearer  token",
        " Bearer token",
        "Bearer token ",
        "Bearer\ttoken",
        "Bearer tok\ten",
        "Bearer tok\nen",
        "Bearer tok en",
        "Bearer töken",
        "Bearer " + ("a" * 4097),
        123,
    ],
)
def test_bearer_scheme_parser_rejects_malformed_whitespace(header):
    assert bearer_token_from_header(header) is None


@pytest.mark.parametrize(
    "token",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.signature_-",
        "a" * 64,
        "abc+/~_-.==",
    ],
)
def test_bearer_scheme_parser_accepts_jwt_and_api_token_syntax(token):
    assert bearer_token_from_header(f"bEaReR {token}") == token
    assert allows_query_jwt("GET", "/api/v1/companions/home/events")
    assert allows_query_jwt("GET", "/api/v1/companions/chat.agent-1/events")
    assert allows_query_jwt("GET", "/api/companion/events")
    assert not allows_query_jwt("POST", "/api/v1/companions/home/events")
    assert not allows_query_jwt("POST", "/api/companion/events")
    assert not allows_query_jwt("GET", "/api/companion/events/")
    assert not allows_query_jwt("GET", "/api/companion/snapshot")
    assert not allows_query_jwt("GET", "/api/v1/companions/home/sync")
