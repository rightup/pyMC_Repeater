"""Tests for the Mobile Companion API v1 action endpoints (phase 2).

Covers POST /api/v1/companions/{name}/messages (send DM/channel, the
Idempotency-Key contract of design doc §6) and the four
/contacts/{pubkey}/{action} handlers (login, status_request,
telemetry_request, reset_path) of §7.3, plus _cp_dispatch routing for both
URL shapes and GET/POST method gating on the shared /messages resource.

Handlers are invoked directly through ``__wrapped__`` (require_auth uses
functools.wraps), same pattern as tests/test_mobile_endpoints.py. Storage is
a real SQLiteHandler on tmp_path so the idempotency table round-trips for
real; the bridge is a fake with async send methods so no event loop thread
is actually needed — ``_run_async`` is monkeypatched to run the coroutine
synchronously (there is no daemon event loop in these tests).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cherrypy
import pytest
from openhop_core.companion.models import SentResult

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.mobile_endpoints import CompanionsV1, MobileAPIEndpoints

_HASH_BYTE = 0x01
_HASH = "0x01"
_NAME = "comp-test"
_PUBKEY_HEX = "aa" * 32


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    return SQLiteHandler(tmp_path)


class _FakeIdentity:
    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31


class _FakeBridge:
    def __init__(self):
        self.sent_texts = []
        self.sent_channels = []
        self.logins = []
        self.status_requests = []
        self.telemetry_requests = []
        self.reset_paths = []
        # Configurable results
        self.text_result = SentResult(success=True, is_flood=False, expected_ack=123)
        self.channel_result = True

    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31

    async def send_text_message(self, pub_key, text, txt_type=0):
        self.sent_texts.append((pub_key, text, txt_type))
        return self.text_result

    async def send_channel_message(self, channel_idx, text):
        self.sent_channels.append((channel_idx, text))
        return self.channel_result

    async def send_login(self, pub_key, password):
        self.logins.append((pub_key, password))
        return {"success": True, "pub_key": pub_key.hex()}

    async def send_status_request(self, pub_key, timeout=15.0):
        self.status_requests.append((pub_key, timeout))
        return {"success": True}

    async def send_telemetry_request(
        self, pub_key, want_base=True, want_location=True, want_environment=True, timeout=20.0
    ):
        self.telemetry_requests.append(
            (pub_key, want_base, want_location, want_environment, timeout)
        )
        return {"success": True, "sensors": {}}

    def reset_path(self, pub_key):
        self.reset_paths.append(pub_key)
        return True


def _daemon(handler, bridge):
    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: [(_NAME, _FakeIdentity(), {})] if t == "companion" else []
    )
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={_HASH_BYTE: bridge},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )


class _SyncLoop:
    """Stand-in for the daemon event loop: run_coroutine_threadsafe normally
    needs a live asyncio loop running on another thread. Tests have no
    daemon loop, so CompanionsV1._run_async is monkeypatched (below) to run
    the coroutine synchronously via asyncio.run instead of touching this."""


@pytest.fixture
def bridge():
    return _FakeBridge()


@pytest.fixture
def endpoints(handler, bridge):
    ep = CompanionsV1(daemon_instance=_daemon(handler, bridge), config={}, event_loop=_SyncLoop())

    def _run_async(coro, timeout=30.0):
        return asyncio.run(coro)

    ep._run_async = _run_async
    return ep


@pytest.fixture(autouse=True)
def request_context():
    """Minimal CherryPy request/response state for direct handler calls."""
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.request.user = {"username": "adam", "auth_type": "jwt"}
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    cherrypy.serving.response.status = None


def _call(bound_method, **kwargs):
    """Invoke an endpoint bypassing require_auth (via functools.wraps chain)."""
    return bound_method.__wrapped__(bound_method.__self__, **kwargs)


def _post(endpoints, body, idempotency_key="idem-1", headers=None):
    """Arrange a POST: method, Idempotency-Key header, and a JSON body.

    ``cherrypy.request.body`` isn't wired up outside a real HTTP request, so
    (matching the pattern in tests/test_companion_settings.py) the body is
    injected by monkeypatching ``_get_json_body`` directly rather than
    faking a WSGI body stream.
    """
    cherrypy.serving.request.method = "POST"
    h = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else {}
    if headers:
        h.update(headers)
    cherrypy.serving.request.headers = h
    endpoints._get_json_body = lambda: body


# --- POST /messages: send DM / channel ---------------------------------------


class TestSendMessage:
    def test_send_dm_happy_path(self, endpoints, bridge):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["success"] is True
        assert result["data"] == {"sent": True, "is_flood": False, "expected_ack": 123}
        assert len(bridge.sent_texts) == 1
        pub_key, text, txt_type = bridge.sent_texts[0]
        assert pub_key == bytes.fromhex(_PUBKEY_HEX)
        assert text == "hello"
        assert txt_type == 0

    def test_send_channel_happy_path(self, endpoints, bridge):
        _post(endpoints, {"channel_idx": 0, "text": "hi channel"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["success"] is True
        assert result["data"] == {"sent": True}
        assert bridge.sent_channels == [(0, "hi channel")]

    def test_missing_idempotency_key_400(self, endpoints):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key=None)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    def test_missing_text_400(self, endpoints):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": ""})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    def test_neither_to_nor_channel_400(self, endpoints):
        _post(endpoints, {"text": "hello"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    def test_both_to_and_channel_400(self, endpoints):
        _post(endpoints, {"to": _PUBKEY_HEX, "channel_idx": 0, "text": "hello"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    def test_retry_same_key_same_body_replays_without_resend(self, endpoints, bridge):
        body = {"to": _PUBKEY_HEX, "text": "hello"}
        _post(endpoints, dict(body))
        first = _call(endpoints.messages, companion_name=_NAME)

        _post(endpoints, dict(body))
        second = _call(endpoints.messages, companion_name=_NAME)

        assert second == first
        assert len(bridge.sent_texts) == 1  # bridge called exactly once total

    def test_retry_same_key_different_body_409(self, endpoints, bridge):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        _call(endpoints.messages, companion_name=_NAME)

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "different"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 409
        assert len(bridge.sent_texts) == 1  # second send never reached the radio

    def test_retry_same_key_different_companion_409(self, endpoints, handler, bridge):
        # A second companion identity sharing the same fake bridge/hash byte
        # under a different registered name.
        identity_manager = SimpleNamespace(
            get_identities_by_type=lambda t: (
                [(_NAME, _FakeIdentity(), {}), ("comp-other", _FakeIdentity(), {})]
                if t == "companion"
                else []
            )
        )
        endpoints.daemon_instance = SimpleNamespace(
            identity_manager=identity_manager,
            companion_bridges={_HASH_BYTE: bridge},
            repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
        )

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        _call(endpoints.messages, companion_name=_NAME)

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name="comp-other")
        assert exc.value.status == 409

    def test_failed_send_not_stored_retry_reaches_bridge_again(self, endpoints, bridge):
        bridge.text_result = SentResult(success=False, error="send_failed")
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        first = _call(endpoints.messages, companion_name=_NAME)
        assert first["data"]["sent"] is False

        bridge.text_result = SentResult(success=True, is_flood=False, expected_ack=5)
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})  # same Idempotency-Key
        second = _call(endpoints.messages, companion_name=_NAME)
        assert second["data"]["sent"] is True
        assert len(bridge.sent_texts) == 2  # radio touched both times

    def test_failed_channel_send_not_stored(self, endpoints, bridge):
        bridge.channel_result = False
        _post(endpoints, {"channel_idx": 1, "text": "hi"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["data"]["sent"] is False

        bridge.channel_result = True
        _post(endpoints, {"channel_idx": 1, "text": "hi"})  # same key, retried
        result2 = _call(endpoints.messages, companion_name=_NAME)
        assert result2["data"]["sent"] is True
        assert len(bridge.sent_channels) == 2

    def test_jwt_caller_fallback_scope_key(self, endpoints, handler, bridge):
        """JWT callers (no token_id) scope idempotency by 'user:{username}'."""
        cherrypy.serving.request.user = {"username": "adam", "auth_type": "jwt"}
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key="jwt-key")
        _call(endpoints.messages, companion_name=_NAME)

        stored = handler.companion_idempotency_get("user:adam", "jwt-key")
        assert stored is not None

    def test_api_token_caller_scopes_by_device_id(self, endpoints, handler, bridge):
        token_id = handler.create_api_token("phone", "hash-1")
        assert handler.companion_device_create(_HASH, "device-xyz", "Phone", token_id) is not None

        cherrypy.serving.request.user = {"auth_type": "api_token", "token_id": token_id}
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key="dev-key")
        _call(endpoints.messages, companion_name=_NAME)

        stored = handler.companion_idempotency_get("device-xyz", "dev-key")
        assert stored is not None


# --- Method gating on /messages -----------------------------------------------


class TestMessagesMethodGating:
    def test_get_still_serves_history(self, endpoints, handler):
        handler.companion_push_message(
            _HASH, {"text": "hello", "timestamp": 1, "packet_hash": "msg-1"}
        )
        cherrypy.serving.request.method = "GET"
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["success"] is True
        assert result["data"]["messages"][0]["text"] == "hello"

    def test_unsupported_method_405(self, endpoints):
        cherrypy.serving.request.method = "DELETE"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 405


# --- POST /contacts/{pubkey}/{action} -----------------------------------------


class TestContactActions:
    def test_login_with_password(self, endpoints, bridge, monkeypatch):
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {}
        monkeypatch.setattr(endpoints, "_get_json_body", lambda: {"password": "secret"})
        result = endpoints.login.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )
        assert result["success"] is True
        assert bridge.logins == [(bytes.fromhex(_PUBKEY_HEX), "secret")]

    def test_status_request_dispatch(self, endpoints, bridge):
        cherrypy.serving.request.method = "POST"
        result = endpoints.status_request.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )
        assert result["success"] is True
        assert len(bridge.status_requests) == 1
        assert bridge.status_requests[0][0] == bytes.fromhex(_PUBKEY_HEX)

    def test_telemetry_request_dispatch(self, endpoints, bridge):
        cherrypy.serving.request.method = "POST"
        result = endpoints.telemetry_request.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )
        assert result["success"] is True
        assert len(bridge.telemetry_requests) == 1
        pub_key, want_base, want_location, want_environment, timeout = bridge.telemetry_requests[0]
        assert pub_key == bytes.fromhex(_PUBKEY_HEX)
        assert (want_base, want_location, want_environment) == (True, True, True)

    def test_reset_path_dispatch(self, endpoints, bridge):
        cherrypy.serving.request.method = "POST"
        result = endpoints.reset_path.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )
        assert result["success"] is True
        assert result["data"] == {"reset": True}
        assert bridge.reset_paths == [bytes.fromhex(_PUBKEY_HEX)]

    def test_contact_action_requires_post(self, endpoints):
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            endpoints.reset_path.__wrapped__(
                endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
            )
        assert exc.value.status == 405


# --- _cp_dispatch routing ------------------------------------------------------


class TestDispatchRouting:
    def test_messages_route_len2(self, endpoints):
        vpath = [_NAME, "messages"]
        handler_fn = endpoints._cp_dispatch(vpath)
        assert handler_fn == endpoints.messages
        assert cherrypy.request.params["companion_name"] == _NAME

    def test_contacts_action_route_len4(self, endpoints):
        vpath = [_NAME, "contacts", _PUBKEY_HEX, "login"]
        handler_fn = endpoints._cp_dispatch(vpath)
        assert handler_fn == endpoints.login
        assert cherrypy.request.params["companion_name"] == _NAME
        assert cherrypy.request.params["contact_pubkey"] == _PUBKEY_HEX

    def test_contacts_route_all_actions(self, endpoints):
        for action in ("login", "status_request", "telemetry_request", "reset_path"):
            cherrypy.request.params = {}
            vpath = [_NAME, "contacts", _PUBKEY_HEX, action]
            handler_fn = endpoints._cp_dispatch(vpath)
            assert handler_fn == getattr(endpoints, action)

    def test_contacts_unknown_action_falls_through(self, endpoints):
        vpath = [_NAME, "contacts", _PUBKEY_HEX, "not_a_real_action"]
        assert endpoints._cp_dispatch(vpath) is None

    def test_three_segment_collection_members_route(self, endpoints):
        """``contacts/{pubkey}`` and ``channels/{index}`` are members of their
        collection, distinct from the ``{pubkey}/{action}`` sub-resources.
        Added 2026-07-18 with contact add/remove and channel join."""
        # `==` not `is`: attribute access creates a fresh bound method each time.
        assert endpoints._cp_dispatch([_NAME, "contacts", _PUBKEY_HEX]) == endpoints.contact
        assert endpoints._cp_dispatch([_NAME, "channels", "3"]) == endpoints.channel

    def test_unknown_three_segment_collection_falls_through(self, endpoints):
        assert endpoints._cp_dispatch([_NAME, "widgets", "1"]) is None

    def test_wrong_length_falls_through(self, endpoints):
        assert endpoints._cp_dispatch([_NAME]) is None


# --- Constructor plumbing ------------------------------------------------------


class TestEventLoopPlumbing:
    def test_mobile_api_endpoints_passes_event_loop_through(self, handler):
        loop = _SyncLoop()
        root = MobileAPIEndpoints(daemon_instance=_daemon(handler, _FakeBridge()), event_loop=loop)
        assert root.companions.event_loop is loop

    def test_run_async_503_when_no_loop(self, handler):
        ep = CompanionsV1(daemon_instance=_daemon(handler, _FakeBridge()), config={})
        coro = asyncio.sleep(0)  # never scheduled; the 503 check precedes it
        try:
            with pytest.raises(cherrypy.HTTPError) as exc:
                ep._run_async(coro)
            assert exc.value.status == 503
        finally:
            coro.close()  # avoid a "coroutine was never awaited" warning
