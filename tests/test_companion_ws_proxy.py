from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.web import companion_ws_proxy as proxy


@pytest.fixture
def cp_cfg(monkeypatch):
    cfg = {}
    monkeypatch.setattr(cherrypy, "config", cfg, raising=False)
    return cfg


def _ws(query_string):
    ws = object.__new__(proxy.CompanionFrameWebSocket)
    ws.environ = {"QUERY_STRING": query_string}
    ws.close = MagicMock()
    ws.send = MagicMock()
    ws._teardown = MagicMock()
    return ws


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


def test_opened_rejects_missing_companion_name(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: {"sub": "u"})
    ws = _ws("token=t")
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="missing companion_name")


def test_opened_rejects_missing_companion_endpoint(cp_cfg):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: {"sub": "u"})
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=None)
    ws.opened()
    ws.close.assert_called_once_with(code=1008, reason="companion not found")


def test_opened_tcp_connect_failure(cp_cfg, monkeypatch):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: {"sub": "u"})
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))

    fake_socket = MagicMock()
    fake_socket.connect.side_effect = RuntimeError("nope")
    monkeypatch.setattr(proxy.socket, "socket", lambda *_args, **_kwargs: fake_socket)

    ws.opened()
    ws.close.assert_called_once_with(code=1011, reason="TCP connect failed")


def test_opened_success_starts_reader_thread(cp_cfg, monkeypatch):
    cp_cfg["jwt_handler"] = SimpleNamespace(verify_jwt=lambda _t: {"sub": "u"})
    ws = _ws("token=t&companion_name=c1")
    ws._resolve_tcp_endpoint = MagicMock(return_value=("127.0.0.1", 5000))

    fake_socket = MagicMock()
    monkeypatch.setattr(proxy.socket, "socket", lambda *_args, **_kwargs: fake_socket)

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


def test_resolve_tcp_endpoint_paths(monkeypatch):
    ws = _ws("token=t")

    # no daemon
    proxy.set_daemon(None)
    assert ws._resolve_tcp_endpoint("c1") is None

    # daemon missing identity manager
    proxy.set_daemon(SimpleNamespace(companion_bridges={1: object()}, config={}))
    assert ws._resolve_tcp_endpoint("c1") is None

    # daemon with empty bridges
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={},
        config={"identities": {"companions": []}},
    )
    proxy.set_daemon(daemon)
    assert ws._resolve_tcp_endpoint("c1") is None

    # found in identity+bridge and in config, bind 0.0.0.0 => loopback
    daemon = SimpleNamespace(
        identity_manager=SimpleNamespace(
            get_identities_by_type=lambda _t: [
                ("c1", SimpleNamespace(get_public_key=lambda: b"\x01"), {})
            ]
        ),
        companion_bridges={1: object()},
        config={
            "identities": {
                "companions": [
                    {"name": "c1", "settings": {"tcp_port": 6000, "bind_address": "0.0.0.0"}}
                ]
            }
        },
    )
    proxy.set_daemon(daemon)
    assert ws._resolve_tcp_endpoint("c1") == ("127.0.0.1", 6000)

    # found bridge but missing in config
    daemon.config = {"identities": {"companions": []}}
    assert ws._resolve_tcp_endpoint("c1") is None


def test_received_message_and_closed_paths():
    ws = _ws("token=t")
    ws._closing = False
    ws._tcp = MagicMock()

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
