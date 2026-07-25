import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass, replace
from enum import Enum
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from openhop_core.companion.constants import (
    CMD_SEND_LOGIN,
    CMD_SEND_STATUS_REQ,
    CMD_SEND_TELEMETRY_REQ,
    ERR_CODE_FILE_IO_ERROR,
    ERR_CODE_TABLE_FULL,
    MAX_PENDING_ACK_CRCS,
    PUSH_CODE_MSG_WAITING,
    RESP_CODE_ERR,
    RESP_CODE_NO_MORE_MESSAGES,
)
from openhop_core.companion.models import Contact, MessageEvent, QueuedMessage, SentResult
from openhop_core.node.handlers.control import ControlHandler
from openhop_core.protocol import LocalIdentity

from repeater.companion.bridge import (
    ChannelTextCapacityError,
    OutboundMessageEvent,
    RepeaterCompanionBridge,
    _to_json_safe,
    outbound_message_id,
    outbound_message_source,
)
from repeater.companion.frame_server import (
    CompanionFrameServer,
    _BaseFrameServer,
    _is_loopback_bind_address,
)
from repeater.companion.correlation import (
    injected_tx_outcome,
    outbound_send_capture,
)
from repeater.companion.journal import CompanionEventJournal
from repeater.companion.utils import (
    normalize_companion_identity_key,
    validate_companion_node_name,
    validate_companion_registration_name,
)
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.handler_helpers.discovery import DiscoveryHelper
from repeater.main import RepeaterDaemon


class _Mode(Enum):
    A = "a"


@dataclass
class _Dc:
    n: int
    b: bytes


def test_to_json_safe_handles_enums_bytes_collections_and_dataclass():
    payload = {
        "enum": _Mode.A,
        "bytes": b"\x01\x02",
        "tuple": (1, _Mode.A, b"x"),
        "dc": _Dc(3, b"\xff"),
        "nested": {"k": _Mode.A},
    }

    out = _to_json_safe(payload)
    assert out["enum"] == "a"
    assert out["bytes"] == "0102"
    assert out["tuple"] == [1, "a", "78"]
    assert out["dc"] == {"n": 3, "b": "ff"}
    assert out["nested"]["k"] == "a"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_to_json_safe_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="non-finite"):
        _to_json_safe({"value": value})


def test_bridge_preserves_upstream_positional_constructor_prefix():
    from openhop_core.companion import CompanionBridge

    upstream = list(inspect.signature(CompanionBridge.__init__).parameters)
    repeater = list(
        inspect.signature(RepeaterCompanionBridge.__init__).parameters
    )

    assert repeater[: len(upstream)] == upstream


def test_frame_server_accepts_upstream_device_metadata_overrides():
    server = CompanionFrameServer(
        SimpleNamespace(),
        "0x01",
        port=0,
        device_model="custom-model",
        device_version="1.2.3",
        build_date="",
        heartbeat_interval=7,
    )

    assert server._model_bytes.rstrip(b"\0") == b"custom-model"
    assert server._version_bytes.rstrip(b"\0") == b"1.2.3"
    assert server._build_date_bytes.rstrip(b"\0") == b""
    assert server._heartbeat_interval == 7


@pytest.mark.asyncio
async def test_frame_server_defaults_to_loopback_and_warns_on_external_bind(caplog):
    assert _is_loopback_bind_address("127.0.0.1")
    assert _is_loopback_bind_address("::1")
    assert _is_loopback_bind_address("localhost")
    assert not _is_loopback_bind_address("0.0.0.0")

    default_server = CompanionFrameServer(SimpleNamespace(), "0x01")
    assert default_server.bind_address == "127.0.0.1"

    exposed_server = CompanionFrameServer(
        SimpleNamespace(),
        "0x01",
        bind_address="0.0.0.0",
    )
    with patch(
        "repeater.companion.frame_server._BaseFrameServer.start",
        new=AsyncMock(),
    ):
        await exposed_server.start()

    assert "SECURITY: companion frame TCP" in caplog.text
    assert "not authenticated" in caplog.text


def test_bridge_save_prefs_persists_and_calls_callback():
    @dataclass
    class _Prefs:
        node_name: str
        retry: int

    sqlite = SimpleNamespace(companion_save_prefs=MagicMock())
    callback = MagicMock()

    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._sqlite_handler = sqlite
    bridge._companion_hash = "abc123"
    bridge._on_prefs_saved = callback
    bridge.prefs = cast(Any, _Prefs(node_name="node-1", retry=2))

    bridge._save_prefs()

    sqlite.companion_save_prefs.assert_called_once()
    args = sqlite.companion_save_prefs.call_args[0]
    assert args[0] == "abc123"
    assert args[1]["node_name"] == "node-1"
    callback.assert_called_once_with("node-1")


def test_bridge_load_prefs_rejects_known_field_type_coercion():
    stored = {"node_name": "new-name", "path_hash_mode": "2"}
    sqlite = SimpleNamespace(companion_load_prefs=lambda _h: stored)

    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._sqlite_handler = sqlite
    bridge._companion_hash = "hash"
    bridge.prefs = cast(
        Any,
        SimpleNamespace(
            node_name="orig",
            path_hash_mode=0,
            default_scope_name="",
            default_scope_key=b"",
        ),
    )

    with pytest.raises(ValueError, match="path_hash_mode.*integer"):
        bridge._load_prefs()


def test_bridge_load_prefs_ignores_invalid_or_missing_backend():
    @dataclass
    class _Prefs:
        node_name: str = "orig"

    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._sqlite_handler = None
    bridge._companion_hash = ""
    bridge.prefs = cast(Any, _Prefs())
    bridge._load_prefs()
    assert bridge.prefs.node_name == "orig"


@pytest.mark.asyncio
async def test_logins_with_same_destination_hash_are_serialized():
    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._state_mutation_lock = asyncio.Lock()
    bridge.contacts = SimpleNamespace(
        get_proxy_by_key=lambda _key: SimpleNamespace(dest_hash=0xAA)
    )
    bridge._login_locks = {}
    bridge._spawn_background_task = (
        lambda coro, _label: asyncio.get_running_loop().create_task(coro)
    )
    loop = asyncio.get_running_loop()
    first_result = loop.create_future()
    second_result = loop.create_future()
    base_start = AsyncMock(
        side_effect=[
            {"success": True, "task": first_result},
            {"success": True, "task": second_result},
        ]
    )
    base_bridge = RepeaterCompanionBridge.__mro__[1]

    with patch.object(base_bridge, "_start_login_request", new=base_start):
        first = await bridge._start_login_request(b"\xAA" + b"\x01" * 31, "one")
        second_start = asyncio.create_task(
            bridge._start_login_request(b"\xAA" + b"\x02" * 31, "two")
        )
        await asyncio.sleep(0)
        assert base_start.await_count == 1
        assert not second_start.done()

        first_result.set_result({"success": True})
        assert await first["task"] == {"success": True}
        second = await second_start
        assert base_start.await_count == 2

        second_result.set_result({"success": True})
        assert await second["task"] == {"success": True}


@pytest.mark.asyncio
async def test_logout_waits_for_login_and_clears_session_before_rf_send():
    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._state_mutation_lock = asyncio.Lock()
    pubkey = b"\xAA" + b"\x03" * 31
    bridge.contacts = SimpleNamespace(
        get_proxy_by_key=lambda _key: SimpleNamespace(dest_hash=0xAA)
    )
    login_lock = asyncio.Lock()
    await login_lock.acquire()
    bridge._login_locks = {0xAA: login_lock}
    bridge._login_connections = {pubkey: float("inf")}
    observed = []
    base_bridge = RepeaterCompanionBridge.__mro__[1]

    async def base_logout(instance, key):
        observed.append((key, instance.has_login_connection(key)))
        return False

    with patch.object(base_bridge, "send_logout", new=base_logout):
        logout = asyncio.create_task(bridge.send_logout(pubkey))
        await asyncio.sleep(0)
        assert logout.done() is False
        assert bridge.has_login_connection(pubkey) is True

        login_lock.release()
        assert await logout is False

    assert observed == [(pubkey, False)]
    assert bridge.has_login_connection(pubkey) is False


@pytest.mark.asyncio
async def test_status_and_telemetry_for_same_contact_are_serialized():
    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._state_mutation_lock = asyncio.Lock()
    bridge._protocol_request_locks = {}
    bridge._spawn_background_task = (
        lambda coro, _label: asyncio.get_running_loop().create_task(coro)
    )
    loop = asyncio.get_running_loop()
    frame_result = loop.create_future()
    rest_result = loop.create_future()
    base_start = AsyncMock(
        side_effect=[
            {"success": True, "task": frame_result},
            {"success": True, "task": rest_result},
        ]
    )
    base_bridge = RepeaterCompanionBridge.__mro__[1]
    pubkey = b"\xBB" * 32

    with patch.object(base_bridge, "_start_protocol_request", new=base_start):
        frame = await bridge._start_protocol_request(
            pubkey,
            1,
            b"",
            timeout=15.0,
            log_label="frame status",
        )
        rest_start = asyncio.create_task(
            bridge._start_protocol_request(
                pubkey,
                2,
                b"\x00",
                timeout=20.0,
                log_label="REST telemetry",
            )
        )
        await asyncio.sleep(0)
        assert base_start.await_count == 1
        assert not rest_start.done()

        frame_result.set_result({"surface": "frame"})
        assert await frame["task"] == {"surface": "frame"}
        rest = await rest_start
        assert base_start.await_count == 2

        rest_result.set_result({"surface": "rest"})
        assert await rest["task"] == {"surface": "rest"}


@pytest.mark.asyncio
async def test_parallel_rest_login_neither_steals_nor_pushes_frame_result():
    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._state_mutation_lock = asyncio.Lock()
    bridge._login_locks = {}
    pubkey = b"\xBC" * 32
    bridge.contacts = SimpleNamespace(
        get_proxy_by_key=lambda key: (
            SimpleNamespace(dest_hash=pubkey[0]) if key == pubkey else None
        )
    )
    bridge._spawn_background_task = (
        lambda coro, _label: asyncio.get_running_loop().create_task(coro)
    )
    frame_result = asyncio.get_running_loop().create_future()
    rest_result = asyncio.get_running_loop().create_future()
    sent = SentResult(
        success=True,
        is_flood=False,
        expected_ack=17,
        timeout_ms=1000,
    )
    base_start = AsyncMock(
        side_effect=[
            {"success": True, "sent": sent, "task": frame_result},
            {"success": True, "sent": sent, "task": rest_result},
        ]
    )
    base_bridge = RepeaterCompanionBridge.__mro__[1]
    server = CompanionFrameServer(bridge, "0x01", port=0)
    session = object()
    server._active_client_session = session
    server._write_queue = asyncio.Queue()
    current_task = asyncio.current_task()
    server._client_sessions[current_task] = session

    with (
        patch.object(base_bridge, "_start_frame_login_request", new=base_start),
        patch.object(base_bridge, "_start_login_request", new=base_start),
    ):
        try:
            await server._handle_cmd(bytes([CMD_SEND_LOGIN]) + pubkey + b"frame")
        finally:
            server._client_sessions.pop(current_task, None)
        assert server._write_queue.qsize() == 1  # Frame SENT

        rest_call = asyncio.create_task(bridge.send_login(pubkey, "rest"))
        await asyncio.sleep(0)
        assert base_start.await_count == 2
        assert not rest_call.done()

        frame_result.set_result(
            {
                "success": True,
                "timeout": False,
                "is_admin": False,
                "tag": 9,
                "acl_permissions": 0,
                "firmware_ver_level": 13,
            }
        )
        for _ in range(10):
            if server._write_queue.qsize() == 2 and base_start.await_count == 2:
                break
            await asyncio.sleep(0)
        assert server._write_queue.qsize() == 2  # SENT + Frame login result
        assert base_start.await_count == 2

        rest_result.set_result({"success": True, "surface": "rest"})
        assert await rest_call == {"success": True, "surface": "rest"}
        await asyncio.sleep(0)
        assert server._write_queue.qsize() == 2


@pytest.mark.asyncio
async def test_parallel_rest_telemetry_neither_steals_nor_pushes_frame_status():
    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._state_mutation_lock = asyncio.Lock()
    bridge._protocol_request_locks = {}
    bridge._spawn_background_task = (
        lambda coro, _label: asyncio.get_running_loop().create_task(coro)
    )
    pubkey = b"\xBD" * 32
    frame_result = asyncio.get_running_loop().create_future()
    rest_result = asyncio.get_running_loop().create_future()
    sent = SentResult(
        success=True,
        is_flood=False,
        expected_ack=18,
        timeout_ms=1000,
    )
    base_start = AsyncMock(
        side_effect=[
            {"success": True, "sent": sent, "task": frame_result},
            {"success": True, "sent": sent, "task": rest_result},
        ]
    )
    base_bridge = RepeaterCompanionBridge.__mro__[1]
    server = CompanionFrameServer(bridge, "0x01", port=0)
    session = object()
    server._active_client_session = session
    server._write_queue = asyncio.Queue()
    current_task = asyncio.current_task()
    server._client_sessions[current_task] = session

    with patch.object(base_bridge, "_start_protocol_request", new=base_start):
        try:
            await server._handle_cmd(bytes([CMD_SEND_STATUS_REQ]) + pubkey)
        finally:
            server._client_sessions.pop(current_task, None)
        assert server._write_queue.qsize() == 1  # Frame SENT

        rest_call = asyncio.create_task(
            bridge.send_telemetry_request(pubkey, timeout=20.0)
        )
        await asyncio.sleep(0)
        assert base_start.await_count == 1
        assert not rest_call.done()

        frame_result.set_result(
            {"success": True, "stats": {"raw_bytes": b"\x01\x02"}}
        )
        for _ in range(10):
            if server._write_queue.qsize() == 2 and base_start.await_count == 2:
                break
            await asyncio.sleep(0)
        assert server._write_queue.qsize() == 2  # SENT + Frame status result
        assert base_start.await_count == 2

        rest_result.set_result(
            {
                "success": True,
                "telemetry_data": {"raw_bytes": b"\x03\x04"},
                "surface": "rest",
            }
        )
        assert (await rest_call)["surface"] == "rest"
        await asyncio.sleep(0)
    assert server._write_queue.qsize() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tx_outcome", "expected_state", "has_hash"),
    [
        ({"accepted": True}, "transmitted", True),
        ({"uncertain": True}, "indeterminate", True),
        ({"accepted": False}, "failed", False),
    ],
)
async def test_bridge_exposes_false_injector_outcome_to_rest_capture(
    tx_outcome,
    expected_state,
    has_hash,
):
    async def _inject(_packet, **_kwargs):
        injected_tx_outcome.get().update(tx_outcome)
        return False

    packet = SimpleNamespace(
        calculate_packet_hash=lambda: bytes.fromhex("AB" * 32),
    )
    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    capture = {}
    token = outbound_send_capture.set(capture)
    try:
        assert not await bridge._send_packet(packet, expected_crc=321)
    finally:
        outbound_send_capture.reset(token)

    assert capture["initial_state"] == expected_state
    assert capture["expected_ack"] == 321
    assert ("hash" in capture) is has_hash


@pytest.mark.asyncio
async def test_channel_send_emits_one_semantic_outbound_event():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(), _inject, companion_hash="0x01"
    )
    events = []
    bridge.add_observer("message_sent", events.append)
    assert bridge.set_channel(1, "test", bytes(32))

    assert await bridge.send_channel_message(1, "hello", timestamp=123)

    assert len(events) == 1
    event = events[0]
    assert event.companion_hash == "0x01"
    assert event.is_channel is True
    assert event.channel_idx == 1
    assert event.text == "hello"
    assert event.timestamp == 123
    assert event.packet_hash
    assert event.source == "frame"
    assert event.result is True


@pytest.mark.asyncio
async def test_send_waits_for_committed_state_without_holding_lock_during_rf():
    injected = asyncio.Event()
    bridge = None

    async def _inject(_packet, **_kwargs):
        assert bridge is not None
        assert not bridge.state_mutation_lock.locked()
        injected.set()
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    assert bridge.set_channel(1, "test", bytes(32))

    await bridge.state_mutation_lock.acquire()
    task = asyncio.create_task(bridge.send_channel_message(1, "hello"))
    await asyncio.sleep(0)
    assert not injected.is_set()

    bridge.state_mutation_lock.release()
    assert await task is True
    assert injected.is_set()


@pytest.mark.asyncio
async def test_rest_channel_send_revalidates_capacity_after_committed_rename():
    injected = []

    async def _inject(packet, **_kwargs):
        injected.append(packet)
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject, node_name="A")
    assert bridge.set_channel(1, "test", bytes(32))
    text = "x" * 128

    await bridge.state_mutation_lock.acquire()
    source_token = outbound_message_source.set("rest")
    try:
        task = asyncio.create_task(bridge.send_channel_message(1, text))
    finally:
        outbound_message_source.reset(source_token)
    await asyncio.sleep(0)
    assert not task.done()

    # Simulate the committed end of a parallel Frame name command. The old
    # name allowed 157 bytes; the 31-byte name allows only 127.
    bridge.prefs.node_name = "N" * 31
    bridge.state_mutation_lock.release()

    with pytest.raises(ChannelTextCapacityError) as exc:
        await task
    assert exc.value.max_bytes == 127
    assert injected == []


@pytest.mark.asyncio
async def test_frame_channel_send_keeps_upstream_truncation_behavior():
    injected = []

    async def _inject(packet, **_kwargs):
        injected.append(packet)
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        node_name="N" * 31,
    )
    assert bridge.set_channel(1, "test", bytes(32))

    # Core/firmware channel sends truncate over-capacity text. The REST
    # fail-closed guard must not change the established Frame contract.
    assert await bridge.send_channel_message(1, "x" * 128)
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_rest_send_source_flows_into_semantic_event():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    bridge.set_channel(1, "test", bytes(32))
    events = []
    bridge.add_observer("message_sent", events.append)

    source_token = outbound_message_source.set("rest")
    message_token = outbound_message_id.set(91)
    try:
        assert await bridge.send_channel_message(1, "hello")
    finally:
        outbound_message_id.reset(message_token)
        outbound_message_source.reset(source_token)

    assert events[0].source == "rest"
    assert events[0].message_id == 91


@pytest.mark.asyncio
async def test_ack_emits_packet_correlated_confirmation_event():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject, companion_hash="0x01")
    confirmations = []
    bridge.add_observer("message_confirmed", confirmations.append)
    outbound = OutboundMessageEvent(
        companion_hash="0x01",
        packet_hash="AB" * 32,
        text="hello",
        timestamp=1,
        is_channel=False,
        recipient_key=b"\x02" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=77,
        source="frame",
        message_id=None,
        result=True,
    )
    await bridge._record_outbound_message(outbound)

    await bridge._fire_callbacks("send_confirmed", 77, 123)

    assert len(confirmations) == 1
    confirmation = confirmations[0]
    assert confirmation.packet_hash == outbound.packet_hash
    assert confirmation.expected_ack == 77
    assert confirmation.trip_ms == 123
    assert confirmation.source == "frame"


@pytest.mark.asyncio
async def test_ack_before_semantic_event_is_buffered_by_exact_send_token():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash="0x01",
    )
    confirmations = []
    bridge.add_observer("message_confirmed", confirmations.append)
    bridge._ack_tokens_by_crc[77] = 501

    await bridge._fire_callbacks("send_confirmed", 77, 123)
    assert confirmations == []

    outbound = OutboundMessageEvent(
        companion_hash="0x01",
        packet_hash="AB" * 32,
        text="hello",
        timestamp=1,
        is_channel=False,
        recipient_key=b"\x02" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=77,
        source="frame",
        message_id=None,
        result=True,
        correlation_token=501,
    )
    await bridge._record_outbound_message(outbound)

    assert len(confirmations) == 1
    assert confirmations[0].packet_hash == outbound.packet_hash
    assert confirmations[0].correlation_token == 501
    assert bridge._early_confirmations_by_token == {}


@pytest.mark.asyncio
async def test_parallel_api_acks_only_confirm_the_frame_owned_send_to_frame():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash="0x01",
    )
    frame_acks = []
    host_acks = []

    class _FrameOwner:
        def __init__(self):
            self.bridge = bridge

        def _setup_push_callbacks(self):
            pass

        def on_ack(self, crc, trip_ms):
            frame_acks.append((crc, trip_ms))

    bridge.on_send_confirmed(_FrameOwner().on_ack)
    bridge.on_send_confirmed(
        lambda crc, trip_ms: host_acks.append((crc, trip_ms))
    )

    def _event(source, token, crc):
        return OutboundMessageEvent(
            companion_hash="0x01",
            packet_hash=f"{token:064X}",
            text=source,
            timestamp=token,
            is_channel=False,
            recipient_key=b"\x02" * 32,
            channel_idx=None,
            txt_type=0,
            expected_ack=crc,
            source=source,
            message_id=token if source == "rest" else None,
            result=True,
            correlation_token=token,
        )

    await asyncio.gather(
        bridge._record_outbound_message(_event("frame", 1, 101)),
        bridge._record_outbound_message(_event("rest", 2, 202)),
        bridge._record_outbound_message(_event("operator", 3, 303)),
    )
    await bridge._fire_callbacks("send_confirmed", 202, 20)
    await bridge._fire_callbacks("send_confirmed", 101, 10)
    await bridge._fire_callbacks("send_confirmed", 303, 30)

    assert frame_acks == [(101, 10)]
    assert host_acks == [(202, 20), (101, 10), (303, 30)]


@pytest.mark.asyncio
async def test_reused_pending_ack_crc_fails_closed_and_maps_stay_bounded():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash="0x01",
    )
    confirmations = []
    frame_acks = []
    host_acks = []
    bridge.add_observer("message_confirmed", confirmations.append)

    class _FrameOwner:
        def __init__(self):
            self.bridge = bridge

        def _setup_push_callbacks(self):
            pass

        def on_ack(self, crc, trip_ms):
            frame_acks.append((crc, trip_ms))

    bridge.on_send_confirmed(_FrameOwner().on_ack)
    bridge.on_send_confirmed(
        lambda crc, trip_ms: host_acks.append((crc, trip_ms))
    )

    def _event(index, expected_ack):
        return OutboundMessageEvent(
            companion_hash="0x01",
            packet_hash=f"{index:064X}",
            text=str(index),
            timestamp=index,
            is_channel=False,
            recipient_key=b"\x02" * 32,
            channel_idx=None,
            txt_type=0,
            expected_ack=expected_ack,
            source="frame",
            message_id=None,
            result=True,
            correlation_token=index,
        )

    await bridge._record_outbound_message(_event(1, 77))
    await bridge._record_outbound_message(_event(2, 77))
    await bridge._fire_callbacks("send_confirmed", 77, 123)
    assert confirmations == []
    assert frame_acks == []
    assert host_acks == [(77, 123)]

    for index in range(10, 10 + MAX_PENDING_ACK_CRCS + 5):
        await bridge._record_outbound_message(_event(index, index))

    assert len(bridge._outbound_by_ack) <= MAX_PENDING_ACK_CRCS
    assert len(bridge._ack_tokens_by_crc) <= MAX_PENDING_ACK_CRCS


@pytest.mark.asyncio
async def test_main_persists_frame_send_and_correlated_ack_only():
    async def _inject(_packet, **_kwargs):
        return True

    tracker = MagicMock()
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        _inject,
        companion_hash="0x01",
        tracker=tracker,
    )
    journal = SimpleNamespace(
        store_outbound_message=MagicMock(return_value={"message_id": 42}),
        update_outbound_state=MagicMock(return_value={}),
    )
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)
    frame_event = OutboundMessageEvent(
        companion_hash="0x01",
        packet_hash="AB" * 32,
        text="from frame",
        timestamp=10,
        is_channel=False,
        recipient_key=b"\x02" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=77,
        source="frame",
        message_id=None,
        result=True,
    )

    await bridge._record_outbound_message(frame_event)
    await bridge._fire_callbacks("send_confirmed", 77, 123)

    journal.store_outbound_message.assert_called_once()
    stored_message, source, state = journal.store_outbound_message.call_args.args
    assert stored_message["packet_hash"] == frame_event.packet_hash
    assert source == "frame"
    assert state == "transmitted"
    journal.update_outbound_state.assert_called_once_with(
        42, "confirmed", frame_event.packet_hash, 77
    )

    rest_event = replace(
        frame_event,
        packet_hash="CD" * 32,
        expected_ack=88,
        source="rest",
        message_id=84,
    )
    await bridge._record_outbound_message(rest_event)
    await bridge._fire_callbacks("send_confirmed", 88, 100)

    assert journal.store_outbound_message.call_count == 1
    assert journal.update_outbound_state.call_count == 2
    journal.update_outbound_state.assert_called_with(
        84, "confirmed", rest_event.packet_hash, 88
    )

    operator_event = replace(
        frame_event,
        packet_hash="EF" * 32,
        expected_ack=99,
        source="operator",
    )
    journal.store_outbound_message.return_value = {"message_id": 126}
    await bridge._record_outbound_message(operator_event)
    await bridge._fire_callbacks("send_confirmed", 99, 80)

    assert journal.store_outbound_message.call_count == 2
    stored_message, source, state = journal.store_outbound_message.call_args.args
    assert stored_message["packet_hash"] == operator_event.packet_hash
    assert source == "operator"
    assert state == "transmitted"
    assert journal.update_outbound_state.call_count == 3
    journal.update_outbound_state.assert_called_with(
        126,
        "confirmed",
        operator_event.packet_hash,
        99,
    )
    assert tracker.register_outbound.call_args_list == [
        call(frame_event.packet_hash, "0x01", None),
        call(rest_event.packet_hash, "0x01", 84),
        call(operator_event.packet_hash, "0x01", None),
    ]
    assert tracker.promote_outbound.call_args_list == [
        call(frame_event.packet_hash, "0x01", 42),
        call(operator_event.packet_hash, "0x01", 126),
    ]


@pytest.mark.asyncio
async def test_frame_ack_arriving_during_storage_is_not_lost():
    async def _inject(_packet, **_kwargs):
        return True

    storage_started = threading.Event()
    release_storage = threading.Event()

    def _store(*_args):
        storage_started.set()
        assert release_storage.wait(timeout=2)
        return {"message_id": 42}

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject, companion_hash="0x01")
    journal = SimpleNamespace(
        store_outbound_message=MagicMock(side_effect=_store),
        update_outbound_state=MagicMock(return_value={}),
    )
    RepeaterDaemon._wire_companion_history_observers(bridge, journal)
    frame_event = OutboundMessageEvent(
        companion_hash="0x01",
        packet_hash="AB" * 32,
        text="from frame",
        timestamp=10,
        is_channel=False,
        recipient_key=b"\x02" * 32,
        channel_idx=None,
        txt_type=0,
        expected_ack=77,
        source="frame",
        message_id=None,
        result=True,
    )

    store_task = asyncio.create_task(bridge._record_outbound_message(frame_event))
    assert await asyncio.to_thread(storage_started.wait, 1)
    await bridge._fire_callbacks("send_confirmed", 77, 123)
    release_storage.set()
    await store_task

    journal.update_outbound_state.assert_called_once_with(
        42,
        "confirmed",
        frame_event.packet_hash,
        77,
    )


@pytest.mark.asyncio
async def test_durable_observer_errors_do_not_break_other_observers(caplog):
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    seen = []

    def _broken(*_args):
        raise RuntimeError("observer failed")

    bridge.add_observer("send_confirmed", _broken)
    bridge.add_observer("send_confirmed", lambda crc, trip_ms: seen.append((crc, trip_ms)))

    await bridge._fire_callbacks("send_confirmed", 7, 11)

    assert seen == [(7, 11)]
    assert "observer failed" in caplog.text


@pytest.mark.asyncio
async def test_durable_observer_cancellation_is_propagated_after_other_observers():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    seen = []

    async def _cancelled(*_args):
        raise asyncio.CancelledError

    bridge.add_observer("custom", _cancelled)
    bridge.add_observer("custom", lambda value: seen.append(value))

    with pytest.raises(asyncio.CancelledError):
        await bridge.notify_observers("custom", 7)

    assert seen == [7]


@pytest.mark.asyncio
async def test_contact_observers_preserve_commit_order_while_callback_yields():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    seen = []

    async def _observer(value):
        seen.append(value)
        if value == 1:
            first_entered.set()
            await release_first.wait()

    bridge.add_observer("contact_committed", _observer)
    first = asyncio.create_task(
        bridge.notify_observers("contact_committed", 1),
    )
    await first_entered.wait()
    second = asyncio.create_task(
        bridge.notify_observers("contact_committed", 2),
    )
    await asyncio.sleep(0)

    assert seen == [1]

    release_first.set()
    await asyncio.gather(first, second)
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_contact_observers_publish_atomic_commit_as_one_ordered_batch():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    seen = []

    async def _observer(change, contact):
        seen.append((change, contact["id"]))
        if contact["id"] == 1:
            first_entered.set()
            await release_first.wait()

    bridge.add_observer("contact_committed", _observer)
    batch = asyncio.create_task(
        bridge.notify_contact_changes(
            [
                {"change": "update", "contact": {"id": 1}},
                {"change": "update", "contact": {"id": 2}},
            ]
        )
    )
    await first_entered.wait()
    later_commit = asyncio.create_task(
        bridge.notify_observers(
            "contact_committed",
            "remove",
            {"id": 3},
        )
    )
    await asyncio.sleep(0)

    assert seen == [("update", 1)]

    release_first.set()
    await asyncio.gather(batch, later_commit)
    assert seen == [("update", 1), ("update", 2), ("remove", 3)]


def test_reconnect_clear_preserves_host_callback_and_removes_frame_callback():
    bridge = RepeaterCompanionBridge.__new__(RepeaterCompanionBridge)
    bridge._push_callbacks = {"send_confirmed": []}

    def _host_callback(*_args):
        pass

    class _FrameOwner:
        def __init__(self):
            self.bridge = bridge

        def _setup_push_callbacks(self):
            pass

        def callback(self, *_args):
            pass

    frame = _FrameOwner()
    bridge._push_callbacks["send_confirmed"] = [_host_callback, frame.callback]

    bridge.clear_push_callbacks()

    assert bridge._push_callbacks["send_confirmed"] == [_host_callback]


def test_current_core_reconnect_setup_preserves_host_without_duplicates():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    server = CompanionFrameServer(bridge, "0x01", port=0)

    def _host_callback(*_args):
        pass

    bridge.on_send_confirmed(_host_callback)
    server._setup_push_callbacks()
    server._setup_push_callbacks()

    callbacks = bridge._push_callbacks["send_confirmed"]
    assert callbacks.count(_host_callback) == 1
    assert callbacks.count(server._on_send_confirmed) == 1


@pytest.mark.asyncio
async def test_reconnect_keeps_persistence_before_legacy_message_callback():
    async def _inject(_packet, **_kwargs):
        return True

    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    server = CompanionFrameServer(bridge, "0x01", port=0)
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    legacy_events = []

    async def _persist(_message, _queue_entry):
        persistence_started.set()
        await release_persistence.wait()

    bridge.on_message_received(lambda *args: legacy_events.append(args))
    server._setup_push_callbacks()
    server._setup_push_callbacks()
    server._persist_companion_message = _persist
    server._write_queue = asyncio.Queue()

    callbacks = bridge._push_callbacks["message_event"]
    assert getattr(callbacks[0], "__self__", None) is server

    event = MessageEvent(
        sender_key=b"\x01" * 32,
        text="committed first",
        timestamp=123,
        txt_type=0,
        packet_hash="AB" * 32,
        snr=1.5,
        rssi=-80,
        sender_prefix=b"\x02\x03\x04\x05",
        path_len=2,
        queued=True,
        queue_entry=object(),
    )
    firing = asyncio.create_task(bridge._fire_callbacks("message_event", event))
    await persistence_started.wait()
    assert legacy_events == []

    release_persistence.set()
    await firing

    assert legacy_events == [
        (
            event.sender_key,
            event.text,
            event.timestamp,
            event.txt_type,
            event.packet_hash,
            event.snr,
            event.rssi,
            event.sender_prefix,
            event.path_len,
            True,
        )
    ]


class _FrameTestWriter:
    def __init__(self):
        self.closed = False

    def get_extra_info(self, _name):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def is_closing(self):
        return self.closed


@pytest.mark.asyncio
async def test_frame_reconnect_waits_for_command_before_replacing_response_queue():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    server._setup_push_callbacks = MagicMock()
    old_writer = _FrameTestWriter()
    new_writer = _FrameTestWriter()
    old_queue = asyncio.Queue()
    old_session = object()
    server._client_reader = object()
    server._client_writer = old_writer
    server._write_queue = old_queue
    server._writer_task = None
    server._active_client_session = old_session

    command_started = asyncio.Event()
    release_command = asyncio.Event()
    new_session_started = asyncio.Event()
    release_new_session = asyncio.Event()

    async def _upstream_command(instance, _payload):
        command_started.set()
        await release_command.wait()
        instance._write_ok()

    async def _old_command():
        task = asyncio.current_task()
        server._client_sessions[task] = old_session
        try:
            await server._handle_cmd(b"\x01")
        finally:
            server._client_sessions.pop(task, None)

    async def _read_new_client(_reader, _writer_task):
        new_session_started.set()
        await release_new_session.wait()
        return "empty_read"

    async def _writer_loop(_writer):
        await asyncio.Future()

    with (
        patch.object(_BaseFrameServer, "_handle_cmd", new=_upstream_command),
        patch.object(server, "_read_client_frames", new=_read_new_client),
        patch.object(server, "_writer_loop", new=_writer_loop),
    ):
        command = asyncio.create_task(_old_command())
        await command_started.wait()
        reconnect = asyncio.create_task(
            server._handle_client(object(), new_writer)
        )
        await asyncio.sleep(0)

        assert server._client_writer is old_writer
        assert not new_session_started.is_set()

        release_command.set()
        await new_session_started.wait()

        assert old_queue.qsize() == 1
        assert server._client_writer is new_writer
        assert server._write_queue.empty()

        release_new_session.set()
        await reconnect
        await command


@pytest.mark.asyncio
async def test_frame_disconnect_path_retires_late_response_before_reconnect():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    server._setup_push_callbacks = MagicMock()
    writer = _FrameTestWriter()
    disconnected_session = []

    async def _read_then_disconnect(_reader, _writer_task):
        session = server._active_client_session
        disconnected_session.append(session)
        server._claim_response_session(
            "trace",
            73,
            session,
            timeout_ms=1000,
        )
        return "empty_read"

    async def _writer_loop(_writer):
        await asyncio.Future()

    with (
        patch.object(server, "_read_client_frames", new=_read_then_disconnect),
        patch.object(server, "_writer_loop", new=_writer_loop),
        patch(
            "repeater.companion.frame_server.time.monotonic",
            return_value=100.0,
        ),
    ):
        await server._handle_client(object(), writer)
        assert server._active_client_session is None
        assert server._response_session_map("trace")[73] is not disconnected_session[0]
        assert server.owns_response_tag("trace", 73)

        replacement_session = object()
        replacement_queue = asyncio.Queue()
        server._active_client_session = replacement_session
        server._write_queue = replacement_queue
        assert server._pop_active_response_session("trace", 73) is None
        assert replacement_queue.empty()


@pytest.mark.asyncio
async def test_frame_superseded_session_command_is_ignored():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    old_session = object()
    server._active_client_session = object()

    async def _stale_command():
        task = asyncio.current_task()
        server._client_sessions[task] = old_session
        try:
            await server._handle_cmd(b"\x01")
        finally:
            server._client_sessions.pop(task, None)

    with patch.object(
        _BaseFrameServer,
        "_handle_cmd",
        new_callable=AsyncMock,
    ) as upstream_command:
        await _stale_command()

    upstream_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "data", "result"),
    [
        (
            CMD_SEND_LOGIN,
            b"\x11" * 32,
            {
                "success": True,
                "tag": 7,
                "is_admin": False,
                "acl_permissions": 0,
                "firmware_ver_level": 13,
            },
        ),
        (
            CMD_SEND_STATUS_REQ,
            b"\x22" * 32,
            {"success": True, "stats": {"raw_bytes": b"\x01\x02"}},
        ),
        (
            CMD_SEND_TELEMETRY_REQ,
            b"\x00" * 3 + b"\x33" * 32,
            {"success": True, "telemetry_data": {"raw_bytes": b"\x03\x04"}},
        ),
    ],
)
async def test_delayed_request_result_stays_with_its_frame_session(
    command,
    data,
    result,
):
    class _Bridge:
        def __init__(self):
            self.response_future = None
            self.tasks = []

        def _spawn_background_task(self, coro, _label):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        async def _started(self):
            return {
                "success": True,
                "sent": SentResult(
                    success=True,
                    is_flood=False,
                    expected_ack=17,
                    timeout_ms=1000,
                ),
                "task": self.response_future,
            }

        async def _start_frame_login_request(self, *_args, **_kwargs):
            return await self._started()

        async def _start_status_request(self, *_args, **_kwargs):
            return await self._started()

        async def _start_telemetry_request(self, *_args, **_kwargs):
            return await self._started()

    bridge = _Bridge()
    server = CompanionFrameServer(bridge, "0x01", port=0)

    async def _start_command(session, queue, response_future):
        server._active_client_session = session
        server._write_queue = queue
        bridge.response_future = response_future
        task = asyncio.current_task()
        server._client_sessions[task] = session
        try:
            await server._handle_cmd(bytes([command]) + data)
        finally:
            server._client_sessions.pop(task, None)
        return bridge.tasks[-1]

    old_session = object()
    old_queue = asyncio.Queue()
    old_result = asyncio.get_running_loop().create_future()
    old_completion = await _start_command(old_session, old_queue, old_result)
    assert old_queue.qsize() == 1  # SENT

    new_session = object()
    new_queue = asyncio.Queue()
    server._active_client_session = new_session
    server._write_queue = new_queue
    old_result.set_result(result)
    await old_completion
    assert new_queue.empty()

    new_result = asyncio.get_running_loop().create_future()
    new_completion = await _start_command(new_session, new_queue, new_result)
    assert new_queue.qsize() == 1  # this session's SENT
    new_result.set_result(result)
    await new_completion
    assert new_queue.qsize() == 2  # SENT + matching completion push


@pytest.mark.asyncio
async def test_frame_stop_drains_active_command_and_rejects_later_commands():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    command_calls = 0

    async def _upstream_command(_instance, _payload):
        nonlocal command_calls
        command_calls += 1
        command_started.set()
        await release_command.wait()

    with (
        patch.object(_BaseFrameServer, "_handle_cmd", new=_upstream_command),
        patch.object(
            _BaseFrameServer,
            "stop",
            new_callable=AsyncMock,
        ) as upstream_stop,
    ):
        command = asyncio.create_task(server._handle_cmd(b"\x01"))
        await command_started.wait()
        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0)

        assert server._frame_stopping is True
        upstream_stop.assert_not_awaited()

        release_command.set()
        await command
        await stopping
        upstream_stop.assert_awaited_once()

        await server._handle_cmd(b"\x02")
        assert command_calls == 1


@pytest.mark.asyncio
async def test_frame_contact_change_notifies_durable_observer():
    public_key = bytes(range(32))
    contact = SimpleNamespace(
        public_key=public_key,
        name="Alice",
        adv_type=1,
        flags=0,
        out_path_len=0,
        out_path=b"",
        last_advert_timestamp=1,
        last_advert_packet=None,
        lastmod=2,
        gps_lat=None,
        gps_lon=None,
        sync_since=0,
    )
    bridge = SimpleNamespace(
        get_contact_by_key=lambda key: contact if key == public_key else None,
        notify_observers=AsyncMock(),
    )
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.bridge = bridge

    await server._notify_contact_change(None, public_key)

    bridge.notify_observers.assert_awaited_once()
    event_name, change, state = bridge.notify_observers.await_args.args
    assert event_name == "contact_changed"
    assert change == "new"
    assert state["pubkey"] == public_key
    assert state["name"] == "Alice"


def _contact_command(public_key: bytes, name: str, path_len: int = 0) -> bytes:
    path = (b"\xAA" if path_len else b"").ljust(64, b"\x00")
    return (
        public_key
        + bytes([1, 0, path_len])
        + path
        + name.encode().ljust(32, b"\x00")
    )


def _stateful_frame_server(tmp_path):
    async def _inject(_packet, **_kwargs):
        return True

    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, "0x01")
    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.bridge = bridge
    server.sqlite_handler = handler
    server.companion_hash = "0x01"
    server.journal = journal
    server.tracker = None
    server._write_frame = MagicMock()
    return server, bridge, handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_name",
    [
        "_cmd_get_contacts",
        "_cmd_get_contact_by_key",
        "_cmd_get_channel",
        "_cmd_get_advert_path",
        "_cmd_export_contact",
    ],
)
async def test_frame_state_reads_use_shared_lock(command_name):
    lock = asyncio.Lock()
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.bridge = SimpleNamespace(state_mutation_lock=lock)
    observed = []

    async def _upstream(_server, _data):
        observed.append(lock.locked())

    with patch.object(_BaseFrameServer, command_name, new=_upstream):
        await getattr(server, command_name)(b"payload")

    assert observed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_name",
    [
        "_cmd_share_contact",
        "_cmd_send_txt_msg",
        "_cmd_send_channel_txt_msg",
        "_cmd_send_channel_data",
        "_cmd_send_binary_req",
        "_cmd_send_anon_req",
        "_cmd_send_path_discovery_req",
        "_cmd_send_login",
        "_cmd_send_status_req",
        "_cmd_send_telemetry_req",
        "_cmd_logout",
    ],
)
async def test_frame_state_dependent_sends_wait_for_commit_without_holding_lock(
    command_name,
):
    lock = asyncio.Lock()
    await lock.acquire()
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.bridge = SimpleNamespace(state_mutation_lock=lock)
    observed = []

    async def _upstream(_server, _data):
        observed.append(lock.locked())

    with patch.object(_BaseFrameServer, command_name, new=_upstream):
        command = asyncio.create_task(getattr(server, command_name)(b"payload"))
        await asyncio.sleep(0)
        assert observed == []
        lock.release()
        await command

    assert observed == [False]


@pytest.mark.asyncio
async def test_frame_early_ack_before_sent_response_uses_inflight_session_once():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    session = object()
    server._active_client_session = session
    server._write_queue = asyncio.Queue()

    async def _upstream(instance, _data):
        instance._on_send_confirmed(77, 12)
        instance._write_sent_response(False, 77, 1000)

    with patch.object(_BaseFrameServer, "_cmd_send_txt_msg", new=_upstream):
        await server._cmd_send_txt_msg(b"payload")

    assert server._write_queue.qsize() == 2
    assert 77 not in server._response_session_map("ack")
    assert 77 not in server._early_ack_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_name", "kind"),
    [
        ("_cmd_send_binary_req", "binary"),
        ("_cmd_send_anon_req", "binary"),
        ("_cmd_send_path_discovery_req", "path"),
    ],
)
async def test_successful_tagged_send_records_exact_frame_session(
    command_name,
    kind,
):
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    session = object()
    server._active_client_session = session
    server._write_queue = asyncio.Queue()

    async def _upstream(instance, _data):
        instance._write_sent_response(False, 55, 1000)

    with patch.object(_BaseFrameServer, command_name, new=_upstream):
        await getattr(server, command_name)(b"payload")

    assert server._response_session_map(kind)[55] is session


def test_frame_stale_and_ambiguous_ack_owners_fail_closed_and_are_bounded():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    old_session = object()
    active_session = object()
    server._active_client_session = active_session
    server._write_queue = asyncio.Queue()

    server._claim_response_session("ack", 7, old_session)
    server._on_send_confirmed(7, 1)
    assert server._write_queue.empty()

    server._claim_response_session("ack", 8, old_session)
    server._claim_response_session("ack", 8, active_session)
    server._on_send_confirmed(8, 1)
    assert server._write_queue.empty()

    for tag in range(MAX_PENDING_ACK_CRCS + 10):
        server._claim_response_session("ack", tag, active_session)
    assert len(server._response_session_map("ack")) == MAX_PENDING_ACK_CRCS


@pytest.mark.parametrize("kind", ["ack", "binary", "path", "trace"])
def test_disconnected_frame_claims_are_retired_for_the_actual_response_window(kind):
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    session = object()
    server._active_client_session = session

    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=100.0,
    ):
        server._claim_response_session(
            kind,
            77,
            session,
            timeout_ms=2500,
        )
        server._retire_session_claims(session)

    server._active_client_session = None
    assert server._response_deadline_map(kind)[77] == 102.5
    assert server._response_session_map(kind)[77] is not session

    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=102.0,
    ):
        assert server.owns_response_tag(kind, 77)
        # A late response is claimed and swallowed, never delivered to a
        # replacement session.
        assert server._pop_active_response_session(kind, 77) is None
        assert not server.owns_response_tag(kind, 77)


def test_retired_frame_claim_blocks_other_companion_only_until_timeout():
    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    retired = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    requester = CompanionFrameServer(SimpleNamespace(), "0x02", port=0)
    old_session = object()
    retired._active_client_session = old_session
    daemon.companion_frame_servers = [retired, requester]

    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=10.0,
    ):
        retired._claim_response_session(
            "trace",
            99,
            old_session,
            timeout_ms=1000,
        )
        retired._retire_session_claims(old_session)
    retired._active_client_session = None

    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=10.5,
    ):
        assert daemon._frame_response_tag_conflict(requester, "trace", 99)
    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=11.0,
    ):
        assert not daemon._frame_response_tag_conflict(requester, "trace", 99)
        assert not retired.owns_response_tag("trace", 99)


def test_retiring_old_session_never_removes_newer_exact_claim():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    old_session = object()
    new_session = object()
    server._active_client_session = old_session

    with patch(
        "repeater.companion.frame_server.time.monotonic",
        return_value=20.0,
    ):
        server._claim_response_session(
            "binary",
            55,
            old_session,
            timeout_ms=1000,
        )
        server.discard_response_tag("binary", 55)
        server._claim_response_session(
            "binary",
            55,
            new_session,
            timeout_ms=2000,
        )
        server._active_client_session = new_session
        server._retire_session_claims(old_session)
        assert server._response_session_map("binary")[55] is new_session
        assert server.owns_response_tag("binary", 55)


def test_same_ack_crc_on_two_companions_fails_closed_across_shared_radio():
    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    first = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    second = CompanionFrameServer(SimpleNamespace(), "0x02", port=0)
    daemon.companion_frame_servers = [first, second]
    first._response_owner_resolver = daemon._is_unique_frame_response_owner
    second._response_owner_resolver = daemon._is_unique_frame_response_owner
    first._active_client_session = object()
    second._active_client_session = object()
    first._write_queue = asyncio.Queue()
    second._write_queue = asyncio.Queue()
    first._claim_response_session("ack", 99, first._active_client_session)
    second._claim_response_session("ack", 99, second._active_client_session)

    first._on_send_confirmed(99, 1)
    second._on_send_confirmed(99, 1)

    assert first._write_queue.empty()
    assert second._write_queue.empty()
    assert not first.owns_response_tag("ack", 99)
    assert not second.owns_response_tag("ack", 99)


@pytest.mark.asyncio
async def test_frame_discovery_owns_tag_locally_without_shared_callback_collision():
    control_handler = ControlHandler(lambda _message: None)
    tag = 0x12345678
    repeater_results = []
    repeater_callback = repeater_results.append
    control_handler.set_response_callback(tag, repeater_callback)
    bridge = SimpleNamespace(send_control_data=AsyncMock(return_value=True))

    owner = CompanionFrameServer(
        bridge,
        "0x01",
        port=0,
        control_handler=control_handler,
    )
    other = CompanionFrameServer(
        bridge,
        "0x02",
        port=0,
        control_handler=control_handler,
    )
    owner._active_client_session = object()
    owner._write_queue = asyncio.Queue()
    other._active_client_session = object()
    other._write_queue = asyncio.Queue()

    request = bytes([0x80, 0x04]) + tag.to_bytes(4, "little")
    await owner._cmd_send_control_data(request)

    assert control_handler._response_callbacks[tag] is repeater_callback
    bridge.send_control_data.assert_not_awaited()
    assert tag not in owner._response_session_map("control")
    assert owner._write_queue.qsize() == 1  # correlation slot unavailable
    assert repeater_results == []

    control_handler.clear_response_callback(tag)
    await owner._cmd_send_control_data(request)
    bridge.send_control_data.assert_awaited_once_with(request)
    assert owner._response_session_map("control")[tag] is owner._active_client_session

    response = bytes([0x92, 0x00]) + tag.to_bytes(4, "little") + b"\xAA" * 8
    await owner.push_control_data(1.0, -70, 0, b"", response)
    await other.push_control_data(1.0, -70, 0, b"", response)

    assert owner._write_queue.qsize() == 3  # ERR + OK + owned discovery response
    assert other._write_queue.empty()
    assert tag not in control_handler._response_callbacks

    await owner.push_control_data(2.0, -71, 0, b"", response)
    assert owner._write_queue.qsize() == 4
    assert owner.owns_response_tag("control", tag)
    owner._control_response_deadlines[tag] = 0
    await owner.push_control_data(2.0, -71, 0, b"", response)
    assert owner._write_queue.qsize() == 4
    assert not owner.owns_response_tag("control", tag)

    operator_tag = tag + 1
    operator_response = (
        bytes([0x92, 0x00])
        + operator_tag.to_bytes(4, "little")
        + b"\xBB" * 8
    )
    await owner.push_control_data(1.0, -70, 0, b"", operator_response)
    await other.push_control_data(1.0, -70, 0, b"", operator_response)
    assert owner._write_queue.qsize() == 4
    assert other._write_queue.empty()


@pytest.mark.asyncio
async def test_frame_trace_rejects_repeater_owned_tag_before_radio_send():
    tag = 0x12345678
    request = tag.to_bytes(4, "little") + b"\x00" * 4 + b"\x00\xAA"
    bridge = SimpleNamespace(
        send_trace_path_raw=AsyncMock(
            return_value=SentResult(
                success=True,
                is_flood=False,
                expected_ack=tag,
                timeout_ms=1000,
            )
        )
    )
    reservation_seen = []

    def repeater_conflict(requesting_server, kind, key):
        reservation_seen.append(
            requesting_server is server
            and kind == "trace"
            and key == tag
            and server.owns_response_tag("trace", tag)
        )
        return kind == "trace" and key == tag

    server = CompanionFrameServer(
        bridge,
        "0x01",
        port=0,
        response_tag_conflict=repeater_conflict,
    )
    session = object()
    server._active_client_session = session
    server._write_frame = MagicMock()

    await server._cmd_send_trace_path(request)

    bridge.send_trace_path_raw.assert_not_awaited()
    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_TABLE_FULL])
    )
    assert not server.owns_response_tag("trace", tag)
    assert reservation_seen == [True]

    server._response_tag_conflict = lambda _server, _kind, _key: False
    server._write_frame.reset_mock()

    await server._cmd_send_trace_path(request)

    bridge.send_trace_path_raw.assert_awaited_once_with(
        tag,
        0,
        0,
        b"\xAA",
    )
    assert server._response_session_map("trace")[tag] is session


@pytest.mark.asyncio
async def test_companion_api_ping_awaits_its_correlated_trace_response():
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        companion_hash="0x01",
        trace_tag_conflict=lambda _bridge, _tag: False,
    )
    public_key = b"\xAA" * 32
    bridge.add_update_contact(
        Contact(public_key=public_key, name="Repeater", adv_type=2)
    )
    bridge.send_trace_path_raw = AsyncMock(
        return_value=SentResult(
            success=True,
            is_flood=False,
            expected_ack=1,
            timeout_ms=1000,
        )
    )

    ping = asyncio.create_task(bridge.ping_contact(public_key))
    await asyncio.sleep(0)
    tag, waiter = next(iter(bridge._trace_waiters.items()))
    packet = SimpleNamespace(rssi=-80, get_snr=lambda: 3.5)

    assert bridge.resolve_trace_ping(
        packet,
        {
            "tag": tag,
            "auth_code": waiter["auth_code"],
            "flags": waiter["flags"],
            "trace_path_bytes": waiter["path"],
        },
    )
    result = await ping

    assert result["success"] is True
    assert result["snr_db"] == 3.5
    assert result["rssi"] == -80
    assert result["hop_count"] == 1
    assert bridge._trace_waiters == {}
    bridge.send_trace_path_raw.assert_awaited_once_with(
        tag,
        0,
        waiter["flags"],
        waiter["path"],
    )
    assert waiter["auth_code"] == 0


@pytest.mark.asyncio
async def test_frame_trace_fails_closed_when_tag_ownership_check_errors(caplog):
    tag = 0x12345678
    request = tag.to_bytes(4, "little") + b"\x00" * 4 + b"\x00\xAA"
    bridge = SimpleNamespace(send_trace_path_raw=AsyncMock())

    def ownership_check(_server, _kind, _key):
        raise RuntimeError("ownership unavailable")

    server = CompanionFrameServer(
        bridge,
        "0x01",
        port=0,
        response_tag_conflict=ownership_check,
    )
    server._active_client_session = object()
    server._write_frame = MagicMock()

    with caplog.at_level(logging.ERROR):
        await server._cmd_send_trace_path(request)

    bridge.send_trace_path_raw.assert_not_awaited()
    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_TABLE_FULL])
    )
    assert not server.owns_response_tag("trace", tag)
    assert "could not verify trace tag" in caplog.text


@pytest.mark.asyncio
async def test_two_frame_trace_requests_with_same_tag_send_only_first():
    tag = 0x12345678
    request = tag.to_bytes(4, "little") + b"\x00" * 4 + b"\x00\xAA"
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()

    async def send_first(*_args):
        first_send_started.set()
        await release_first_send.wait()
        return SentResult(
            success=True,
            is_flood=False,
            expected_ack=tag,
            timeout_ms=1000,
        )

    first_bridge = SimpleNamespace(
        send_trace_path_raw=AsyncMock(side_effect=send_first)
    )
    second_bridge = SimpleNamespace(
        send_trace_path_raw=AsyncMock(
            return_value=SentResult(
                success=True,
                is_flood=False,
                expected_ack=tag,
                timeout_ms=1000,
            )
        )
    )
    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    first = CompanionFrameServer(
        first_bridge,
        "0x01",
        port=0,
        response_owner_resolver=daemon._is_unique_frame_response_owner,
        response_tag_conflict=daemon._frame_response_tag_conflict,
    )
    second = CompanionFrameServer(
        second_bridge,
        "0x02",
        port=0,
        response_owner_resolver=daemon._is_unique_frame_response_owner,
        response_tag_conflict=daemon._frame_response_tag_conflict,
    )
    daemon.companion_frame_servers = [first, second]
    first._active_client_session = object()
    second._active_client_session = object()
    first._write_queue = asyncio.Queue()
    second._write_queue = asyncio.Queue()
    first._write_frame = MagicMock()
    second._write_frame = MagicMock()

    first_command = asyncio.create_task(first._cmd_send_trace_path(request))
    await asyncio.wait_for(first_send_started.wait(), timeout=1.0)
    await second._cmd_send_trace_path(request)

    second_bridge.send_trace_path_raw.assert_not_awaited()
    second._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_TABLE_FULL])
    )
    assert first.owns_response_tag("trace", tag)
    assert not second.owns_response_tag("trace", tag)

    release_first_send.set()
    await asyncio.wait_for(first_command, timeout=1.0)
    first_bridge.send_trace_path_raw.assert_awaited_once()

    first.push_trace_data(1, 0, tag, 0, b"\xAA", b"\x00", 0)

    first._write_frame.assert_called_once()  # SENT
    assert first._write_queue.qsize() == 1  # owned trace response
    assert second._write_frame.call_count == 1
    assert second._write_queue.empty()
    assert not first.owns_response_tag("trace", tag)


@pytest.mark.asyncio
async def test_two_frame_control_requests_with_same_tag_send_only_first():
    tag = 0x12345678
    request = bytes([0x80, 0x04]) + tag.to_bytes(4, "little")
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()

    async def send_first(_payload):
        first_send_started.set()
        await release_first_send.wait()
        return True

    first_bridge = SimpleNamespace(
        send_control_data=AsyncMock(side_effect=send_first)
    )
    second_bridge = SimpleNamespace(send_control_data=AsyncMock(return_value=True))
    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    first = CompanionFrameServer(
        first_bridge,
        "0x01",
        port=0,
        response_owner_resolver=daemon._is_unique_frame_response_owner,
        response_tag_conflict=daemon._frame_response_tag_conflict,
    )
    second = CompanionFrameServer(
        second_bridge,
        "0x02",
        port=0,
        response_owner_resolver=daemon._is_unique_frame_response_owner,
        response_tag_conflict=daemon._frame_response_tag_conflict,
    )
    daemon.companion_frame_servers = [first, second]
    first._active_client_session = object()
    second._active_client_session = object()
    first._write_queue = asyncio.Queue()
    second._write_queue = asyncio.Queue()
    first._write_frame = MagicMock()
    second._write_frame = MagicMock()

    first_command = asyncio.create_task(first._cmd_send_control_data(request))
    await asyncio.wait_for(first_send_started.wait(), timeout=1.0)
    await second._cmd_send_control_data(request)

    second_bridge.send_control_data.assert_not_awaited()
    second._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_TABLE_FULL])
    )
    assert first.owns_response_tag("control", tag)
    assert not second.owns_response_tag("control", tag)

    release_first_send.set()
    await asyncio.wait_for(first_command, timeout=1.0)
    first_bridge.send_control_data.assert_awaited_once_with(request)

    response = bytes([0x92, 0x00]) + tag.to_bytes(4, "little") + b"\xAA" * 8
    await first.push_control_data(1.0, -70, 0, b"", response)

    first._write_frame.assert_called_once()  # OK
    assert first._write_queue.qsize() == 1  # owned control response
    assert second._write_frame.call_count == 1
    assert second._write_queue.empty()
    assert first.owns_response_tag("control", tag)


@pytest.mark.asyncio
async def test_discovery_allocation_and_frame_claim_on_daemon_loop_send_only_first():
    tag = 0x12345678
    discovery_injector = AsyncMock(return_value=True)
    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    discovery = DiscoveryHelper(
        local_identity=None,
        packet_injector=discovery_injector,
        tag_conflict=lambda candidate: daemon._frame_has_response_owner(
            "control",
            candidate,
        ),
    )
    daemon.discovery_helper = discovery

    frame_bridge = SimpleNamespace(send_control_data=AsyncMock(return_value=True))
    frame = CompanionFrameServer(
        frame_bridge,
        "0x01",
        port=0,
        control_handler=discovery.control_handler,
        response_owner_resolver=daemon._is_unique_frame_response_owner,
        response_tag_conflict=daemon._frame_response_tag_conflict,
    )
    daemon.companion_frame_servers = [frame]
    frame._active_client_session = object()
    frame._write_frame = MagicMock()
    request = bytes([0x80, 0x04]) + tag.to_bytes(4, "little")

    async def allocate_and_start_discovery():
        # This is the no-await daemon-loop critical section used by the HTTP
        # endpoint: reserve the tag and schedule its RF task as one operation.
        discovery.cleanup_sessions()
        session = discovery.create_session(
            timeout=1,
            filter_mask=0x04,
        )
        discovery.start_session_task(session["session_id"])
        return session

    with patch(
        "repeater.handler_helpers.discovery.secrets.randbits",
        return_value=tag,
    ):
        # Both requests become runnable together. The allocation task is
        # deliberately queued first, so its reservation must be visible when
        # the Frame command performs its own preflight.
        allocation_task = asyncio.create_task(allocate_and_start_discovery())
        frame_task = asyncio.create_task(frame._cmd_send_control_data(request))
        session, _ = await asyncio.gather(allocation_task, frame_task)

    await asyncio.sleep(0)

    assert session["tag"] == tag
    discovery_injector.assert_awaited_once()
    frame_bridge.send_control_data.assert_not_awaited()
    frame._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_TABLE_FULL])
    )
    assert discovery.owns_response_tag(tag)
    assert not frame.owns_response_tag("control", tag)

    pending_tasks = tuple(discovery._pending_tasks)
    for task in pending_tasks:
        task.cancel()
    await asyncio.gather(*pending_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_trace_fanout_reaches_only_exact_owner_server_and_session():
    bridge = SimpleNamespace()
    owner = CompanionFrameServer(bridge, "0x01", port=0)
    other = CompanionFrameServer(bridge, "0x02", port=0)
    owner_session = object()
    owner._active_client_session = owner_session
    owner._write_queue = asyncio.Queue()
    other._active_client_session = object()
    other._write_queue = asyncio.Queue()
    tag = 41
    owner._claim_response_session("trace", tag, owner_session)

    args = (1, 0, tag, 9, b"\xAA", b"\x10", 0x20)
    await owner.push_trace_data_async(*args)
    await other.push_trace_data_async(*args)
    assert owner._write_queue.qsize() == 1
    assert other._write_queue.empty()

    stale_tag = 42
    owner._claim_response_session("trace", stale_tag, owner_session)
    owner._active_client_session = object()
    await owner.push_trace_data_async(
        1,
        0,
        stale_tag,
        9,
        b"\xAA",
        b"\x10",
        0x20,
    )
    assert owner._write_queue.qsize() == 1


def test_binary_and_path_responses_require_current_exact_session():
    server = CompanionFrameServer(SimpleNamespace(), "0x01", port=0)
    current_session = object()
    stale_session = object()
    server._active_client_session = current_session
    server._write_queue = asyncio.Queue()

    server._claim_response_session("binary", 10, stale_session)
    server._companion_binary_tags.add(10)
    server._on_binary_response((10).to_bytes(4, "little"), b"stale")
    assert server._write_queue.empty()

    server._claim_response_session("binary", 11, current_session)
    server._companion_binary_tags.add(11)
    server._on_binary_response((11).to_bytes(4, "little"), b"owned")
    assert server._write_queue.qsize() == 1

    server._claim_response_session("path", 12, stale_session)
    server._on_path_discovery_response(
        (12).to_bytes(4, "little"),
        b"\x01" * 32,
        0,
        b"",
        0,
        b"",
    )
    assert server._write_queue.qsize() == 1

    server._claim_response_session("path", 13, current_session)
    server._on_path_discovery_response(
        (13).to_bytes(4, "little"),
        b"\x01" * 32,
        0,
        b"",
        0,
        b"",
    )
    assert server._write_queue.qsize() == 2


@pytest.mark.asyncio
async def test_frame_unexpected_contact_command_failure_rolls_back_and_cleans_state(
    tmp_path,
):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    public_key = b"\xB1" * 32

    async def _mutate_then_fail(_server, _data):
        bridge.add_update_contact(
            Contact(public_key=public_key, name="partial", adv_type=1)
        )
        raise RuntimeError("unexpected command failure")

    with patch.object(
        _BaseFrameServer,
        "_cmd_add_update_contact",
        new=_mutate_then_fail,
    ):
        with pytest.raises(RuntimeError, match="unexpected command failure"):
            await server._cmd_add_update_contact(
                _contact_command(public_key, "Alice")
            )

    assert bridge.get_contact_by_key(public_key) is None
    assert server._defer_command_response is False
    assert server._deferred_command_response is None
    assert server._command_persistence_error is None
    assert server._contact_command_key is None
    assert server._contact_command_before is None
    server._write_frame.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_result", "contact_present"),
    [
        ({"event_seq": 1}, True),
        (False, False),
    ],
)
async def test_frame_cancelled_contact_commit_finishes_and_cleans_state(
    tmp_path,
    storage_result,
    contact_present,
):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    public_key = b"\xB2" * 32
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _blocking_store(_contact, *_args):
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return storage_result

    server.journal.store_contact = _blocking_store
    command = asyncio.create_task(
        server._cmd_add_update_contact(_contact_command(public_key, "Alice"))
    )
    assert await asyncio.to_thread(started.wait, 2)

    command.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await command

    assert finished.is_set()
    assert (bridge.get_contact_by_key(public_key) is not None) is contact_present
    assert server._defer_command_response is False
    assert server._deferred_command_response is None
    assert server._command_persistence_error is None
    assert server._contact_command_key is None
    assert server._contact_command_before is None
    server._write_frame.assert_not_called()


@pytest.mark.asyncio
async def test_frame_contact_add_reset_remove_each_journal_once(tmp_path):
    server, _bridge, handler = _stateful_frame_server(tmp_path)
    public_key = b"\xA1" * 32

    await server._cmd_add_update_contact(
        _contact_command(public_key, "Alice", path_len=1)
    )
    await server._cmd_reset_path(public_key)
    await server._cmd_remove_contact(public_key)

    events = [
        event
        for event in handler.companion_get_events("0x01", 0)
        if event["event_type"] == "contact"
    ]
    assert [event["payload"]["change"] for event in events] == [
        "new",
        "path",
        "remove",
    ]
    assert events[1]["payload"]["out_path_len"] == -1
    assert handler.companion_load_contacts("0x01") == []


@pytest.mark.asyncio
async def test_reset_path_storage_failure_rolls_back_and_returns_one_error(
    tmp_path,
    caplog,
):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    public_key = b"\xA2" * 32
    await server._cmd_add_update_contact(
        _contact_command(public_key, "Alice", path_len=1)
    )
    before = server._contact_state(public_key)
    server._write_frame.reset_mock()
    server.journal.store_contact = MagicMock(side_effect=RuntimeError("disk failed"))

    await server._cmd_reset_path(public_key)

    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
    )
    assert server._contact_state(public_key) == before
    assert "Save contact after path reset failed" in caplog.text


@pytest.mark.asyncio
async def test_contact_add_storage_failure_rolls_back_and_returns_one_error(tmp_path):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    public_key = b"\xA3" * 32
    server.journal.store_contact = MagicMock(side_effect=RuntimeError("disk failed"))

    await server._cmd_add_update_contact(_contact_command(public_key, "Alice"))

    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
    )
    assert bridge.get_contact_by_key(public_key) is None


@pytest.mark.asyncio
async def test_contact_remove_storage_failure_rolls_back_and_returns_one_error(tmp_path):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    public_key = b"\xA4" * 32
    await server._cmd_add_update_contact(_contact_command(public_key, "Alice"))
    before = server._contact_state(public_key)
    server._write_frame.reset_mock()
    server.journal.remove_contact = MagicMock(side_effect=RuntimeError("disk failed"))

    await server._cmd_remove_contact(public_key)

    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
    )
    assert server._contact_state(public_key) == before


@pytest.mark.asyncio
async def test_channel_storage_failure_rolls_back_and_returns_one_error(tmp_path):
    server, bridge, _handler = _stateful_frame_server(tmp_path)
    original_secret = b"\x44" * 32
    replacement_secret = b"\x55" * 32
    await server._cmd_set_channel(
        bytes([3]) + b"original".ljust(32, b"\x00") + original_secret
    )
    server._write_frame.reset_mock()
    server.journal.store_channel = MagicMock(side_effect=RuntimeError("disk failed"))

    await server._cmd_set_channel(
        bytes([3]) + b"replacement".ljust(32, b"\x00") + replacement_secret
    )

    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
    )
    channel = bridge.get_channel(3)
    assert channel.name == "original"
    assert channel.secret == original_secret


@pytest.mark.asyncio
async def test_frame_preference_event_failure_rolls_back_and_returns_file_error(
    tmp_path,
):
    server, bridge, handler = _stateful_frame_server(tmp_path)
    bridge._sqlite_handler = handler
    bridge._companion_hash = "0x01"
    bridge._journal = server.journal
    original_name = bridge.prefs.node_name
    conn = handler._connect()
    conn.execute(
        """
        CREATE TRIGGER reject_prefs_event
        BEFORE INSERT ON companion_events
        WHEN NEW.event_type = 'prefs'
        BEGIN
            SELECT RAISE(ABORT, 'test rejection');
        END
        """
    )
    conn.commit()

    await server._cmd_set_advert_name(b"Renamed")

    server._write_frame.assert_called_once_with(
        bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
    )
    assert bridge.prefs.node_name == original_name
    assert handler.companion_load_prefs("0x01") is None
    assert handler.companion_get_events("0x01", 0) == []


@pytest.mark.asyncio
async def test_frame_preference_change_persists_and_journals_once(tmp_path):
    server, bridge, handler = _stateful_frame_server(tmp_path)
    bridge._sqlite_handler = handler
    bridge._companion_hash = "0x01"
    bridge._journal = server.journal

    await server._cmd_set_advert_name(b"Renamed")

    assert handler.companion_load_prefs("0x01")["node_name"] == "Renamed"
    events = handler.companion_get_events("0x01", 0)
    assert [event["event_type"] for event in events] == ["prefs"]
    assert events[0]["payload"] == {"node_name": "Renamed"}


@pytest.mark.asyncio
async def test_frame_channel_change_is_atomic_deduplicated_and_secret_free(tmp_path):
    server, _bridge, handler = _stateful_frame_server(tmp_path)
    secret = bytes(range(32))
    set_command = bytes([3]) + b"chat".ljust(32, b"\x00") + secret
    clear_command = bytes([3]) + bytes(32) + secret

    await server._cmd_set_channel(set_command)
    await server._cmd_set_channel(set_command)
    await server._cmd_set_channel(clear_command)

    events = [
        event
        for event in handler.companion_get_events("0x01", 0)
        if event["event_type"] == "channel"
    ]
    assert [event["payload"] for event in events] == [
        {"index": 3, "name": "chat", "change": "update"},
        {"index": 3, "name": None, "change": "remove"},
    ]
    assert secret.hex() not in str(events)
    assert handler.companion_load_channels("0x01") == []


@pytest.mark.asyncio
async def test_frame_server_persistence_paths_and_stop():
    sqlite = SimpleNamespace(
        companion_store_inbound_message=MagicMock(
            return_value={"inserted": True, "message_id": 1}
        ),
        companion_pop_message=MagicMock(
            return_value={
                "sender_key": b"k",
                "txt_type": 1,
                "timestamp": 2,
                "text": "hello",
                "is_channel": True,
                "channel_idx": 3,
                "path_len": 1,
            }
        ),
        companion_save_contacts=MagicMock(),
        companion_save_channels=MagicMock(),
        companion_upsert_contact=MagicMock(),
    )
    queue_entry = object()
    bridge = SimpleNamespace(
        message_queue=SimpleNamespace(remove=MagicMock(return_value=True)),
        sync_next_message=lambda: None,
        get_contacts=lambda: [],
        channels=SimpleNamespace(max_channels=2),
        get_channel=lambda idx: None,
    )

    with (
        patch(
            "repeater.companion.frame_server._BaseFrameServer.__init__", lambda self, **kwargs: None
        ),
        patch("repeater.companion.frame_server._BaseFrameServer.stop", AsyncMock()) as base_stop,
    ):
        srv = CompanionFrameServer(bridge=bridge, companion_hash="h", sqlite_handler=sqlite)
        srv.bridge = bridge
        srv.companion_hash = "h"
        srv._write_frame = MagicMock()
        srv._build_message_frame = MagicMock(return_value=b"frame")

        await srv._persist_companion_message({"text": "x"}, queue_entry)
        sqlite.companion_store_inbound_message.assert_called_once_with(
            "h", {"text": "x"}, None
        )
        bridge.message_queue.remove.assert_called_once_with(queue_entry)

        msg = srv._sync_next_from_persistence()
        assert msg is not None
        assert msg.text == "hello"

        await srv._cmd_sync_next_message(b"")
        srv._write_frame.assert_called_once_with(b"frame")

        contact = SimpleNamespace(
            public_key=b"\x01\x02",
            name="n",
            adv_type=1,
            flags=0,
            out_path_len=1,
            out_path=b"\x03",
            last_advert_timestamp=4,
            lastmod=5,
            gps_lat=1.1,
            gps_lon=2.2,
            sync_since=6,
        )
        await srv._persist_contact(contact)
        sqlite.companion_upsert_contact.assert_called_once()

        bridge.get_contacts = lambda: [contact]
        bridge.get_channel = lambda idx: (
            SimpleNamespace(name="c1", secret="s") if idx == 1 else None
        )
        await srv.stop()

        sqlite.companion_save_contacts.assert_not_called()
        sqlite.companion_save_channels.assert_not_called()
        base_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistence_removes_the_supplied_entry_not_a_later_receive():
    persisted_entry = object()
    later_entry = object()

    class _Queue:
        max_size = 10

        def __init__(self):
            self.entries = [persisted_entry, later_entry]

        def remove(self, entry):
            for index, queued in enumerate(self.entries):
                if queued is entry:
                    del self.entries[index]
                    return True
            return False

    sqlite = SimpleNamespace(
        companion_store_inbound_message=MagicMock(
            return_value={"inserted": True, "message_id": 1}
        )
    )
    bridge = SimpleNamespace(message_queue=_Queue())
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.sqlite_handler = sqlite
    server.companion_hash = "0x01"
    server.journal = None
    server.tracker = None
    server.bridge = bridge

    await server._persist_companion_message({"text": "first"}, persisted_entry)

    assert bridge.message_queue.entries == [later_entry]


@pytest.mark.asyncio
async def test_sqlite_is_only_sync_source_while_inbound_commit_is_inflight():
    persistence_started = threading.Event()
    release_persistence = threading.Event()
    queue_entry = QueuedMessage(
        sender_key=b"\x01" * 32,
        txt_type=0,
        timestamp=1,
        text="once",
    )

    class _Queue:
        max_size = 10

        def __init__(self):
            self.entries = [queue_entry]

        def remove(self, entry):
            if entry not in self.entries:
                return False
            self.entries.remove(entry)
            return True

    def _store(*_args):
        persistence_started.set()
        assert release_persistence.wait(timeout=2)
        return {"inserted": True, "message_id": 1}

    sqlite = SimpleNamespace(
        companion_store_inbound_message=MagicMock(side_effect=_store),
        companion_pop_message=MagicMock(
            side_effect=[
                None,
                {
                    "sender_key": queue_entry.sender_key,
                    "txt_type": 0,
                    "timestamp": 1,
                    "text": "once",
                },
                None,
            ]
        ),
    )
    bridge = SimpleNamespace(
        message_queue=_Queue(),
        sync_next_message=MagicMock(
            side_effect=AssertionError("SQLite mode must not pop the core queue")
        ),
    )
    server = CompanionFrameServer.__new__(CompanionFrameServer)
    server.sqlite_handler = sqlite
    server.companion_hash = "0x01"
    server.journal = None
    server.tracker = None
    server.bridge = bridge
    server._write_frame = MagicMock()
    server._build_message_frame = lambda message: message.text.encode()

    persistence = asyncio.create_task(
        server._persist_companion_message({"text": "once"}, queue_entry)
    )
    assert await asyncio.to_thread(persistence_started.wait, 1)

    await server._cmd_sync_next_message(b"")
    assert bridge.message_queue.entries == [queue_entry]
    bridge.sync_next_message.assert_not_called()

    release_persistence.set()
    await persistence
    assert bridge.message_queue.entries == []

    await server._cmd_sync_next_message(b"")
    await server._cmd_sync_next_message(b"")
    assert [entry.args[0] for entry in server._write_frame.call_args_list] == [
        bytes([RESP_CODE_NO_MORE_MESSAGES]),
        b"once",
        bytes([RESP_CODE_NO_MORE_MESSAGES]),
    ]


@pytest.mark.asyncio
async def test_frame_server_no_more_messages_response_when_empty():
    bridge = SimpleNamespace(sync_next_message=lambda: None)

    with patch(
        "repeater.companion.frame_server._BaseFrameServer.__init__", lambda self, **kwargs: None
    ):
        srv = CompanionFrameServer(bridge=bridge, companion_hash="h", sqlite_handler=None)
        srv.bridge = bridge
        srv._write_frame = MagicMock()
        await srv._cmd_sync_next_message(b"")
        # RESP_CODE_NO_MORE_MESSAGES is encoded as a single-byte frame.
        assert srv._write_frame.call_args[0][0] == bytes([RESP_CODE_NO_MORE_MESSAGES])


@pytest.mark.asyncio
async def test_restart_queue_rows_are_delivered_once_from_sqlite():
    sqlite = SimpleNamespace(
        companion_pop_message=MagicMock(
            side_effect=[
                {"sender_key": b"a", "timestamp": 1, "text": "first"},
                {"sender_key": b"b", "timestamp": 2, "text": "second"},
                None,
            ]
        )
    )
    bridge = SimpleNamespace(sync_next_message=lambda: None)

    with patch(
        "repeater.companion.frame_server._BaseFrameServer.__init__", lambda self, **kwargs: None
    ):
        srv = CompanionFrameServer(bridge=bridge, companion_hash="h", sqlite_handler=sqlite)
        srv.bridge = bridge
        srv.companion_hash = "h"
        srv._write_frame = MagicMock()
        srv._build_message_frame = lambda message: message.text.encode()

        await srv._cmd_sync_next_message(b"")
        await srv._cmd_sync_next_message(b"")
        await srv._cmd_sync_next_message(b"")

    assert [call.args[0] for call in srv._write_frame.call_args_list] == [
        b"first",
        b"second",
        bytes([RESP_CODE_NO_MORE_MESSAGES]),
    ]
    assert sqlite.companion_pop_message.call_count == 3


@pytest.mark.asyncio
async def test_rejected_queue_callback_persists_history_and_notifies_client():
    server = object.__new__(CompanionFrameServer)
    server._persist_companion_message = AsyncMock()
    server._enqueue_frame = MagicMock()

    await server._on_message_event(
        MessageEvent(
            sender_key=b"\x01" * 32,
            text="rejected",
            timestamp=1,
            txt_type=0,
            queued=False,
        )
    )

    server._persist_companion_message.assert_awaited_once()
    assert server._persist_companion_message.await_args.args[1] is None
    server._enqueue_frame.assert_called_once_with(bytes([PUSH_CODE_MSG_WAITING]))


@pytest.mark.asyncio
async def test_queued_callback_passes_exact_entry_to_persistence():
    queue_entry = object()
    server = object.__new__(CompanionFrameServer)
    server._persist_companion_message = AsyncMock()
    server._enqueue_frame = MagicMock()

    await server._on_message_event(
        MessageEvent(
            sender_key=b"\x01" * 32,
            text="queued",
            timestamp=1,
            txt_type=0,
            queued=True,
            queue_entry=queue_entry,
        )
    )

    server._persist_companion_message.assert_awaited_once()
    assert server._persist_companion_message.await_args.args[1] is queue_entry


def test_companion_utils_validation_and_normalization():
    assert normalize_companion_identity_key(" 0xAABB ") == "AABB"
    assert validate_companion_node_name("  node-1  ") == "node-1"
    assert validate_companion_registration_name("chat.agent-1") == "chat.agent-1"

    with pytest.raises(ValueError):
        validate_companion_node_name(cast(Any, 123))
    with pytest.raises(ValueError):
        validate_companion_node_name("   ")
    with pytest.raises(ValueError):
        validate_companion_node_name("x" * 32)
    with pytest.raises(ValueError):
        validate_companion_node_name("bad\nname")
    for value in ("bad\tname", "bad\x1bname", "bad\x7fname"):
        with pytest.raises(ValueError):
            validate_companion_node_name(value)
    for invalid_name in (
        "",
        " leading",
        "trailing ",
        "has space",
        "bad/name",
        "bad:name",
        "bad%name",
        "é",
        "x" * 65,
    ):
        with pytest.raises(ValueError, match="companion name"):
            validate_companion_registration_name(invalid_name)
