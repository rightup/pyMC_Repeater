"""Authorization-boundary tests for the repeater packet WebSocket."""

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition import websocket_handler as packet_ws
from repeater.data_acquisition.sqlite_handler import CompanionStorageError
from repeater.web.auth import lease as auth_lease
from repeater.web.auth.jwt_handler import JWTHandler

_JWT_SECRET = "test-secret-key-minimum-32-bytes!!"


@pytest.fixture(autouse=True)
def clean_connected_clients():
    packet_ws._connected_clients.clear()
    yield
    packet_ws._connected_clients.clear()


def _websocket(query_string="", api_key=""):
    websocket = object.__new__(packet_ws.PacketWebSocket)
    websocket.environ = {
        "QUERY_STRING": query_string,
        "HTTP_X_API_KEY": api_key,
    }
    websocket.close = MagicMock()
    return websocket


def _configure(monkeypatch, token_info):
    config = {
        "jwt_handler": SimpleNamespace(
            verify_jwt=lambda _token, **_kwargs: None
        ),
        "token_manager": SimpleNamespace(verify_token=lambda _token: token_info),
    }
    monkeypatch.setattr(cherrypy, "config", config, raising=False)


@pytest.mark.parametrize("scope", ["companion:home", "companion:*", "read", ""])
def test_packet_websocket_rejects_non_admin_api_token(monkeypatch, scope):
    _configure(monkeypatch, {"id": 7, "name": "phone", "scope": scope})
    websocket = _websocket(api_key="device-token")

    websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="forbidden")
    assert websocket not in packet_ws._connected_clients


@pytest.mark.parametrize(
    "token_info",
    [
        {"id": 1, "name": "operator", "scope": "admin"},
        {"id": 2, "name": "legacy-operator"},
        {"id": 3, "name": "legacy-null", "scope": None},
    ],
)
def test_packet_websocket_accepts_admin_and_migrated_legacy_tokens(
    monkeypatch, token_info
):
    _configure(monkeypatch, token_info)
    websocket = _websocket(api_key="operator-token")

    websocket.opened()

    websocket.close.assert_not_called()
    assert websocket in packet_ws._connected_clients


def test_packet_websocket_escapes_legacy_token_name_in_identity_and_logs(
    monkeypatch,
    caplog,
):
    raw_name = "legacy\n\u202eoperator"
    _configure(
        monkeypatch,
        {"id": 7, "name": raw_name, "scope": "admin"},
    )
    websocket = _websocket(api_key="operator-token")

    with caplog.at_level("INFO", logger="WebSocket"):
        websocket.opened()

    assert websocket.close.call_count == 0
    assert websocket.user == "api_token:legacy\\u000a\\u202eoperator"
    assert raw_name not in caplog.text
    assert "\u202e" not in caplog.text
    assert "\\\\u000a" in caplog.text
    assert "\\\\u202e" in caplog.text


def test_packet_websocket_reports_api_token_storage_failure(monkeypatch):
    verify_token = MagicMock(
        side_effect=CompanionStorageError("private database detail")
    )
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: None
            ),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(api_key="operator-token")

    websocket.opened()

    websocket.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )
    assert websocket not in packet_ws._connected_clients


def test_packet_websocket_reports_missing_api_token_manager(monkeypatch):
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: None
            )
        },
        raising=False,
    )
    websocket = _websocket(api_key="operator-token")

    websocket.opened()

    websocket.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )
    assert websocket not in packet_ws._connected_clients


def test_packet_websocket_reports_unexpected_api_token_verifier_failure(monkeypatch):
    verify_token = MagicMock(side_effect=RuntimeError("private implementation detail"))
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: None
            ),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(api_key="operator-token")

    websocket.opened()

    websocket.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )
    assert websocket not in packet_ws._connected_clients


def test_packet_websocket_reports_unexpected_jwt_verifier_failure(monkeypatch):
    verify_jwt = MagicMock(side_effect=RuntimeError("private implementation detail"))
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=MagicMock()),
        },
        raising=False,
    )
    websocket = _websocket(query_string="token=operator-jwt")

    websocket.opened()

    websocket.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )
    assert websocket not in packet_ws._connected_clients


@pytest.mark.parametrize(
    "query_string",
    [
        "token=one&token=two",
        "token=one&client_id=first&client_id=second",
        "token=one&unknown=true",
    ],
)
def test_packet_websocket_rejects_ambiguous_or_unknown_query_fields(
    monkeypatch,
    query_string,
):
    verify_jwt = MagicMock(
        return_value={
            "sub": "admin",
            "client_id": "web",
            "exp": time.time() + 3600,
        }
    )
    verify_token = MagicMock()
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(query_string=query_string)

    websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="invalid query")
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()


@pytest.mark.parametrize("query_token", ["operator-jwt", ""])
def test_packet_websocket_rejects_simultaneous_jwt_and_api_key(
    monkeypatch,
    query_token,
):
    verify_jwt = MagicMock()
    verify_token = MagicMock()
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(
        query_string=f"token={query_token}",
        api_key="operator-token",
    )

    websocket.opened()

    websocket.close.assert_called_once_with(
        code=1008,
        reason="ambiguous credentials",
    )
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()


def test_packet_websocket_accepts_operator_jwt(monkeypatch):
    config = {
        "jwt_handler": SimpleNamespace(
            verify_jwt=lambda _token, **_kwargs: {
                "sub": "admin",
                "client_id": "web",
                "exp": time.time() + 3600,
            }
        ),
        "token_manager": SimpleNamespace(verify_token=MagicMock()),
    }
    monkeypatch.setattr(cherrypy, "config", config, raising=False)
    websocket = _websocket(query_string="token=operator-jwt&client_id=web")

    websocket.opened()

    websocket.close.assert_not_called()
    assert websocket in packet_ws._connected_clients
    config["token_manager"].verify_token.assert_not_called()


class _AuthTickStop:
    def __init__(self):
        self.calls = 0

    def wait(self, timeout):
        assert timeout == packet_ws.AUTHORIZATION_RECHECK_SECONDS
        self.calls += 1
        return self.calls > 1


def test_idle_packet_websocket_closes_at_jwt_expiration(monkeypatch):
    clock = {"wall": 1000.0}
    monkeypatch.setattr(auth_lease.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: {
                    "sub": "admin",
                    "client_id": "web",
                    "exp": 1001.0,
                }
            ),
            "token_manager": SimpleNamespace(verify_token=MagicMock()),
        },
        raising=False,
    )
    websocket = _websocket(query_string="token=operator-jwt&client_id=web")
    websocket.opened()
    clock["wall"] = 1002.0

    packet_ws._heartbeat_loop(_AuthTickStop())

    websocket.close.assert_called_once_with(
        code=1008,
        reason="authorization expired or revoked",
    )
    assert websocket not in packet_ws._connected_clients


def test_idle_packet_websocket_closes_after_api_token_revocation(monkeypatch):
    token_info = {"id": 1, "name": "operator", "scope": "admin"}
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value=token_info),
        get_token=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: None
            ),
            "token_manager": token_manager,
        },
        raising=False,
    )
    websocket = _websocket(api_key="operator-token")
    websocket.opened()

    packet_ws._heartbeat_loop(_AuthTickStop())

    websocket.close.assert_called_once_with(
        code=1008,
        reason="authorization expired or revoked",
    )
    assert websocket not in packet_ws._connected_clients


def test_packet_auth_ticks_do_not_change_thirty_second_ping_cadence(
    monkeypatch,
):
    clock = {"monotonic": 0.0}
    monkeypatch.setattr(
        packet_ws.time,
        "monotonic",
        lambda: clock["monotonic"],
    )
    client = MagicMock()
    client._authorization_is_active.return_value = True
    packet_ws._connected_clients.add(client)

    class _TwoTicks:
        calls = 0

        def wait(self, timeout):
            assert timeout == packet_ws.AUTHORIZATION_RECHECK_SECONDS
            self.calls += 1
            clock["monotonic"] += timeout
            return self.calls > 2

    packet_ws._heartbeat_loop(_TwoTicks())

    client.send.assert_called_once_with('{"type": "ping"}')
    assert client._authorization_is_active.call_count == 2


def test_packet_websocket_accepts_legacy_query_admin_api_token(
    monkeypatch,
    caplog,
):
    jwt_handler = JWTHandler(_JWT_SECRET)
    verify_jwt = MagicMock(wraps=jwt_handler.verify_jwt)
    jwt_handler.verify_jwt = verify_jwt
    verify_token = MagicMock(
        return_value={"id": 1, "name": "operator", "scope": "admin"}
    )
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": jwt_handler,
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(query_string="token=long-lived-api-token")

    with caplog.at_level(logging.WARNING):
        websocket.opened()

    websocket.close.assert_not_called()
    assert websocket in packet_ws._connected_clients
    verify_jwt.assert_called_once_with("long-lived-api-token", quiet=True)
    verify_token.assert_called_once_with("long-lived-api-token")
    assert [record.getMessage() for record in caplog.records] == [
        (
            "Packet WebSocket accepted a legacy API token in the token query "
            "parameter; use X-API-Key instead"
        )
    ]
    assert "long-lived-api-token" not in caplog.text


def test_packet_websocket_rejects_legacy_query_device_api_token(monkeypatch):
    verify_jwt = MagicMock(return_value=None)
    verify_token = MagicMock(
        return_value={"id": 1, "name": "phone", "scope": "companion:home"}
    )
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(query_string="token=device-api-token")

    websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="forbidden")
    assert websocket not in packet_ws._connected_clients
    verify_jwt.assert_called_once_with("device-api-token")
    verify_token.assert_called_once_with("device-api-token")


def test_packet_websocket_invalid_query_credential_logs_one_sanitized_failure(
    monkeypatch,
    caplog,
):
    credential = "invalid-operator-credential"
    jwt_handler = JWTHandler(_JWT_SECRET)
    verify_jwt = MagicMock(wraps=jwt_handler.verify_jwt)
    jwt_handler.verify_jwt = verify_jwt
    verify_token = MagicMock(return_value=None)
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": jwt_handler,
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(query_string=f"token={credential}")

    with caplog.at_level(logging.WARNING):
        websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="unauthorized")
    assert websocket not in packet_ws._connected_clients
    verify_jwt.assert_called_once_with(credential, quiet=True)
    verify_token.assert_called_once_with(credential)
    assert [record.getMessage() for record in caplog.records] == [
        "WebSocket connection rejected: no valid authentication"
    ]
    assert credential not in caplog.text


def test_idle_packet_websocket_closes_after_legacy_query_api_token_revocation(
    monkeypatch,
):
    token_info = {"id": 1, "name": "operator", "scope": "admin"}
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value=token_info),
        get_token=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(
                verify_jwt=lambda _token, **_kwargs: None
            ),
            "token_manager": token_manager,
        },
        raising=False,
    )
    websocket = _websocket(query_string="token=legacy-operator-token")
    websocket.opened()

    packet_ws._heartbeat_loop(_AuthTickStop())

    websocket.close.assert_called_once_with(
        code=1008,
        reason="authorization expired or revoked",
    )
    assert websocket not in packet_ws._connected_clients


def test_websocket_shutdown_interrupts_and_joins_idle_heartbeat(monkeypatch):
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=packet_ws._heartbeat_loop,
        args=(stop_event,),
        daemon=True,
    )
    monkeypatch.setattr(packet_ws, "_heartbeat_stop", stop_event)
    monkeypatch.setattr(packet_ws, "_heartbeat_thread", heartbeat)
    monkeypatch.setattr(packet_ws, "_heartbeat_running", True)
    monkeypatch.setattr(packet_ws, "_websocket_plugin", None)
    heartbeat.start()

    packet_ws.shutdown_websocket()

    assert not heartbeat.is_alive()
    assert packet_ws._heartbeat_running is False
    assert packet_ws._heartbeat_thread is None
    assert packet_ws._heartbeat_stop is None


def test_websocket_restart_replaces_a_still_stopping_heartbeat(monkeypatch):
    stale_stop = threading.Event()
    stale_stop.set()
    stale_thread = MagicMock()
    stale_thread.is_alive.return_value = True
    plugin = MagicMock()
    new_thread = MagicMock()
    new_thread.is_alive.return_value = True

    monkeypatch.setattr(packet_ws, "_heartbeat_stop", stale_stop)
    monkeypatch.setattr(packet_ws, "_heartbeat_thread", stale_thread)
    monkeypatch.setattr(packet_ws, "_heartbeat_running", True)
    monkeypatch.setattr(packet_ws, "_websocket_plugin", None)
    monkeypatch.setattr(packet_ws, "WebSocketPlugin", MagicMock(return_value=plugin))
    monkeypatch.setattr(packet_ws, "WebSocketTool", MagicMock())
    monkeypatch.setattr(cherrypy, "tools", SimpleNamespace())
    thread_factory = MagicMock(return_value=new_thread)
    monkeypatch.setattr(packet_ws.threading, "Thread", thread_factory)

    packet_ws.init_websocket()

    assert packet_ws._heartbeat_stop is not stale_stop
    assert not packet_ws._heartbeat_stop.is_set()
    assert packet_ws._heartbeat_thread is new_thread
    new_thread.start.assert_called_once_with()
    thread_factory.assert_called_once_with(
        target=packet_ws._heartbeat_loop,
        args=(packet_ws._heartbeat_stop,),
        daemon=True,
    )


@pytest.mark.parametrize(
    ("query_string", "api_key"),
    [
        ("token=has%20space", ""),
        ("token=has%09control", ""),
        ("token=" + ("a" * 4097), ""),
        ("", "has space"),
        ("", "has\ncontrol"),
        ("", "a" * 4097),
    ],
)
def test_packet_websocket_rejects_malformed_credentials_before_verification(
    monkeypatch,
    query_string,
    api_key,
):
    verify_jwt = MagicMock(return_value=None)
    verify_token = MagicMock(return_value=None)
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(query_string=query_string, api_key=api_key)

    websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="unauthorized")
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()


@pytest.mark.parametrize(
    "client_id",
    [
        "%20%20",
        "has%09control",
        "has%1Bescape",
        "has%C2%85control",
        "has%E2%80%8Bcontrol",
        "has%E2%80%A8control",
        "has%E2%80%AEcontrol",
        "a" * 129,
    ],
)
def test_packet_websocket_rejects_invalid_client_id_before_verification(
    monkeypatch,
    client_id,
):
    verify_jwt = MagicMock(return_value={"sub": "admin", "client_id": "web"})
    verify_token = MagicMock(return_value=None)
    monkeypatch.setattr(
        cherrypy,
        "config",
        {
            "jwt_handler": SimpleNamespace(verify_jwt=verify_jwt),
            "token_manager": SimpleNamespace(verify_token=verify_token),
        },
        raising=False,
    )
    websocket = _websocket(
        query_string=f"token=operator-jwt&client_id={client_id}"
    )

    websocket.opened()

    websocket.close.assert_called_once_with(code=1008, reason="unauthorized")
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()
