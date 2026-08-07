import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import CompanionStorageError
from repeater.web.auth_endpoints import (
    AuthAPIEndpoints,
    AuthEndpoints,
    TokensAPIEndpoint,
    _LoginThrottle,
)


@pytest.fixture
def cp_ctx(monkeypatch):
    def _set(method="GET", headers=None, body=b"", path="/api/auth"):
        request_headers = dict(headers or {})
        if method in {"POST", "PUT", "PATCH"}:
            request_headers.setdefault("Content-Type", "application/json")
        req = SimpleNamespace(
            method=method,
            headers=request_headers,
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
    assert endpoint.index() is None
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
    assert cherrypy.response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "name",
    (
        "build\nbot",
        "build\x7fbot",
        "\tbuild-bot",
        "build-bot\n",
        "build\u0085bot",
        "build\u2028bot",
        "build\u202ebot",
        "build\u200bbot",
    ),
)
def test_token_creation_rejects_control_characters(cp_ctx, name):
    endpoint = TokensAPIEndpoint()
    manager = _token_mgr()
    manager.create_token = MagicMock(return_value=(3, "plain-token"))
    _req, response, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"name": name}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = manager

    result = endpoint.index()

    assert result["success"] is False
    assert "control characters" in result["error"]
    assert response.status == 400
    manager.create_token.assert_not_called()


@pytest.mark.parametrize("operation", ["list", "create", "revoke"])
def test_token_management_storage_failure_is_generic_503(
    cp_ctx,
    operation,
):
    endpoint = TokensAPIEndpoint()

    def unavailable(*_args, **_kwargs):
        raise CompanionStorageError("private database detail")

    manager = SimpleNamespace(
        list_tokens=unavailable,
        create_token=unavailable,
        revoke_token=unavailable,
    )
    method = {"list": "GET", "create": "POST", "revoke": "DELETE"}[operation]
    body = json.dumps({"name": "build-bot"}).encode() if operation == "create" else b""
    _req, response, cfg = cp_ctx(
        method=method,
        headers={"Authorization": "Bearer ok"},
        body=body,
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = manager

    result = endpoint.default(token_id="1") if operation == "revoke" else endpoint.index()

    assert result == {
        "success": False,
        "error": "Authentication storage unavailable",
    }
    assert response.status == 503
    assert "private database detail" not in result["error"]


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


@pytest.mark.parametrize("token_id", ["0", "-1", str(1 << 63)])
def test_token_revoke_id_is_bounded_to_sqlite_integer_range(cp_ctx, token_id):
    endpoint = TokensAPIEndpoint()
    _req, response, cfg = cp_ctx(
        method="DELETE",
        headers={"Authorization": "Bearer ok"},
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    revoke_token = MagicMock(return_value=False)
    cfg["token_manager"] = SimpleNamespace(revoke_token=revoke_token)

    result = endpoint.default(token_id=token_id)

    assert result["success"] is False
    assert response.status == 400
    revoke_token.assert_not_called()


def test_token_revoke_accepts_sqlite_integer_max(cp_ctx):
    endpoint = TokensAPIEndpoint()
    _req, response, cfg = cp_ctx(
        method="DELETE",
        headers={"Authorization": "Bearer ok"},
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    maximum = (1 << 63) - 1
    revoke_token = MagicMock(return_value=False)
    cfg["token_manager"] = SimpleNamespace(revoke_token=revoke_token)

    result = endpoint.default(token_id=str(maximum))

    assert result["success"] is False
    assert response.status == 404
    revoke_token.assert_called_once_with(maximum)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_token_management_rejects_companion_scope(cp_ctx, method):
    endpoint = TokensAPIEndpoint()
    manager = _token_mgr()
    manager.verify_token = lambda _key: {
        "id": 7,
        "name": "phone",
        "scope": "companion:home",
    }
    body = json.dumps({"name": "new-admin-token"}).encode() if method == "POST" else b""
    _req, resp, cfg = cp_ctx(
        method=method,
        path="/api/auth/tokens",
        headers={"X-API-Key": "device-token"},
        body=body,
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = manager

    out = endpoint.index()

    assert out["success"] is False
    assert resp.status == 403


def test_token_revocation_rejects_companion_scope(cp_ctx):
    endpoint = TokensAPIEndpoint()
    revoke = MagicMock(return_value=True)
    manager = SimpleNamespace(
        verify_token=lambda _key: {
            "id": 7,
            "name": "phone",
            "scope": "companion:home",
        },
        revoke_token=revoke,
    )
    _req, resp, cfg = cp_ctx(
        method="DELETE",
        path="/api/auth/tokens/1",
        headers={"X-API-Key": "device-token"},
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = manager

    out = endpoint.default(token_id="1")

    assert out["success"] is False
    assert resp.status == 403
    revoke.assert_not_called()


def test_token_management_accepts_legacy_scope_less_admin_token(cp_ctx):
    endpoint = TokensAPIEndpoint()
    manager = _token_mgr()  # Missing scope is the explicit legacy-admin migration case.
    _req, _resp, cfg = cp_ctx(
        method="GET",
        path="/api/auth/tokens",
        headers={"X-API-Key": "legacy-admin-token"},
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = manager

    out = endpoint.index()

    assert out["success"] is True


def test_login_paths(cp_ctx):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )

    cp_ctx(method="OPTIONS")
    assert auth.login() == b""
    assert cherrypy.response.headers["Cache-Control"] == "no-store"

    cp_ctx(method="POST", body=b"{}")
    out = json.loads(auth.login().decode())
    assert out["success"] is False


def test_login_rejects_cross_origin_simple_content_type(cp_ctx):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )
    cp_ctx(
        method="POST",
        headers={"Content-Type": "text/plain"},
        body=json.dumps({"username": "admin", "password": "pw", "client_id": "browser"}).encode(),
    )

    result = json.loads(auth.login().decode())

    assert result["success"] is False
    assert cherrypy.response.status == 415
    assert "application/json" in result["error"]


@pytest.mark.parametrize("configured_password", [None, "", "admin123", 123])
def test_login_never_issues_admin_jwt_for_unconfigured_password(
    cp_ctx,
    configured_password,
):
    jwt_handler = _jwt_handler(ok=True)
    jwt_handler.create_jwt = MagicMock(return_value="unsafe-jwt")
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": configured_password}}},
        jwt_handler=jwt_handler,
        token_manager=_token_mgr(),
    )
    cp_ctx(
        method="POST",
        body=json.dumps(
            {
                "username": "admin",
                "password": "admin123",
                "client_id": "first-boot-peer",
            }
        ).encode(),
    )

    result = json.loads(auth.login().decode())

    assert result == {
        "success": False,
        "error": "System not configured. Please complete setup wizard.",
    }
    assert cherrypy.response.status == 409
    jwt_handler.create_jwt.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("username", "admin\nforged"),
        ("username", "admin\x7f"),
        ("client_id", "chat\tinjected"),
        ("client_id", "chat\x1b"),
    ],
)
def test_login_rejects_control_characters(cp_ctx, field, value):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "pw"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )
    body = {"username": "admin", "password": "pw", "client_id": "chat"}
    body[field] = value
    _req, resp, _cfg = cp_ctx(method="POST", body=json.dumps(body).encode())

    out = json.loads(auth.login().decode())

    assert out["success"] is False
    assert resp.status == 400
    assert "control characters" in out["error"]


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


def test_login_throttle_blocked_reads_do_not_allocate_and_expired_keys_prune():
    now = [1000.0]
    throttle = _LoginThrottle(
        per_ip_threshold=99,
        per_user_threshold=99,
        global_threshold=1,
        base_backoff_sec=10,
        max_backoff_sec=10,
        window_sec=60,
        time_fn=lambda: now[0],
    )
    assert throttle.register_failure("203.0.113.1", "admin") == 10
    initial_ip_count = len(throttle._ip_states)
    initial_user_count = len(throttle._user_states)

    for index in range(2000):
        assert (
            throttle.get_retry_after(
                f"198.51.100.{index}",
                f"fresh-user-{index}",
            )
            > 0
        )

    assert len(throttle._ip_states) == initial_ip_count
    assert len(throttle._user_states) == initial_user_count

    now[0] += 61
    assert throttle.get_retry_after("192.0.2.1", "fresh") == 0
    assert throttle._ip_states == {}
    assert throttle._user_states == {}


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
    assert cherrypy.response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("client_id", ["chat\nforged", "chat\x7f"])
def test_refresh_rejects_client_id_controls(cp_ctx, client_id):
    auth = AuthEndpoints(
        config={},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
    )
    _req, resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps({"client_id": client_id}).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()

    out = json.loads(auth.refresh().decode())

    assert out["success"] is False
    assert resp.status == 400
    assert "control characters" in out["error"]


@pytest.mark.parametrize(
    ("endpoint_name", "body", "operation"),
    [
        ("refresh", {"client_id": "chat"}, "token refresh"),
        (
            "change_password",
            {
                "current_password": "old-password",
                "new_password": "new-password",
            },
            "password change",
        ),
    ],
)
def test_manual_auth_storage_failure_returns_generic_503(
    cp_ctx,
    caplog,
    endpoint_name,
    body,
    operation,
):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=False),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=True)),
    )
    _req, response, cfg = cp_ctx(
        method="POST",
        headers={"X-API-Key": "device-token"},
        body=json.dumps(body).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)

    def fail_lookup(_token):
        raise CompanionStorageError("private database detail")

    cfg["token_manager"] = SimpleNamespace(verify_token=fail_lookup)

    result = json.loads(getattr(auth, endpoint_name)().decode())

    assert result == {
        "success": False,
        "error": "Authentication storage unavailable",
    }
    assert response.status == 503
    assert "private database detail" not in result["error"]
    assert operation in caplog.text


@pytest.mark.parametrize("endpoint_name", ["refresh", "change_password"])
@pytest.mark.parametrize("api_key", ["has space", "has\tcontrol", "ü", "a" * 4097])
def test_manual_auth_rejects_malformed_api_key_before_verification(
    cp_ctx,
    endpoint_name,
    api_key,
):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=False),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=True)),
    )
    body = (
        {"client_id": "client"}
        if endpoint_name == "refresh"
        else {
            "current_password": "old-password",
            "new_password": "new-password",
        }
    )
    _req, resp, cfg = cp_ctx(
        method="POST",
        headers={"X-API-Key": api_key},
        body=json.dumps(body).encode(),
    )
    verify_token = MagicMock(return_value=None)
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)

    result = json.loads(getattr(auth, endpoint_name)().decode())

    assert result["success"] is False
    assert resp.status == 401
    verify_token.assert_not_called()


@pytest.mark.parametrize("endpoint_name", ["refresh", "change_password"])
@pytest.mark.parametrize(
    ("bearer_token", "api_key", "valid_token", "expected_calls"),
    [
        ("admin-bearer", "stale-x-key", "admin-bearer", ["admin-bearer"]),
        (
            "stale-bearer",
            "admin-x-key",
            "admin-x-key",
            ["stale-bearer", "admin-x-key"],
        ),
    ],
)
def test_manual_auth_uses_middleware_credential_order_and_fallback(
    cp_ctx,
    endpoint_name,
    bearer_token,
    api_key,
    valid_token,
    expected_calls,
):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=False),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=True)),
    )
    body = (
        {"client_id": "client"}
        if endpoint_name == "refresh"
        else {
            "current_password": "old-password",
            "new_password": "new-password",
        }
    )
    _req, response, cfg = cp_ctx(
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "X-API-Key": api_key,
        },
        body=json.dumps(body).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    calls = []

    def verify_token(token):
        calls.append(token)
        if token != valid_token:
            return None
        return {"id": 7, "name": "operator", "scope": "admin"}

    cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)

    result = json.loads(getattr(auth, endpoint_name)().decode())

    assert result["success"] is True
    assert response.status == 200
    assert calls == expected_calls


@pytest.mark.parametrize("transport", ["x-api-key", "bearer"])
def test_refresh_rejects_companion_and_unknown_token_scopes(cp_ctx, transport):
    auth = AuthEndpoints(config={}, jwt_handler=_jwt_handler(ok=True), token_manager=_token_mgr())

    for scope in ("companion:home", "read"):
        headers = (
            {"X-API-Key": "device-token"}
            if transport == "x-api-key"
            else {"Authorization": "Bearer device-token"}
        )
        _req, resp, cfg = cp_ctx(
            method="POST",
            path="/auth/refresh",
            headers=headers,
            body=json.dumps({"client_id": "phone"}).encode(),
        )
        cfg["jwt_handler"] = _jwt_handler(ok=False)
        cfg["token_manager"] = SimpleNamespace(
            verify_token=lambda _key, current_scope=scope: {
                "id": 7,
                "name": "phone",
                "scope": current_scope,
            }
        )

        out = json.loads(auth.refresh().decode())

        assert out["success"] is False
        assert resp.status == 403


def test_verify_rejects_companion_scope(cp_ctx):
    auth = AuthEndpoints(config={}, jwt_handler=_jwt_handler(ok=True), token_manager=_token_mgr())
    _req, resp, cfg = cp_ctx(
        method="GET",
        path="/auth/verify",
        headers={"X-API-Key": "device-token"},
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = SimpleNamespace(
        verify_token=lambda _key: {
            "id": 7,
            "name": "phone",
            "scope": "companion:home",
        }
    )

    out = auth.verify()

    assert out["success"] is False
    assert resp.status == 403


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

    # historical public default cannot be restored
    _req, _resp, cfg = cp_ctx(
        method="POST",
        headers={"Authorization": "Bearer ok"},
        body=json.dumps(
            {
                "current_password": "old-password",
                "new_password": " admin123 ",
            }
        ).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=True)
    cfg["token_manager"] = _token_mgr()
    out = json.loads(auth.change_password().decode())
    assert out["success"] is False
    assert "default admin123" in out["error"]
    assert cherrypy.response.status == 400
    assert config["repeater"]["security"]["admin_password"] == "old-password"

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
    assert auth_fail_save.config["repeater"]["security"]["admin_password"] == "old-password"

    # an unexpected persistence exception also rolls live state back
    auth_raise_save = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(side_effect=OSError("read-only"))),
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
    out = json.loads(auth_raise_save.change_password().decode())
    assert out["success"] is False
    assert cherrypy.response.status == 500
    assert auth_raise_save.config["repeater"]["security"]["admin_password"] == "old-password"


@pytest.mark.parametrize("scope", ["companion:home", "read"])
def test_change_password_rejects_non_admin_scope(cp_ctx, scope):
    auth = AuthEndpoints(
        config={"repeater": {"security": {"admin_password": "old-password"}}},
        jwt_handler=_jwt_handler(ok=True),
        token_manager=_token_mgr(),
        config_manager=SimpleNamespace(save_to_file=MagicMock(return_value=True)),
    )
    _req, resp, cfg = cp_ctx(
        method="POST",
        path="/auth/change_password",
        headers={"X-API-Key": "device-token"},
        body=json.dumps(
            {"current_password": "old-password", "new_password": "new-password"}
        ).encode(),
    )
    cfg["jwt_handler"] = _jwt_handler(ok=False)
    cfg["token_manager"] = SimpleNamespace(
        verify_token=lambda _key: {
            "id": 7,
            "name": "phone",
            "scope": scope,
        }
    )

    out = json.loads(auth.change_password().decode())

    assert out["success"] is False
    assert resp.status == 403


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
