"""Tests for the Mobile Companion API v1 action endpoints (phase 2).

Covers POST /api/v1/companions/{name}/messages (send DM/channel, the
Idempotency-Key contract of design doc §6) and the
/contacts/{pubkey}/{action} handlers (login, connection, logout,
status_request, telemetry_request, reset_path) of §7.3, plus _cp_dispatch routing for both
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
import io
import threading
import time
from concurrent.futures import CancelledError as FutureCancelledError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cherrypy
import pytest
from openhop_core.companion.constants import ADV_TYPE_CHAT, ADV_TYPE_NONE
from openhop_core.companion.models import Contact, SentResult
from openhop_core.protocol import LocalIdentity

from repeater.companion.bridge import (
    ChannelTextCapacityError,
    RepeaterCompanionBridge,
)
from repeater.companion.correlation import outbound_send_capture
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web import mobile_endpoints as mobile_endpoints_module
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
        self.logouts = []
        self.login_connections = set()
        self.status_requests = []
        self.telemetry_requests = []
        self.reset_paths = []
        # Configurable results
        self.text_result = SentResult(success=True, is_flood=False, expected_ack=123)
        self.channel_result = True
        self.prefs = SimpleNamespace(node_name="TestNode")
        self.channels = SimpleNamespace(max_channels=8)
        contact = SimpleNamespace(
            public_key=bytes.fromhex(_PUBKEY_HEX),
            name="Alice",
            adv_type=1,
            flags=0,
            out_path_len=1,
            out_path=b"\x01",
            last_advert_timestamp=1,
            lastmod=1,
            gps_lat=0.0,
            gps_lon=0.0,
        )
        self.contacts = SimpleNamespace(get_by_key=lambda key: contact)

    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31

    @staticmethod
    def _capture_hash():
        holder = outbound_send_capture.get()
        if holder is not None:
            holder["hash"] = "AB" * 32

    async def send_text_message(self, pub_key, text, txt_type=0, **kwargs):
        self.sent_texts.append((pub_key, text, txt_type))
        if self.text_result.success:
            self._capture_hash()
        return self.text_result

    async def send_channel_message(self, channel_idx, text):
        self.sent_channels.append((channel_idx, text))
        if self.channel_result:
            self._capture_hash()
        return self.channel_result

    async def send_login(self, pub_key, password):
        self.logins.append((pub_key, password))
        self.login_connections.add(bytes(pub_key))
        return {"success": True, "pub_key": pub_key.hex()}

    def has_login_connection(self, pub_key):
        return bytes(pub_key) in self.login_connections

    async def send_logout(self, pub_key):
        self.logouts.append(bytes(pub_key))
        self.login_connections.discard(bytes(pub_key))
        return True

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
    journal = CompanionEventJournal(handler, _HASH)
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={_HASH_BYTE: bridge},
        companion_journals={_HASH: journal},
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
    ep._get_json_body = lambda: {}
    return ep


@pytest.fixture(autouse=True)
def request_context():
    """Minimal CherryPy request/response state for direct handler calls."""
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.request.user = {
        "username": "adam",
        "auth_type": "jwt",
        "scope": "admin",
    }
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
    @staticmethod
    def _assert_dispatch_unavailable_is_terminal(
        endpoints,
        handler,
        bridge,
    ):
        body = {"to": _PUBKEY_HEX, "text": "hello"}
        _post(endpoints, body)

        first = _call(endpoints.messages, companion_name=_NAME)

        assert first == {
            "success": True,
            "data": {
                "message_id": first["data"]["message_id"],
                "sent": False,
                "state": "failed",
                "reason": "Radio dispatch is unavailable",
            },
        }
        assert bridge.sent_texts == []
        stored = handler.companion_idempotency_get("jwt:adam:unknown", "idem-1")
        assert stored["state"] not in {"pending", "indeterminate"}
        message = handler.companion_message_get_by_id(
            _HASH,
            first["data"]["message_id"],
        )
        assert message["state"] == "failed"

        _post(endpoints, body)
        replay = _call(endpoints.messages, companion_name=_NAME)

        assert replay == first
        assert cherrypy.response.headers["Idempotency-Replayed"] == "true"
        assert bridge.sent_texts == []

    def test_no_event_loop_is_terminal_failed_without_bridge_send(
        self,
        handler,
        bridge,
    ):
        endpoints = CompanionsV1(
            daemon_instance=_daemon(handler, bridge),
            config={},
            event_loop=None,
        )
        self._assert_dispatch_unavailable_is_terminal(endpoints, handler, bridge)

    def test_scheduling_failure_is_terminal_failed_without_bridge_send(
        self,
        handler,
        bridge,
        monkeypatch,
    ):
        endpoints = CompanionsV1(
            daemon_instance=_daemon(handler, bridge),
            config={},
            event_loop=_SyncLoop(),
        )

        def fail_before_scheduling(_coro, _loop):
            raise RuntimeError("loop is closed")

        monkeypatch.setattr(
            asyncio,
            "run_coroutine_threadsafe",
            fail_before_scheduling,
        )
        self._assert_dispatch_unavailable_is_terminal(endpoints, handler, bridge)

    def test_same_key_concurrency_reserves_once_without_second_rate_charge(self):
        endpoint = CompanionsV1(config={})
        state = {"reservation": None, "reserve_count": 0, "admit_count": 0}
        state_lock = threading.Lock()

        class _Handler:
            def companion_idempotency_lookup(self, *_args):
                # Widen the old lookup/admit/reserve race. The endpoint's
                # keyed stripe must keep the second caller outside it.
                time.sleep(0.02)
                with state_lock:
                    reservation = state["reservation"]
                    return dict(reservation) if reservation is not None else None

        class _Journal:
            def reserve_outbound_send(self, *_args):
                with state_lock:
                    state["reserve_count"] += 1
                    state["reservation"] = {"result": "in_progress"}
                return {"result": "reserved", "message_id": 1}

        def admit_once():
            with state_lock:
                state["admit_count"] += 1
                if state["admit_count"] > 1:
                    raise cherrypy.HTTPError(429, "RF request rate exceeded")

        endpoint._admit_rf = admit_once
        start = threading.Barrier(3)
        results = []
        errors = []

        def reserve():
            start.wait()
            try:
                results.append(
                    endpoint._lookup_or_reserve_outbound(
                        _Handler(),
                        _Journal(),
                        "jwt",
                        "operator:chat",
                        "same-key",
                        "request-hash",
                        {},
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        callers = [threading.Thread(target=reserve) for _ in range(2)]
        for caller in callers:
            caller.start()
        start.wait()
        for caller in callers:
            caller.join(timeout=2.0)

        assert errors == []
        assert sorted(result["result"] for result in results) == [
            "in_progress",
            "reserved",
        ]
        assert state["reserve_count"] == 1
        assert state["admit_count"] == 1
        assert len(endpoint._idempotency_locks) == 64

    def test_marker_eviction_blocks_absent_key_before_rf(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            mobile_endpoints_module,
            "_UNPERSISTED_INDETERMINATE_MAX",
            1,
        )
        endpoint = CompanionsV1(config={})
        endpoint._remember_unpersisted_indeterminate(
            "jwt",
            "operator:chat",
            "first-key",
            "first-request",
            1,
            None,
            None,
        )
        endpoint._remember_unpersisted_indeterminate(
            "jwt",
            "operator:chat",
            "second-key",
            "second-request",
            2,
            None,
            None,
        )

        calls = {"admit": 0, "reserve": 0}

        class _Handler:
            @staticmethod
            def companion_idempotency_lookup(*_args):
                return None

        class _Journal:
            @staticmethod
            def reserve_outbound_send(*_args):
                calls["reserve"] += 1
                raise AssertionError("marker eviction must block reservation")

        def admit():
            calls["admit"] += 1
            raise AssertionError("marker eviction must block RF admission")

        endpoint._admit_rf = admit
        result = endpoint._lookup_or_reserve_outbound(
            _Handler(),
            _Journal(),
            "jwt",
            "operator:chat",
            "unknown-key",
            "unknown-request",
            {},
        )

        assert endpoint._unpersisted_indeterminate_safety_lost is True
        assert len(endpoint._unpersisted_indeterminate) == 1
        assert result == {
            "result": "indeterminate",
            "state": "indeterminate",
            "message_id": None,
            "packet_hash": None,
            "expected_ack": None,
        }
        assert calls == {"admit": 0, "reserve": 0}

    @pytest.mark.parametrize(
        "response_json",
        [
            "{not-json",
            "[]",
            '{"success":true,"data":{"message_id":1,"sent":true,"state":"transmitted","value":NaN}}',
            '{"success":true,"success":false,"data":{"message_id":1,"sent":true,"state":"transmitted"}}',
            '{"success":true}',
            '{"success":true,"data":{}}',
            '{"success":true,"data":{"message_id":1,"sent":true,"state":"transmitted","expected_ack":-1}}',
            '{"success":true,"data":{"message_id":1,"sent":true,"state":"transmitted","expected_ack":4294967296}}',
        ],
    )
    def test_corrupt_replay_fails_closed_without_second_send(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
        response_json,
    ):
        monkeypatch.setattr(
            handler,
            "companion_idempotency_lookup",
            lambda *_args: {
                "result": "replay",
                "state": "complete",
                "response_json": response_json,
            },
        )
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)

        assert exc.value.status == 503
        assert bridge.sent_texts == []

    def test_send_dm_happy_path(self, endpoints, bridge):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["success"] is True
        assert result["data"]["sent"] is True
        assert result["data"]["state"] == "transmitted"
        assert result["data"]["is_flood"] is False
        assert result["data"]["expected_ack"] == 123
        assert result["data"]["message_id"] > 0
        assert result["data"]["packet_hash"] == "AB" * 8
        assert len(bridge.sent_texts) == 1
        pub_key, text, txt_type = bridge.sent_texts[0]
        assert pub_key == bytes.fromhex(_PUBKEY_HEX)
        assert text == "hello"
        assert txt_type == 0

    @pytest.mark.parametrize("expected_ack", [-1, 1 << 32])
    def test_send_dm_rejects_expected_ack_outside_uint32(
        self,
        endpoints,
        handler,
        bridge,
        expected_ack,
    ):
        bridge.text_result = SentResult(
            success=True,
            is_flood=False,
            expected_ack=expected_ack,
        )
        _post(
            endpoints,
            {"to": _PUBKEY_HEX, "text": "hello"},
            idempotency_key=f"bad-ack-{expected_ack}",
        )

        result = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 503
        assert result["data"]["state"] == "indeterminate"
        assert "expected_ack" not in result["data"]
        message = handler.companion_message_get_by_id(
            _HASH,
            result["data"]["message_id"],
        )
        assert message["state"] == "indeterminate"

    def test_send_channel_happy_path(self, endpoints, bridge):
        _post(endpoints, {"channel_idx": 0, "text": "hi channel"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["success"] is True
        assert result["data"]["sent"] is True
        assert result["data"]["state"] == "transmitted"
        assert result["data"]["message_id"] > 0
        assert bridge.sent_channels == [(0, "hi channel")]

    def test_missing_idempotency_key_400(self, endpoints):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key=None)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    @pytest.mark.parametrize(
        "idempotency_key",
        [
            " idem",
            "idem ",
            "idem key",
            "idem\tkey",
            "idem\nkey",
            "emoji-\N{ROCKET}",
        ],
    )
    def test_idempotency_key_rejects_ambiguous_whitespace(
        self,
        endpoints,
        bridge,
        idempotency_key,
    ):
        _post(
            endpoints,
            {"to": _PUBKEY_HEX, "text": "hello"},
            idempotency_key=idempotency_key,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)

        assert exc.value.status == 400
        assert bridge.sent_texts == []

    def test_duplicate_send_body_field_is_rejected_before_radio(
        self,
        endpoints,
        bridge,
    ):
        raw = b'{"to":"' + _PUBKEY_HEX.encode("ascii") + b'","text":"one","text":"two"}'
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
            "Idempotency-Key": "duplicate-body",
        }
        cherrypy.serving.request.body = io.BytesIO(raw)
        endpoints._get_json_body = CompanionsV1._get_json_body.__get__(
            endpoints,
            CompanionsV1,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)

        assert exc.value.status == 400
        assert bridge.sent_texts == []

    def test_non_json_media_type_is_rejected_before_radio(
        self,
        endpoints,
        bridge,
    ):
        raw = b'{"to":"' + _PUBKEY_HEX.encode("ascii") + b'","text":"hello"}'
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "text/plain",
            "Idempotency-Key": "wrong-media-type",
        }
        cherrypy.serving.request.body = io.BytesIO(raw)
        endpoints._get_json_body = CompanionsV1._get_json_body.__get__(
            endpoints,
            CompanionsV1,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)

        assert exc.value.status == 415
        assert bridge.sent_texts == []

    def test_missing_text_400(self, endpoints):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": ""})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    @pytest.mark.parametrize(
        "target",
        [
            {"to": _PUBKEY_HEX},
            {"channel_idx": 0},
        ],
    )
    def test_nul_text_is_rejected_before_storage_or_radio(
        self,
        endpoints,
        handler,
        bridge,
        target,
    ):
        _post(
            endpoints,
            {**target, "text": "visible\x00hidden"},
            idempotency_key="nul-text",
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)

        assert exc.value.status == 400
        assert handler.companion_count_messages(_HASH) == 0
        assert handler.companion_idempotency_get("jwt:adam:unknown", "nul-text") is None
        assert bridge.sent_texts == []
        assert bridge.sent_channels == []

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

    @pytest.mark.parametrize("channel_idx", [True, 1.0, "1"])
    def test_channel_index_requires_a_json_integer(self, endpoints, channel_idx):
        _post(endpoints, {"channel_idx": channel_idx, "text": "hello"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 400

    @pytest.mark.parametrize("txt_type", [False, 0.0, "0"])
    def test_text_type_requires_a_json_integer(self, endpoints, txt_type):
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello", "txt_type": txt_type})
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
        other_hash_byte = 0x02
        other_hash = "0x02"

        class _OtherIdentity:
            def get_public_key(self):
                return bytes([other_hash_byte]) + b"\x33" * 31

        other_bridge = _FakeBridge()
        other_bridge.get_public_key = _OtherIdentity().get_public_key
        identity_manager = SimpleNamespace(
            get_identities_by_type=lambda t: (
                [(_NAME, _FakeIdentity(), {}), ("comp-other", _OtherIdentity(), {})]
                if t == "companion"
                else []
            )
        )
        endpoints.daemon_instance = SimpleNamespace(
            identity_manager=identity_manager,
            companion_bridges={_HASH_BYTE: bridge, other_hash_byte: other_bridge},
            companion_journals={
                _HASH: CompanionEventJournal(handler, _HASH),
                other_hash: CompanionEventJournal(handler, other_hash),
            },
            repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
        )

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        _call(endpoints.messages, companion_name=_NAME)

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name="comp-other")
        assert exc.value.status == 409

    def test_failed_send_is_terminal_and_replays_without_resend(self, endpoints, bridge):
        bridge.text_result = SentResult(success=False, error="send_failed")
        cherrypy.serving.response.status = 200
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        first = _call(endpoints.messages, companion_name=_NAME)
        assert first["data"]["sent"] is False
        assert first["data"]["reason"] == "Direct-message send failed before radio dispatch"
        first_status = cherrypy.serving.response.status

        bridge.text_result = SentResult(success=True, is_flood=False, expected_ack=5)
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})  # same Idempotency-Key
        second = _call(endpoints.messages, companion_name=_NAME)
        assert second == first
        assert cherrypy.serving.response.status == first_status == 200
        assert len(bridge.sent_texts) == 1

    def test_failed_channel_send_is_replayed(self, endpoints, bridge):
        bridge.channel_result = False
        _post(endpoints, {"channel_idx": 1, "text": "hi"})
        result = _call(endpoints.messages, companion_name=_NAME)
        assert result["data"]["sent"] is False
        assert result["data"]["reason"] == "Channel send failed before radio dispatch"

        bridge.channel_result = True
        _post(endpoints, {"channel_idx": 1, "text": "hi"})  # same key, retried
        result2 = _call(endpoints.messages, companion_name=_NAME)
        assert result2 == result
        assert len(bridge.sent_channels) == 1

    def test_committed_name_capacity_failure_is_terminal_and_replayed(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
    ):
        attempts = []

        async def renamed_before_packet(channel_idx, text):
            attempts.append((channel_idx, text))
            raise ChannelTextCapacityError(127)

        monkeypatch.setattr(
            bridge,
            "send_channel_message",
            renamed_before_packet,
        )
        body = {"channel_idx": 1, "text": "x" * 128}

        _post(endpoints, body, idempotency_key="rename-race")
        first = _call(endpoints.messages, companion_name=_NAME)

        assert first["data"] == {
            "message_id": first["data"]["message_id"],
            "sent": False,
            "state": "failed",
            "reason": ("text exceeds 127 UTF-8 bytes for the current channel sender name"),
        }
        message = handler.companion_message_get_by_id(
            _HASH,
            first["data"]["message_id"],
        )
        assert message["state"] == "failed"

        _post(endpoints, body, idempotency_key="rename-race")
        replay = _call(endpoints.messages, companion_name=_NAME)
        assert replay == first
        assert attempts == [(1, "x" * 128)]

    @pytest.mark.parametrize("is_channel", [False, True])
    @pytest.mark.parametrize(
        ("initial_state", "expected_state", "expected_sent", "first_status"),
        [
            ("transmitted", "transmitted", True, 200),
            ("indeterminate", "indeterminate", None, 503),
            ("failed", "failed", False, 200),
        ],
    )
    def test_rf_capture_wins_over_false_bridge_result_without_duplicate_retry(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
        is_channel,
        initial_state,
        expected_state,
        expected_sent,
        first_status,
    ):
        async def direct_result(pub_key, text, txt_type=0, **kwargs):
            bridge.sent_texts.append((pub_key, text, txt_type))
            capture = outbound_send_capture.get()
            capture["initial_state"] = initial_state
            if initial_state != "failed":
                capture["hash"] = "AB" * 32
                capture["expected_ack"] = 444
            return SentResult(
                success=False,
                is_flood=False,
                expected_ack=444 if initial_state != "failed" else None,
                error="send_failed",
            )

        async def channel_result(channel_idx, text):
            bridge.sent_channels.append((channel_idx, text))
            capture = outbound_send_capture.get()
            capture["initial_state"] = initial_state
            if initial_state != "failed":
                capture["hash"] = "AB" * 32
            return False

        if is_channel:
            monkeypatch.setattr(
                bridge,
                "send_channel_message",
                channel_result,
            )
            body = {"channel_idx": 1, "text": "capture"}
        else:
            monkeypatch.setattr(
                bridge,
                "send_text_message",
                direct_result,
            )
            body = {"to": _PUBKEY_HEX, "text": "capture"}

        cherrypy.serving.response.status = 200
        _post(endpoints, body, idempotency_key="capture-key")
        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == first_status
        assert first["data"]["state"] == expected_state
        if expected_sent is not None:
            assert first["data"]["sent"] is expected_sent
        if initial_state == "failed":
            expected_reason = (
                "Radio rejected the channel send"
                if is_channel
                else "Radio rejected the direct-message send"
            )
            assert first["data"]["reason"] == expected_reason
        message_id = first["data"]["message_id"]
        assert (
            handler.companion_message_get_by_id(
                _HASH,
                message_id,
            )["state"]
            == expected_state
        )

        _post(endpoints, body, idempotency_key="capture-key")
        retry = _call(endpoints.messages, companion_name=_NAME)
        if initial_state == "indeterminate":
            assert cherrypy.serving.response.status == 409
            assert retry["data"]["state"] == "indeterminate"
        else:
            assert retry == first
        assert len(bridge.sent_channels if is_channel else bridge.sent_texts) == 1

    @pytest.mark.parametrize("is_channel", [False, True])
    @pytest.mark.parametrize(
        ("completion_failure", "failure"),
        [
            ("timeout", cherrypy.HTTPError(504, "Timed out waiting for radio response")),
            ("cancellation", FutureCancelledError()),
            ("exception", RuntimeError("post-transmit work failed")),
        ],
    )
    def test_post_transmit_failure_retains_capture_and_blocks_duplicate_retry(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
        is_channel,
        completion_failure,
        failure,
    ):
        async def direct_result(pub_key, text, txt_type=0, **kwargs):
            bridge.sent_texts.append((pub_key, text, txt_type))
            capture = outbound_send_capture.get()
            capture["initial_state"] = "transmitted"
            capture["hash"] = "AB" * 32
            capture["expected_ack"] = 444
            return SentResult(
                success=True,
                is_flood=False,
                expected_ack=444,
            )

        async def channel_result(channel_idx, text):
            bridge.sent_channels.append((channel_idx, text))
            capture = outbound_send_capture.get()
            capture["initial_state"] = "transmitted"
            capture["hash"] = "AB" * 32
            return True

        if is_channel:
            monkeypatch.setattr(bridge, "send_channel_message", channel_result)
            body = {"channel_idx": 1, "text": completion_failure}
        else:
            monkeypatch.setattr(bridge, "send_text_message", direct_result)
            body = {"to": _PUBKEY_HEX, "text": completion_failure}

        run_async = endpoints._run_async

        def fail_after_completion(coro, timeout=30.0):
            run_async(coro, timeout)
            raise failure

        monkeypatch.setattr(endpoints, "_run_async", fail_after_completion)
        _post(endpoints, body, idempotency_key=f"post-tx-{completion_failure}")

        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 503
        assert first["data"] == {
            "message_id": first["data"]["message_id"],
            "state": "indeterminate",
            "packet_hash": "AB" * 8,
            **({"expected_ack": 444} if not is_channel else {}),
        }
        message_id = first["data"]["message_id"]
        stored = handler.companion_message_get_by_id(_HASH, message_id)
        assert stored["state"] == "indeterminate"
        assert stored["packet_hash"] == "AB" * 8
        assert stored["expected_ack"] == (444 if not is_channel else None)

        _post(endpoints, body, idempotency_key=f"post-tx-{completion_failure}")
        retry = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert retry["data"]["message_id"] == first["data"]["message_id"]
        assert retry["data"]["state"] == "indeterminate"
        assert retry["data"]["packet_hash"] == "AB" * 8
        assert retry["data"]["expected_ack"] == (444 if not is_channel else None)
        assert len(bridge.sent_channels if is_channel else bridge.sent_texts) == 1

    @pytest.mark.parametrize("is_channel", [False, True])
    def test_prune_winning_completion_race_returns_correlated_indeterminate(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
        is_channel,
    ):
        complete = handler.companion_complete_outbound_send

        def prune_before_completion(*args, **kwargs):
            with handler._connect() as conn:
                conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET created_at = ?
                    WHERE device_id = 'jwt:adam:unknown'
                      AND idempotency_key = 'prune-race'
                    """,
                    (time.time() - 2,),
                )
                conn.commit()
            handler.companion_idempotency_prune(max_age_seconds=1)
            return complete(*args, **kwargs)

        monkeypatch.setattr(
            handler,
            "companion_complete_outbound_send",
            prune_before_completion,
        )
        body = (
            {"channel_idx": 1, "text": "prune race"}
            if is_channel
            else {"to": _PUBKEY_HEX, "text": "prune race"}
        )
        _post(endpoints, body, idempotency_key="prune-race")

        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 503
        assert first["data"]["state"] == "indeterminate"
        assert first["data"]["packet_hash"] == "AB" * 8
        if is_channel:
            assert "expected_ack" not in first["data"]
        else:
            assert first["data"]["expected_ack"] == 123
        message_id = first["data"]["message_id"]
        key = handler.companion_idempotency_get(
            "jwt:adam:unknown",
            "prune-race",
        )
        message = handler.companion_message_get_by_id(_HASH, message_id)
        assert key["state"] == message["state"] == "indeterminate"
        assert key["packet_hash"] == message["packet_hash"] == "AB" * 8
        assert key["expected_ack"] == message["expected_ack"] == (
            None if is_channel else 123
        )

        _post(endpoints, body, idempotency_key="prune-race")
        retry = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert retry["data"]["message_id"] == message_id
        assert retry["data"]["state"] == "indeterminate"
        assert retry["data"]["packet_hash"] == "AB" * 8
        assert retry["data"]["expected_ack"] == (None if is_channel else 123)
        assert len(bridge.sent_channels if is_channel else bridge.sent_texts) == 1

    def test_post_commit_raise_returns_stored_response_without_contradiction(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
    ):
        journal = endpoints.daemon_instance.companion_journals[_HASH]
        complete = journal.complete_outbound_send

        def complete_then_raise(*args, **kwargs):
            complete(*args, **kwargs)
            raise RuntimeError("connection finalization failed after commit")

        monkeypatch.setattr(
            journal,
            "complete_outbound_send",
            complete_then_raise,
        )
        body = {"to": _PUBKEY_HEX, "text": "committed response"}
        cherrypy.serving.response.status = 200
        _post(endpoints, body, idempotency_key="post-commit")

        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 200
        assert cherrypy.response.headers["Idempotency-Replayed"] == "true"
        assert first["success"] is True
        assert first["data"]["sent"] is True
        assert first["data"]["state"] == "transmitted"
        message_id = first["data"]["message_id"]
        key = handler.companion_idempotency_get(
            "jwt:adam:unknown",
            "post-commit",
        )
        message = handler.companion_message_get_by_id(_HASH, message_id)
        assert key["state"] == "complete"
        assert message["state"] == "transmitted"

        cherrypy.serving.response.headers = {}
        cherrypy.serving.response.status = 200
        _post(endpoints, body, idempotency_key="post-commit")
        replay = _call(endpoints.messages, companion_name=_NAME)
        assert replay == first
        assert cherrypy.response.headers["Idempotency-Replayed"] == "true"
        assert len(bridge.sent_texts) == 1

    def test_malformed_post_dispatch_result_is_durably_indeterminate(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
    ):
        async def malformed_result(pub_key, text, txt_type=0, **kwargs):
            bridge.sent_texts.append((pub_key, text, txt_type))
            bridge._capture_hash()
            return SimpleNamespace(
                success=True,
                expected_ack=0x1234,
                error=None,
                # Missing is_flood: response shaping must fail closed after RF.
            )

        monkeypatch.setattr(bridge, "send_text_message", malformed_result)
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})

        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 503
        assert first["data"] == {
            "message_id": first["data"]["message_id"],
            "state": "indeterminate",
            "packet_hash": "AB" * 8,
        }
        stored = handler.companion_idempotency_get("jwt:adam:unknown", "idem-1")
        assert stored["state"] == "indeterminate"
        message = handler.companion_message_get_by_id(
            _HASH,
            first["data"]["message_id"],
        )
        assert message["state"] == "indeterminate"

        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"})
        replay = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert replay["data"]["state"] == "indeterminate"
        assert len(bridge.sent_texts) == 1

    def test_unpersisted_indeterminate_stays_visible_and_repairs_without_resend(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
    ):
        journal = endpoints.daemon_instance.companion_journals[_HASH]
        real_mark = journal.mark_outbound_send_indeterminate
        storage = {"available": False}

        def fail_finalization(*_args, **_kwargs):
            raise RuntimeError("finalization storage unavailable")

        def mark_when_available(*args, **kwargs):
            if not storage["available"]:
                raise RuntimeError("compensation storage unavailable")
            return real_mark(*args, **kwargs)

        monkeypatch.setattr(journal, "complete_outbound_send", fail_finalization)
        monkeypatch.setattr(
            journal,
            "mark_outbound_send_indeterminate",
            mark_when_available,
        )
        body = {"to": _PUBKEY_HEX, "text": "hello"}
        _post(endpoints, body)

        first = _call(endpoints.messages, companion_name=_NAME)

        assert cherrypy.serving.response.status == 503
        assert first["data"]["state"] == "indeterminate"
        message_id = first["data"]["message_id"]
        assert (
            handler.companion_idempotency_get(
                "jwt:adam:unknown",
                "idem-1",
            )["state"]
            == "pending"
        )
        assert (
            handler.companion_message_get_by_id(
                _HASH,
                message_id,
            )["state"]
            == "pending"
        )

        # A same-process retry remains explicitly indeterminate even while
        # compensation storage is still down; it never reaches RF again.
        _post(endpoints, body)
        retry = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert retry["data"]["state"] == "indeterminate"
        assert len(bridge.sent_texts) == 1

        # The next same-key lookup repairs both durable rows when storage
        # returns, while preserving the same fail-closed client result.
        storage["available"] = True
        _post(endpoints, body)
        repaired = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert repaired["data"]["state"] == "indeterminate"
        assert (
            handler.companion_idempotency_get(
                "jwt:adam:unknown",
                "idem-1",
            )["state"]
            == "indeterminate"
        )
        assert (
            handler.companion_message_get_by_id(
                _HASH,
                message_id,
            )["state"]
            == "indeterminate"
        )
        assert len(bridge.sent_texts) == 1

    def test_unpersisted_indeterminate_blocks_resend_if_reservation_disappears(
        self,
        endpoints,
        handler,
        bridge,
        monkeypatch,
    ):
        journal = endpoints.daemon_instance.companion_journals[_HASH]

        def fail_finalization(*_args, **_kwargs):
            raise RuntimeError("finalization storage unavailable")

        def fail_compensation(*_args, **_kwargs):
            raise RuntimeError("compensation storage unavailable")

        monkeypatch.setattr(journal, "complete_outbound_send", fail_finalization)
        monkeypatch.setattr(
            journal,
            "mark_outbound_send_indeterminate",
            fail_compensation,
        )
        body = {"to": _PUBKEY_HEX, "text": "hello"}
        _post(endpoints, body)
        first = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 503
        assert first["data"]["state"] == "indeterminate"
        assert len(bridge.sent_texts) == 1

        # Simulate a live database replacement/restore that lost the pending
        # reservation while this process still remembers the RF ambiguity.
        with handler._connect() as conn:
            conn.execute(
                """
                DELETE FROM companion_idempotency
                WHERE device_id = ? AND idempotency_key = ?
                """,
                ("jwt:adam:unknown", "idem-1"),
            )
            conn.commit()

        _post(endpoints, body)
        retry = _call(endpoints.messages, companion_name=_NAME)
        assert cherrypy.serving.response.status == 409
        assert retry["data"]["state"] == "indeterminate"
        assert len(bridge.sent_texts) == 1

    def test_jwt_caller_fallback_scope_key(self, endpoints, handler, bridge):
        """JWT callers (no token_id) scope idempotency by 'user:{username}'."""
        cherrypy.serving.request.user = {
            "username": "adam",
            "auth_type": "jwt",
            "scope": "admin",
        }
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key="jwt-key")
        _call(endpoints.messages, companion_name=_NAME)

        stored = handler.companion_idempotency_get("jwt:adam:unknown", "jwt-key")
        assert stored is not None

    def test_api_token_caller_scopes_by_device_id(self, endpoints, handler, bridge):
        token_id = handler.create_api_token("phone", "hash-1")
        companion_identity = _FakeIdentity().get_public_key().hex()
        assert (
            handler.companion_device_create(
                _HASH,
                "device-xyz",
                "Phone",
                token_id,
                companion_identity=companion_identity,
            )
            is not None
        )

        cherrypy.serving.request.user = {"auth_type": "api_token", "token_id": token_id}
        _post(endpoints, {"to": _PUBKEY_HEX, "text": "hello"}, idempotency_key="dev-key")
        _call(endpoints.messages, companion_name=_NAME)

        stored = handler.companion_idempotency_get(
            f"device:{companion_identity}:device-xyz",
            "dev-key",
        )
        assert stored is not None

    def test_repaired_same_device_replays_lost_retry_without_second_rf_send(
        self,
        endpoints,
        handler,
        bridge,
    ):
        companion_identity = _FakeIdentity().get_public_key().hex()
        first_token = handler.create_api_token("phone-1", "hash-1")
        assert (
            handler.companion_device_create(
                _HASH,
                "stable-device",
                "Phone",
                first_token,
                companion_identity=companion_identity,
            )
            is not None
        )
        body = {"to": _PUBKEY_HEX, "text": "lost response"}
        cherrypy.serving.request.user = {
            "auth_type": "api_token",
            "token_id": first_token,
        }
        _post(endpoints, body, idempotency_key="stable-key")
        first = _call(endpoints.messages, companion_name=_NAME)
        assert len(bridge.sent_texts) == 1

        assert handler.companion_revoke_device(device_id="stable-device") == {
            "devices_deleted": 1,
            "tokens_deleted": 1,
        }
        second_token = handler.create_api_token("phone-2", "hash-2")
        assert (
            handler.companion_device_create(
                _HASH,
                "stable-device",
                "Phone",
                second_token,
                companion_identity=companion_identity,
            )
            is not None
        )
        cherrypy.serving.request.user = {
            "auth_type": "api_token",
            "token_id": second_token,
        }
        _post(endpoints, body, idempotency_key="stable-key")
        replay = _call(endpoints.messages, companion_name=_NAME)

        assert replay == first
        assert cherrypy.response.headers["Idempotency-Replayed"] == "true"
        assert len(bridge.sent_texts) == 1


# --- Method gating on /messages -----------------------------------------------


class TestMessagesMethodGating:
    def test_get_still_serves_history(self, endpoints, handler):
        handler.companion_push_message(
            _HASH,
            {
                "text": "hello",
                "timestamp": 1,
                "packet_hash": "ABCDEF0123456789",
            },
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

    def test_login_rejects_nul_password_before_admission_or_radio(
        self,
        endpoints,
        bridge,
        monkeypatch,
    ):
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {}
        monkeypatch.setattr(
            endpoints,
            "_get_json_body",
            lambda: {"password": "visible\x00hidden"},
        )

        def unexpected_admission():
            raise AssertionError("invalid credentials must not consume RF admission")

        monkeypatch.setattr(endpoints, "_admit_rf", unexpected_admission)

        with pytest.raises(cherrypy.HTTPError) as exc:
            endpoints.login.__wrapped__(
                endpoints,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )

        assert exc.value.status == 400
        assert bridge.logins == []

    def test_connection_reports_remote_login_session(self, endpoints, bridge):
        pub_key = bytes.fromhex(_PUBKEY_HEX)
        bridge.login_connections.add(pub_key)

        cherrypy.serving.request.method = "GET"
        result = endpoints.connection.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )

        assert result == {"success": True, "data": {"connected": True}}
        assert cherrypy.response.headers["Cache-Control"] == "no-store"

    def test_logout_ends_session_and_sends_once(self, endpoints, bridge):
        pub_key = bytes.fromhex(_PUBKEY_HEX)
        bridge.login_connections.add(pub_key)
        _post(endpoints, {}, idempotency_key=None)

        result = endpoints.logout.__wrapped__(
            endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
        )

        assert result == {
            "success": True,
            "data": {"logged_out": True, "sent": True},
        }
        assert bridge.logouts == [pub_key]
        assert bridge.has_login_connection(pub_key) is False

    def test_empty_action_body_needs_no_content_type(self, endpoints, bridge):
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {"Content-Length": "0"}
        cherrypy.serving.request.body = io.BytesIO(b"")
        endpoints._get_json_body = CompanionsV1._get_json_body.__get__(
            endpoints,
            CompanionsV1,
        )

        result = endpoints.logout.__wrapped__(
            endpoints,
            companion_name=_NAME,
            contact_pubkey=_PUBKEY_HEX,
        )

        assert result["data"] == {"logged_out": True, "sent": True}
        assert bridge.logouts == [bytes.fromhex(_PUBKEY_HEX)]

    def test_logout_rejects_body_fields(self, endpoints, bridge):
        _post(endpoints, {"password": "not-used"}, idempotency_key=None)

        with pytest.raises(cherrypy.HTTPError) as exc:
            endpoints.logout.__wrapped__(
                endpoints, companion_name=_NAME, contact_pubkey=_PUBKEY_HEX
            )

        assert exc.value.status == 400
        assert bridge.logouts == []

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


class TestContactMutationValidation:
    def test_contact_rejects_core_non_contact_advert_type(self, endpoints):
        _post(endpoints, {"adv_type": 0})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.contact,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )

        assert exc.value.status == 400

    @pytest.mark.parametrize(
        "body",
        [
            {"adv_type": True},
            {"adv_type": 1.0},
            {"gps_lat": True},
            {"gps_lat": "47.5"},
            {"gps_lon": False},
            {"gps_lon": "-122.3"},
        ],
    )
    def test_contact_numeric_fields_do_not_coerce_json_types(self, endpoints, body):
        _post(endpoints, body)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.contact,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )
        assert exc.value.status == 400


class TestDurableContactMutations:
    @staticmethod
    def _real_endpoints(handler):
        identity = LocalIdentity()
        hash_byte = identity.get_public_key()[0]
        hash_name = f"0x{hash_byte:02x}"
        bridge = RepeaterCompanionBridge(
            identity,
            AsyncMock(return_value=True),
            companion_hash=hash_name,
        )
        journal = CompanionEventJournal(handler, hash_name)
        daemon = SimpleNamespace(
            identity_manager=SimpleNamespace(
                get_identities_by_type=lambda kind: (
                    [(_NAME, identity, {})] if kind == "companion" else []
                )
            ),
            companion_bridges={hash_byte: bridge},
            companion_journals={hash_name: journal},
            repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
        )
        endpoints = CompanionsV1(
            daemon_instance=daemon,
            config={},
            event_loop=_SyncLoop(),
        )
        endpoints._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
        return endpoints, bridge, hash_name

    def test_update_preserves_raw_advert_and_notifies_after_commit(self, handler):
        endpoints, bridge, companion_hash = self._real_endpoints(handler)
        raw_advert = b"raw-advert"
        bridge.add_update_contact(
            Contact(
                public_key=bytes.fromhex(_PUBKEY_HEX),
                name="Alice",
                adv_type=1,
                sync_since=77,
                last_advert_packet=raw_advert,
            )
        )
        committed = []
        bridge.add_observer(
            "contact_committed",
            lambda change, contact: committed.append((change, contact)),
        )
        _post(endpoints, {"name": "Alice Updated", "favorite": True})

        result = _call(
            endpoints.contact,
            companion_name=_NAME,
            contact_pubkey=_PUBKEY_HEX,
        )

        assert result["success"] is True
        contact = bridge.get_contact_by_key(bytes.fromhex(_PUBKEY_HEX))
        assert contact.last_advert_packet == raw_advert
        assert contact.sync_since == 77
        stored = handler.companion_load_contacts_strict(companion_hash)[0]
        assert stored["last_advert_packet"] == raw_advert
        assert stored["sync_since"] == 77
        assert committed[0][0] == "update"
        assert committed[0][1]["name"] == "Alice Updated"

    def test_new_contact_requires_name_and_defaults_to_chat_type(self, handler):
        endpoints, bridge, _companion_hash = self._real_endpoints(handler)
        for body in ({}, {"name": ""}, {"name": "   "}):
            _post(endpoints, body)
            with pytest.raises(cherrypy.HTTPError) as exc:
                _call(
                    endpoints.contact,
                    companion_name=_NAME,
                    contact_pubkey=_PUBKEY_HEX,
                )
            assert exc.value.status == 400

        _post(endpoints, {"name": "Alice"})
        result = _call(
            endpoints.contact,
            companion_name=_NAME,
            contact_pubkey=_PUBKEY_HEX,
        )
        assert result["success"] is True
        assert bridge.get_contact_by_key(bytes.fromhex(_PUBKEY_HEX)).adv_type == 1

    def test_upsert_promotes_transient_route_as_new_visible_contact(self, handler):
        endpoints, bridge, companion_hash = self._real_endpoints(handler)
        pub_key = bytes.fromhex(_PUBKEY_HEX)
        bridge.contacts.add_transient(
            Contact(
                public_key=pub_key,
                name="",
                adv_type=ADV_TYPE_NONE,
                out_path_len=1,
                out_path=b"\x42",
                last_advert_timestamp=77,
            )
        )
        assert bridge.get_contacts() == []

        _post(endpoints, {"name": "Alice"})
        result = _call(
            endpoints.contact,
            companion_name=_NAME,
            contact_pubkey=_PUBKEY_HEX,
        )

        assert result["data"]["contact"]["adv_type"] == ADV_TYPE_CHAT
        promoted = bridge.get_contact_by_key(pub_key)
        assert promoted.out_path_len == 1
        assert promoted.out_path == b"\x42"
        assert promoted.last_advert_timestamp == 77
        assert bridge.get_contacts() == [promoted]
        stored = handler.companion_load_contacts_strict(companion_hash)
        assert stored[0]["adv_type"] == ADV_TYPE_CHAT
        events = handler.companion_get_events(companion_hash, 0)
        assert events[-1]["payload"]["change"] == "new"

    def test_failed_transient_promotion_restores_internal_route(
        self,
        handler,
        monkeypatch,
    ):
        endpoints, bridge, companion_hash = self._real_endpoints(handler)
        pub_key = bytes.fromhex(_PUBKEY_HEX)
        bridge.contacts.add_transient(
            Contact(
                public_key=pub_key,
                name="",
                adv_type=ADV_TYPE_NONE,
                out_path_len=1,
                out_path=b"\x42",
            )
        )
        journal = endpoints._get_journal(companion_hash)

        def fail_store(*_args):
            raise RuntimeError("disk unavailable")

        monkeypatch.setattr(journal, "store_contact", fail_store)
        _post(endpoints, {"name": "Alice"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.contact,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )

        assert exc.value.status == 503
        restored = bridge.get_contact_by_key(pub_key)
        assert restored.adv_type == ADV_TYPE_NONE
        assert restored.out_path == b"\x42"
        assert handler.companion_load_contacts_strict(companion_hash) == []
        assert handler.companion_get_events(companion_hash, 0) == []

    @pytest.mark.parametrize("action", ["reset_path", "delete"])
    def test_transient_contact_is_not_publicly_mutable(self, handler, action):
        endpoints, bridge, companion_hash = self._real_endpoints(handler)
        pub_key = bytes.fromhex(_PUBKEY_HEX)
        transient = Contact(
            public_key=pub_key,
            name="",
            adv_type=ADV_TYPE_NONE,
            out_path_len=1,
            out_path=b"\x42",
        )
        bridge.contacts.add_transient(transient)

        if action == "reset_path":
            _post(endpoints, {})
            endpoint = endpoints.reset_path
        else:
            cherrypy.serving.request.method = "DELETE"
            endpoint = endpoints.contact

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoint,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )

        assert exc.value.status == 404
        assert bridge.get_contact_by_key(pub_key) is transient
        assert handler.companion_load_contacts_strict(companion_hash) == []
        assert handler.companion_get_events(companion_hash, 0) == []

    @pytest.mark.parametrize("name", ["Alice\x00Admin", "Alice\nAdmin"])
    def test_contact_name_rejects_control_characters(self, handler, name):
        endpoints, bridge, _companion_hash = self._real_endpoints(handler)
        _post(endpoints, {"name": name})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.contact,
                companion_name=_NAME,
                contact_pubkey=_PUBKEY_HEX,
            )

        assert exc.value.status == 400
        assert bridge.get_contact_by_key(bytes.fromhex(_PUBKEY_HEX)) is None


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
        for action in (
            "login",
            "connection",
            "logout",
            "status_request",
            "telemetry_request",
            "reset_path",
        ):
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
