from types import SimpleNamespace
import time
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import CompanionStorageError
from repeater.web import companion_ws_proxy as proxy
from repeater.web.auth import lease as auth_lease


def _jwt_payload():
    return {"sub": "u", "exp": time.time() + 3600}


@pytest.fixture
def cp_cfg(monkeypatch):
    cfg = {}
    monkeypatch.setattr(cherrypy, "config", cfg, raising=False)
    return cfg


def _ws(query_string, api_key=""):
    ws = object.__new__(proxy.CompanionFrameWebSocket)
    ws.environ = {
        "QUERY_STRING": query_string,
        "HTTP_X_API_KEY": api_key,
    }
    ws.close = MagicMock()
    ws.send = MagicMock()
    ws._teardown = MagicMock()
    return ws


def _allow_tcp_connection(ws, monkeypatch):
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))
    fake_socket = MagicMock()
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        MagicMock(return_value=fake_socket),
    )
    thread = MagicMock()
    monkeypatch.setattr(
        proxy.threading,
        "Thread",
        MagicMock(return_value=thread),
    )
    return fake_socket, thread


def test_opened_rejects_missing_jwt_handler(cp_cfg):
    ws = _ws("token=t&companion_name=c1")
    ws.opened()
    ws.close.assert_called_once_with(code=1011, reason="server configuration error")


def test_opened_rejects_missing_token(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: {"sub": "u"})
    ws = _ws("companion_name=c1")
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="unauthorized")


def test_opened_rejects_invalid_token(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: None)
    ws = _ws("token=t&companion_name=c1")
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="unauthorized")


@pytest.mark.parametrize(
    "query_string",
    [
        "token=one&token=two&companion_name=c1",
        "token=one&companion_name=c1&companion_name=c2",
        "token=one&companion_name=c1&debug=true",
    ],
)
def test_opened_rejects_ambiguous_or_unknown_query_fields_before_auth(
    cp_cfg,
    query_string,
):
    verify_jwt = MagicMock(return_value=_jwt_payload())
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=verify_jwt)
    ws = _ws(query_string)

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="invalid query")
    verify_jwt.assert_not_called()


@pytest.mark.parametrize("query_token", ["operator-jwt", ""])
def test_opened_rejects_simultaneous_jwt_and_api_key(cp_cfg, query_token):
    verify_jwt = MagicMock(return_value=_jwt_payload())
    verify_token = MagicMock(return_value={"id": 1, "name": "operator", "scope": "admin"})
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=verify_jwt)
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws(
        f"token={query_token}&companion_name=c1",
        api_key="operator-token",
    )

    ws.opened()

    ws.close.assert_called_once_with(
        code=1008,
        reason="ambiguous credentials",
    )
    verify_jwt.assert_not_called()
    verify_token.assert_not_called()


@pytest.mark.parametrize("token", ["has space", "has%09tab", "ü", "a" * 4097])
def test_opened_rejects_malformed_token_before_verification(cp_cfg, token):
    verify_jwt = MagicMock(return_value={"sub": "u"})
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=verify_jwt)
    ws = _ws(f"token={token}&companion_name=c1")

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="unauthorized")
    verify_jwt.assert_not_called()


def test_opened_rejects_companion_api_token(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: None)
    cp_cfg["token_manager"] = SimpleNamespace(
        verify_token=MagicMock(return_value={"id": 7, "name": "phone", "scope": "companion:home"})
    )
    ws = _ws("token=device-token&companion_name=c1")

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="unauthorized")
    # The frame proxy intentionally accepts only operator JWTs.  It must not
    # reinterpret a failed JWT as a long-lived device API token.
    cp_cfg["token_manager"].verify_token.assert_not_called()


@pytest.mark.parametrize("scope", ["companion:home", "companion:*", "read", ""])
def test_opened_rejects_non_admin_api_key(cp_cfg, scope):
    verify_token = MagicMock(return_value={"id": 7, "name": "phone", "scope": scope})
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws("companion_name=c1", api_key="device-token")

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="forbidden")
    verify_token.assert_called_once_with("device-token")


@pytest.mark.parametrize(
    "token_info",
    [
        {"id": 1, "name": "operator", "scope": "admin"},
        {"id": 2, "name": "legacy-operator"},
        {"id": 3, "name": "legacy-null", "scope": None},
    ],
)
def test_opened_accepts_admin_and_migrated_legacy_api_keys(
    cp_cfg,
    monkeypatch,
    token_info,
):
    verify_jwt = MagicMock()
    verify_token = MagicMock(return_value=token_info)
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=verify_jwt)
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws("companion_name=c1", api_key="operator-token")
    fake_socket, thread = _allow_tcp_connection(ws, monkeypatch)

    ws.opened()

    ws.close.assert_not_called()
    verify_jwt.assert_not_called()
    verify_token.assert_called_once_with("operator-token")
    fake_socket.settimeout.assert_not_called()
    thread.start.assert_called_once_with()


@pytest.mark.parametrize("api_key", ["has space", "has\ttab", "ü", "a" * 4097])
def test_opened_rejects_malformed_api_key_before_verification(cp_cfg, api_key):
    verify_token = MagicMock(return_value={"scope": "admin"})
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws("companion_name=c1", api_key=api_key)

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="unauthorized")
    verify_token.assert_not_called()


def test_opened_reports_missing_api_token_manager(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    ws = _ws("companion_name=c1", api_key="operator-token")

    ws.opened()

    ws.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )


def test_opened_reports_api_token_storage_failure(cp_cfg):
    verify_token = MagicMock(side_effect=CompanionStorageError("private database detail"))
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws("companion_name=c1", api_key="operator-token")

    ws.opened()

    ws.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )


def test_opened_reports_unexpected_api_token_verifier_failure(cp_cfg):
    verify_token = MagicMock(side_effect=RuntimeError("private implementation detail"))
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = SimpleNamespace(verify_token=verify_token)
    ws = _ws("companion_name=c1", api_key="operator-token")

    ws.opened()

    ws.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )


def test_opened_reports_unexpected_jwt_verifier_failure(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(
        verify_jwt=MagicMock(side_effect=RuntimeError("private implementation detail"))
    )
    ws = _ws("token=operator-jwt&companion_name=c1")

    ws.opened()

    ws.close.assert_called_once_with(
        code=1011,
        reason="authentication unavailable",
    )


def test_opened_escapes_api_token_name_in_logs(
    cp_cfg,
    monkeypatch,
    caplog,
):
    raw_name = "legacy\n\u202eoperator"
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = SimpleNamespace(
        verify_token=MagicMock(return_value={"id": 7, "name": raw_name, "scope": "admin"})
    )
    ws = _ws("companion_name=c1", api_key="operator-token")
    _allow_tcp_connection(ws, monkeypatch)

    with caplog.at_level("INFO", logger="CompanionWSProxy"):
        ws.opened()

    assert ws.close.call_count == 0
    assert raw_name not in caplog.text
    assert "\u202e" not in caplog.text
    assert "\\\\u000a" in caplog.text
    assert "\\\\u202e" in caplog.text


def test_opened_rejects_missing_companion_name(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: _jwt_payload())
    ws = _ws("token=t")
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="missing companion_name")


@pytest.mark.parametrize(
    "companion_name",
    ["../admin", "two%20words", "%C3%BCmlaut", "a" * 65],
)
def test_opened_rejects_invalid_companion_name_before_resolution(
    cp_cfg,
    companion_name,
):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: _jwt_payload())
    ws = _ws(f"token=t&companion_name={companion_name}")
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))

    ws.opened()

    ws.close.assert_called_once_with(code=1008, reason="invalid companion_name")
    ws._resolve_tcp_endpoint.assert_not_called()


def test_opened_rejects_missing_companion_endpoint(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: _jwt_payload())
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=None)
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="companion not found")


def test_opened_tcp_connect_failure(cp_cfg, monkeypatch):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: _jwt_payload())
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))

    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        MagicMock(side_effect=RuntimeError("nope")),
    )

    ws.opened()
    ws.close.assert_called_once_with(code=1011, reason="TCP connect failed")


def test_opened_success_starts_reader_thread(cp_cfg, monkeypatch):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: _jwt_payload())
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))

    fake_socket = MagicMock()
    connect = MagicMock(return_value=fake_socket)
    monkeypatch.setattr(proxy.socket, "create_connection", connect)

    thread_started = {"started": False}

    class _T:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            thread_started["started"] = True

    monkeypatch.setattr(proxy.threading, "Thread", _T)

    ws.opened()
    assert ws._closing is False
    assert ws._companion_name == "c1"
    assert thread_started["started"] is True
    connect.assert_called_once_with(("127.0.0.1", 5000), timeout=5.0)
    fake_socket.settimeout.assert_not_called()


def test_idle_proxy_closes_when_its_jwt_expires(cp_cfg, monkeypatch):
    clock = {"wall": 1000.0}
    monkeypatch.setattr(auth_lease.time, "time", lambda: clock["wall"])
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _token: {"sub": "u", "exp": 1001.0})
    ws = _ws("token=t&companion_name=c1")
    fake_socket, _thread = _allow_tcp_connection(ws, monkeypatch)
    ws.opened()

    def expire_while_idle(_size):
        clock["wall"] = 1002.0
        raise proxy.socket.timeout()

    fake_socket.recv.side_effect = expire_while_idle
    ws._tcp_to_ws()

    ws._teardown.assert_called_once_with(
        1008,
        "authorization expired or revoked",
    )


def test_idle_proxy_closes_when_admin_api_token_is_revoked(
    cp_cfg,
    monkeypatch,
):
    clock = {"monotonic": 100.0}
    monkeypatch.setattr(
        auth_lease.time,
        "monotonic",
        lambda: clock["monotonic"],
    )
    token_info = {
        "id": 7,
        "name": "operator",
        "scope": "admin",
    }
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value=token_info),
        get_token=MagicMock(side_effect=[token_info, None]),
    )
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = token_manager
    ws = _ws("companion_name=c1", api_key="operator-token")
    fake_socket, _thread = _allow_tcp_connection(ws, monkeypatch)
    ws.opened()

    def revoke_while_idle(_size):
        clock["monotonic"] += 16.0
        raise proxy.socket.timeout()

    fake_socket.recv.side_effect = revoke_while_idle
    ws._tcp_to_ws()

    assert token_manager.get_token.call_count == 2
    ws._teardown.assert_called_once_with(
        1008,
        "authorization expired or revoked",
    )


def test_proxy_closes_on_auth_storage_failure_after_open(
    cp_cfg,
    monkeypatch,
):
    token_info = {
        "id": 7,
        "name": "operator",
        "scope": "admin",
    }
    token_manager = SimpleNamespace(
        verify_token=MagicMock(return_value=token_info),
        get_token=MagicMock(side_effect=CompanionStorageError("private database detail")),
    )
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=MagicMock())
    cp_cfg["token_manager"] = token_manager
    ws = _ws("companion_name=c1", api_key="operator-token")
    _allow_tcp_connection(ws, monkeypatch)
    ws.opened()

    ws._tcp_to_ws()

    ws._teardown.assert_called_once_with(
        1011,
        "authentication unavailable",
    )


def test_resolve_tcp_endpoint_paths(monkeypatch):
    ws = _ws("token=t")

    def listener(host, port, family=proxy.socket.AF_INET):
        bound_socket = SimpleNamespace(
            family=family,
            getsockname=lambda: (host, port),
        )
        return SimpleNamespace(sockets=[bound_socket])

    # no daemon
    proxy.set_daemon(None)
    assert ws._resolve_tcp_endpoint("c1") is None

    # daemon missing identity manager
    proxy.set_daemon(
        SimpleNamespace(
            companion_bridges={1: object()},
            companion_frame_servers=[],
            config={},
        )
    )
    assert ws._resolve_tcp_endpoint("c1") is None

    # daemon with empty bridges
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={},
        companion_frame_servers=[],
        config={"identities": {"companions": []}},
    )
    proxy.set_daemon(daemon)
    assert ws._resolve_tcp_endpoint("c1") is None

    # Runtime listener wins over restart-required config drift.
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={1: object()},
        companion_frame_servers=[
            SimpleNamespace(
                companion_hash="0x01",
                port=6000,
                bind_address="0.0.0.0",
                _server=listener("0.0.0.0", 6000),
            )
        ],
        config={
            "identities": {
                "companions": [
                    {
                        "name": "c1",
                        "settings": {
                            "tcp_port": 7000,
                            "bind_address": "192.0.2.1",
                        },
                    }
                ]
            }
        },
    )
    proxy.set_daemon(daemon)
    assert ws._resolve_tcp_endpoint("c1") == ("127.0.0.1", 6000)

    # IPv6 wildcard listeners are reached over IPv6 loopback.
    daemon.companion_frame_servers[0].bind_address = "::"
    daemon.companion_frame_servers[0]._server = listener(
        "::",
        6000,
        proxy.socket.AF_INET6,
    )
    assert ws._resolve_tcp_endpoint("c1") == ("::1", 6000)

    # found bridge but no running Frame listener
    daemon.companion_frame_servers = []
    assert ws._resolve_tcp_endpoint("c1") is None


def test_resolve_tcp_endpoint_uses_bound_socket_not_hostname(monkeypatch):
    ws = _ws("token=t")
    bound_socket = SimpleNamespace(
        family=proxy.socket.AF_INET,
        getsockname=lambda: ("127.0.0.2", 6100),
    )
    server = SimpleNamespace(
        companion_hash="0x01",
        # This hostname must never be re-resolved by the WS proxy.
        bind_address="listener.example.invalid",
        port=6000,
        _server=SimpleNamespace(sockets=[bound_socket]),
    )
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={1: object()},
        companion_frame_servers=[server],
    )
    proxy.set_daemon(daemon)

    assert ws._resolve_tcp_endpoint("c1") == ("127.0.0.2", 6100)


def test_resolve_tcp_endpoint_fails_closed_without_bound_socket(monkeypatch):
    ws = _ws("token=t")
    server = SimpleNamespace(
        companion_hash="0x01",
        bind_address="listener.example.invalid",
        port=6000,
        _server=SimpleNamespace(sockets=[]),
    )
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={1: object()},
        companion_frame_servers=[server],
    )
    proxy.set_daemon(daemon)

    assert ws._resolve_tcp_endpoint("c1") is None


def test_received_message_and_closed_paths():
    ws = _ws("token=t")
    ws._closing = False
    ws._tcp = MagicMock()
    ws._authorization = SimpleNamespace(is_active=lambda: True)

    ws.received_message(SimpleNamespace(data="abc"))
    ws._tcp.sendall.assert_called_once_with(b"abc")

    ws._tcp.sendall.side_effect = RuntimeError("sendfail")
    ws.received_message(SimpleNamespace(data=b"x"))
    ws._teardown.assert_called_once()

    ws.closed(1000, "done")
    assert ws._teardown.call_count == 2


def test_tcp_to_ws_and_teardown():
    ws = _ws("token=t")
    ws._teardown = MagicMock()
    ws._companion_name = "c1"
    ws._closing = False
    ws._authorization = SimpleNamespace(
        is_active=lambda: True,
        check_in=lambda _maximum: 15.0,
    )

    tcp = MagicMock()
    tcp.recv.side_effect = [b"a", b""]
    ws._tcp = tcp
    ws._tcp_to_ws()
    ws.send.assert_called_once_with(b"a", binary=True)
    ws._teardown.assert_called_once()

    # teardown closes tcp and closes websocket when active
    ws2 = _ws("token=t")
    ws2._closing = False
    ws2._companion_name = "c2"
    tcp_ref = MagicMock()
    ws2._tcp = tcp_ref
    ws2._teardown = proxy.CompanionFrameWebSocket._teardown.__get__(
        ws2, proxy.CompanionFrameWebSocket
    )
    ws2._teardown()
    tcp_ref.shutdown.assert_called_once_with(proxy.socket.SHUT_RDWR)
    tcp_ref.close.assert_called_once()
    ws2.close.assert_called_once()
