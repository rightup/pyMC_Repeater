import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.web.auth_endpoints import (
    AuthAPIEndpoints,
    AuthEndpoints,
    TokensAPIEndpoint,
    _LoginThrottle,
)


@pytest.fixture
def cp_ctx(monkeypatch):
    def _set(method="GET", headers=None, body=b"", path="/api/auth"):
        req = SimpleNamespace(
            method=method,
            headers=headers or {},
            body=io.BytesIO(body),
            path_info=path,
            user=None,
        )
        resp = SimpleNamespace(status=200, headers={})
        cfg = {}
        monkeypatch.setattr(cherrypy, "request", req, raising=False)
        monkeypatch.setattr(cherrypy, "response", resp, raising=False)
        monkeypatch.setattr(cherrypy, "config", cfg, raising=False)
        return req, resp, cfg

    return _set


def _jwt_ok_payload():
    return {"sub": "admin", "client_id": "cli-1"}


def _jwt_handler(ok=True):
    if ok:
        return SimpleNamespace(
            verify_jwt=lambda _token: _jwt_ok_payload(),
            create_jwt=lambda u, c: "jwt-new",
            expiry_minutes=15,
        )
    return SimpleNamespace(
        verify_jwt=lambda _token: None, create_jwt=lambda u, c: "jwt-new", expiry_minutes=15
    )


def _token_mgr():
    return SimpleNamespace(
        verify_token=lambda _k: {"id": 7, "name": "tok"},
        list_tokens=lambda: [{"id": 1, "name": "a"}],
        create_token=lambda name: (3, "plain-token"),
        revoke_token=lambda _id: True,
    )


def test_auth_api_endpoints_constructs_tokens_endpoint():
    api = AuthAPIEndpoints()
    assert isinstance(api.tokens, TokensAPIEndpoint)


def test_tokens_index_options_and_missing_manager(cp_ctx):
    endpoint = TokensAPIEndpoint()

    _req, response, _cfg = cp_ctx(method="OPTIONS")
    assert endpoint.index() == b""
    assert response.status == 204

    cp_ctx(method="GET", headers={"Authorization": "Bearer x"})
    with pytest.raises(cherrypy.HTTPError):
        endpoint.index()


def test_tokens_index_get_post_and_error_paths(cp_ctx):
    endpoint = TokensAPIEndpoint()

    # Authenticated GET success
    _req, _resp, cfg = cp_ctx(method="GET", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.index()
    assert out["success"] is True
    assert out["tokens"][0]["id"] == 1

    # GET exception
    _req, _resp, cfg = cp_ctx(method="GET", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = SimpleNamespace(
        list_tokens=lambda: (_ for _ in ()).throw(RuntimeError("db"))
    )
    out = endpoint.index()
    assert out["success"] is False
    assert cherrypy.response.status == 500

    # POST missing name
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"name": ""}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.index()
    assert out["success"] is False
    assert cherrypy.response.status == 400

    # POST success
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"name": "build-bot"}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.index()
    assert out["success"] is True
    assert out["token"] == "plain-token"


def test_tokens_default_delete_paths(cp_ctx):
    endpoint = TokensAPIEndpoint()

    # Missing token_id
    _req, _resp, cfg = cp_ctx(method="DELETE", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.default(token_id=None)
    assert out["success"] is False
    assert cherrypy.response.status == 400

    # Invalid token id
    _req, _resp, cfg = cp_ctx(method="DELETE", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.default(token_id="abc")
    assert out["success"] is False
    assert cherrypy.response.status == 400

    # Not found
    _req, _resp, cfg = cp_ctx(method="DELETE", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = SimpleNamespace(revoke_token=lambda _id: False)
    out = endpoint.default(token_id="9")
    assert out["success"] is False
    assert cherrypy.response.status == 404

    # Success
    _req, _resp, cfg = cp_ctx(method="DELETE", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = endpoint.default(token_id="9")
    assert out["success"] is True


def test_login_paths(cp_ctx):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )

    cp_ctx(method="OPTIONS")
    assert auth.login() == b""

    cp_ctx(method="POST", body=b"{}")
    out = json.loads(auth.login().decode())
    assert out["success"] is False


def test_login_throttle_backoff(cp_ctx):
    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    throttle = _LoginThrottle(
        per_ip_threshold=1,
        per_user_threshold=1,
        global_threshold=99,
        base_backoff_sec=10,
        max_backoff_sec=10,
        time_fn=clock.monotonic,
    )

    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
        login_throttle=throttle,
    )

    cp_ctx(
        method="POST",
        headers={"X-Forwarded-For": "203.0.113.5"},
        body=json.dumps({"username": "admin", "password": "bad", "client_id": "abc"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is False
    assert "retry_after" in out
    assert cherrypy.response.status == 429

    # Still blocked immediately afterwards.
    cp_ctx(
        method="POST",
        headers={"X-Forwarded-For": "203.0.113.5"},
        body=json.dumps({"username": "admin", "password": "pw", "client_id": "abc"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 429

    # After backoff expires, correct credentials work.
    clock.now += 11
    cp_ctx(
        method="POST",
        headers={"X-Forwarded-For": "203.0.113.5"},
        body=json.dumps({"username": "admin", "password": "pw", "client_id": "abc"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is True

    cp_ctx(
        method="POST",
        body=json.dumps({"username": "admin", "password": "pw", "client_id": "abc"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is True
    assert out["token"] == "jwt-new"

    cp_ctx(
        method="POST",
        body=json.dumps({"username": "admin", "password": "bad", "client_id": "abc"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is False


@pytest.mark.asyncio
async def test_verify_requires_get_and_auth(cp_ctx):
    auth = AuthEndpoints(config={}, jwt_handler=_jwt_handler(ok=True), token_manager=_token_mgr())

    _req, _resp, cfg = cp_ctx(method="GET", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = auth.verify()
    assert out["success"] is True

    _req, _resp, cfg = cp_ctx(method="POST", headers={"Authorization": "Bearer ok"})
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    with pytest.raises(cherrypy.HTTPError):
        auth.verify()


def test_refresh_paths(cp_ctx):
    auth = AuthEndpoints(config={}, jwt_handler=_jwt_handler(ok=True), token_manager=_token_mgr())

    cp_ctx(method="OPTIONS")
    assert auth.refresh() == b""

    # unauthorized
    _req, _resp, cfg = cp_ctx(method="POST", body=b"{}")
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = SimpleNamespace(verify_token=lambda _k: None)
    out = json.loads(auth.refresh().decode())
    assert out["success"] is False

    # missing client id
    _req, _resp, cfg = cp_ctx(method="POST", headers={"Authorization": "Bearer ok"}, body=b"{}")
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.refresh().decode())
    assert out["success"] is True  # falls back to payload client_id

    # api token path
    _req, _resp, cfg = cp_ctx(
        method="POST", headers={"X-API-Key": "k"}, body=json.dumps({"client_id": "z"}).encode()
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.refresh().decode())
    assert out["success"] is True


def test_change_password_paths(cp_ctx):
    config = {"repeater": {"security": {"admin_password": "old-password"}}}
    auth = AuthEndpoints(
        config=config,
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=True)),
    )

    cp_ctx(method="OPTIONS")
    assert auth.change_password() == b""

    # no auth handlers configured in cherrypy config
    cp_ctx(method="POST", headers={})
    with pytest.raises(cherrypy.HTTPError):
        auth.change_password()

    # unauthorized
    _req, _resp, cfg = cp_ctx(method="POST", headers={}, body=b"{}")
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = SimpleNamespace(verify_token=lambda _k: None)
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 401

    # missing fields
    _req, _resp, cfg = cp_ctx(method="POST", headers={"Authorization": "Bearer ok"}, body=b"{}")
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 400

    # weak new password
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"current_password": "old-password", "new_password": "short"}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 400

    # wrong current password
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"current_password": "wrong", "new_password": "new-password"}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 401

    # success
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps(
            {"current_password": "old-password", "new_password": "new-password"}
        ).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.change_password().decode())
    assert out["success"] is True

    # save fails
    auth_fail_save = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=False)),
    )
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps(
            {"current_password": "old-password", "new_password": "new-password"}
        ).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth_fail_save.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 500


def test_protected_auth_urls_block_unauthenticated_access(cp_ctx):
    auth = AuthEndpoints(config={}, jwt_handler=_jwt_handler(ok=True), token_manager=_token_mgr())
    no_auth_cfg = {
        "jwt_handler": _jwt_handler(ok=False),
        "token_manager": SimpleNamespace(verify_token=lambda _k: None),
    }

    # /api/auth/tokens requires auth
    endpoint = TokensAPIEndpoint()
    _req, _resp, cfg = cp_ctx(method="GET", path="/api/auth/tokens", headers={})
    cfg.update(no_auth_cfg)
    out = endpoint.index()
    assert out["success"] is False
    assert cherrypy.response.status == 401

    # /api/auth/tokens/<id> requires auth
    _req, _resp, cfg = cp_ctx(method="DELETE", path="/api/auth/tokens/1", headers={})
    cfg.update(no_auth_cfg)
    out = endpoint.default(token_id="1")
    assert out["success"] is False
    assert cherrypy.response.status == 401

    # /api/auth/verify requires auth
    _req, _resp, cfg = cp_ctx(method="GET", path="/api/auth/verify", headers={})
    cfg.update(no_auth_cfg)
    out = auth.verify()
    assert out["success"] is False
    assert cherrypy.response.status == 401

    # /api/auth/change_password requires auth
    _req, _resp, cfg = cp_ctx(
        method="POST",
        path="/api/auth/change_password",
        headers={},
        body=json.dumps({"current_password": "x", "new_password": "new-password"}).encode(),
    )
    cfg.update(no_auth_cfg)
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 401


def test_public_and_restricted_auth_url_methods(cp_ctx):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )

    # /api/auth/login is public but only for POST/OPTIONS.
    cp_ctx(method="GET", path="/api/auth/login")
    with pytest.raises(cherrypy.HTTPError):
        auth.login()

    cp_ctx(
        method="POST",
        path="/api/auth/login",
        body=json.dumps({"username": "admin", "password": "pw", "client_id": "client-a"}).encode(),
    )
    out = json.loads(auth.login().decode())
    assert out["success"] is True

    # /api/auth/refresh is not publicly readable.
    cp_ctx(method="GET", path="/api/auth/refresh")
    with pytest.raises(cherrypy.HTTPError):
        auth.refresh()
