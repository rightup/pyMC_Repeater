import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.companion.constants import PUSH_CODE_MSG_WAITING
from openhop_core.companion.models import (
    ChannelDataEvent,
    ChannelMessageEvent,
    MessageEvent,
    QueuedMessage,
)
from openhop_core.companion import CompanionBridge
from openhop_core.node.handlers.result import HandlerResult
from openhop_core.protocol import LocalIdentity

from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.companion.utils import validate_companion_listener_config
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.identity_manager import IdentityManager
from repeater.main import IdentityConfigurationError, RepeaterDaemon


class _Identity:
    def __init__(self, seed: bytes):
        self._public_key = bytes(seed[:32])

    def get_public_key(self):
        return self._public_key

    def get_address_bytes(self):
        return self._public_key[:3]


def _daemon(companions=()):
    daemon = RepeaterDaemon(
        {
            "repeater": {"node_name": "n"},
            "logging": {},
            "identities": {"companions": list(companions)},
        },
        radio=object(),
    )
    daemon.identity_manager = IdentityManager({})
    daemon.router = SimpleNamespace(inject_packet=AsyncMock())
    daemon.repeater_handler = None
    return daemon


def test_disabled_frame_listener_does_not_reserve_or_validate_a_port():
    validate_companion_listener_config(
        [
            {
                "name": "frame-client",
                "settings": {"frame_enabled": True, "tcp_port": 5000},
            },
            {
                "name": "rest-only",
                "settings": {
                    "frame_enabled": False,
                    "tcp_port": "unused",
                    "bind_address": ["unused"],
                },
            },
        ],
        {"enabled": True, "port": 8000},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_name", "event", "expected_text"),
    [
        (
            "message_event",
            MessageEvent(
                sender_key=b"\x01" * 32,
                text="direct",
                timestamp=1,
                txt_type=0,
                packet_hash="11" * 8,
                queued=False,
            ),
            "direct",
        ),
        (
            "channel_message_event",
            ChannelMessageEvent(
                channel_name="#chat",
                sender_name="peer",
                text="channel",
                timestamp=2,
                channel_idx=0,
                packet_hash="22" * 8,
                queued=False,
            ),
            "channel",
        ),
        (
            "channel_data_event",
            ChannelDataEvent(
                channel_idx=0,
                path_len=0,
                data_type=7,
                payload=b"\x01\x02",
                packet_hash="33" * 8,
                queued=False,
            ),
            "",
        ),
    ],
)
async def test_rest_only_bridge_persists_every_inbound_event_without_frame_server(
    tmp_path,
    event_name,
    event,
    expected_text,
):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, "0x41")
    notified = []
    journal.register_listener(notified.append)
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=handler,
        companion_hash="0x41",
        journal=journal,
    )

    await bridge._fire_callbacks(event_name, event)

    messages = handler.companion_get_messages("0x41")
    assert len(messages) == 1
    assert messages[0]["text"] == expected_text
    assert [item["event_type"] for item in notified] == ["message"]


@pytest.mark.asyncio
async def test_frame_enabled_bridge_persists_once_before_frame_push(
    tmp_path,
    monkeypatch,
):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, "0x42")
    tracker = MagicMock()
    tracker.register_inbound.return_value = 73
    tracker.promote_inbound.return_value = []
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=handler,
        companion_hash="0x42",
        journal=journal,
        tracker=tracker,
    )
    server = CompanionFrameServer(
        bridge,
        "0x42",
        port=0,
        sqlite_handler=handler,
        journal=journal,
    )
    server._write_queue = asyncio.Queue()
    server._setup_push_callbacks()
    queue_entry = QueuedMessage(
        sender_key=b"\x02" * 32,
        text="exactly once",
        timestamp=3,
        txt_type=0,
    )
    assert bridge.message_queue.push(queue_entry) is True
    store_inbound = MagicMock(wraps=journal.store_inbound_message)
    monkeypatch.setattr(journal, "store_inbound_message", store_inbound)
    event = MessageEvent(
        sender_key=b"\x02" * 32,
        text="exactly once",
        timestamp=3,
        txt_type=0,
        packet_hash="44" * 8,
        queued=True,
        queue_entry=queue_entry,
    )

    await bridge._fire_callbacks("message_event", event)

    messages = handler.companion_get_messages("0x42")
    assert [message["text"] for message in messages] == ["exactly once"]
    assert [
        item["event_type"]
        for item in handler.companion_get_events("0x42", 0)
    ] == ["message"]
    store_inbound.assert_called_once()
    tracker.register_inbound.assert_called_once()
    tracker.promote_inbound.assert_called_once()
    tracker.discard_registration.assert_not_called()
    assert bridge.message_queue.is_empty()
    assert server._write_queue.get_nowait().endswith(
        bytes([PUSH_CODE_MSG_WAITING])
    )
    assert server._write_queue.empty()


@pytest.mark.asyncio
async def test_frame_push_is_suppressed_when_inbound_commit_fails(
    tmp_path,
    monkeypatch,
):
    handler = SQLiteHandler(tmp_path)
    tracker = MagicMock()
    tracker.register_inbound.return_value = 74
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=handler,
        companion_hash="0x43",
        tracker=tracker,
    )
    server = CompanionFrameServer(
        bridge,
        "0x43",
        port=0,
        sqlite_handler=handler,
    )
    server._write_queue = asyncio.Queue()
    host_callback = AsyncMock()
    bridge.on_message_event(host_callback)
    server._setup_push_callbacks()
    store_inbound = MagicMock(side_effect=RuntimeError("storage unavailable"))
    monkeypatch.setattr(
        handler, "companion_store_inbound_message", store_inbound
    )
    queue_entry = QueuedMessage(
        sender_key=b"\x03" * 32,
        text="not committed",
        timestamp=4,
        txt_type=0,
    )
    assert bridge.message_queue.push(queue_entry) is True
    event = MessageEvent(
        sender_key=b"\x03" * 32,
        text="not committed",
        timestamp=4,
        txt_type=0,
        packet_hash="55" * 8,
        queued=True,
        queue_entry=queue_entry,
    )

    async def _authenticated_receive(receiving_bridge, _packet):
        await receiving_bridge._fire_callbacks("message_event", event)
        return HandlerResult.consumed()

    packet = SimpleNamespace(get_payload_type=lambda: 0)
    with patch.object(
        CompanionBridge,
        "process_received_packet",
        _authenticated_receive,
    ):
        result = await bridge.process_received_packet(packet)

    assert result.authenticated is True
    store_inbound.assert_called_once()
    tracker.discard_registration.assert_called_once_with(74)
    assert bridge.sync_next_message() is queue_entry
    host_callback.assert_not_awaited()
    assert server._write_queue.empty()


@pytest.mark.asyncio
async def test_cancelled_inbound_commit_reconciles_then_propagates(
    tmp_path,
    monkeypatch,
):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, "0x45")
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=handler,
        companion_hash="0x45",
        journal=journal,
    )
    server = CompanionFrameServer(
        bridge,
        "0x45",
        port=0,
        sqlite_handler=handler,
        journal=journal,
    )
    server._write_queue = asyncio.Queue()
    server._setup_push_callbacks()
    queue_entry = QueuedMessage(
        sender_key=b"\x04" * 32,
        text="commit before cancel",
        timestamp=5,
        txt_type=0,
    )
    assert bridge.message_queue.push(queue_entry) is True
    event = MessageEvent(
        sender_key=b"\x04" * 32,
        text="commit before cancel",
        timestamp=5,
        txt_type=0,
        packet_hash="66" * 8,
        queued=True,
        queue_entry=queue_entry,
    )
    entered = threading.Event()
    release = threading.Event()
    real_store = journal.store_inbound_message

    def _blocked_store(*args):
        entered.set()
        assert release.wait(2)
        return real_store(*args)

    monkeypatch.setattr(journal, "store_inbound_message", _blocked_store)
    task = asyncio.create_task(bridge._fire_callbacks("message_event", event))
    assert await asyncio.to_thread(entered.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [message["text"] for message in handler.companion_get_messages("0x45")] == [
        "commit before cancel"
    ]
    assert bridge.message_queue.is_empty()
    assert server._write_queue.empty()


@pytest.mark.asyncio
async def test_stopping_frame_server_detaches_only_its_transient_callbacks(tmp_path):
    handler = SQLiteHandler(tmp_path)
    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=handler,
        companion_hash="0x44",
    )
    server = CompanionFrameServer(
        bridge,
        "0x44",
        port=0,
        sqlite_handler=handler,
    )
    host_callback = AsyncMock()
    bridge.on_message_event(host_callback)
    server._setup_push_callbacks()

    assert any(
        getattr(callback, "__self__", None) is server
        for callbacks in bridge._push_callbacks.values()
        for callback in callbacks
    )

    await server.stop()

    assert host_callback in bridge._push_callbacks["message_event"]
    assert not any(
        getattr(callback, "__self__", None) is server
        for callbacks in bridge._push_callbacks.values()
        for callback in callbacks
    )


@pytest.mark.asyncio
async def test_boot_rest_only_companion_starts_bridge_without_frame_server():
    config = {
        "name": "rest-only",
        "identity_key": "31" * 32,
        "settings": {"frame_enabled": False},
    }
    daemon = _daemon((config,))

    with (
        patch("openhop_core.LocalIdentity", _Identity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()

        await daemon._load_companion_identities()

    server_cls.assert_not_called()
    bridge.start.assert_awaited_once()
    assert daemon.companion_bridges == {0x31: bridge}
    assert daemon.companion_frame_servers == []
    assert daemon.identity_manager.get_identity_by_name("rest-only") is not None


@pytest.mark.asyncio
async def test_hot_add_rest_only_companion_starts_bridge_without_frame_server():
    daemon = _daemon()
    config = {
        "name": "rest-only",
        "identity_key": "32" * 32,
        "settings": {"frame_enabled": False},
    }

    with (
        patch("openhop_core.LocalIdentity", _Identity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()

        await daemon.add_companion_from_config(config)

    server_cls.assert_not_called()
    bridge.start.assert_awaited_once()
    assert daemon.companion_bridges == {0x32: bridge}
    assert daemon.companion_frame_servers == []
    assert daemon.identity_manager.get_identity_by_name("rest-only") is not None


@pytest.mark.asyncio
async def test_boot_frame_bind_failure_is_fatal_and_not_published():
    config = {
        "name": "frame-client",
        "identity_key": "33" * 32,
        "settings": {
            "frame_enabled": True,
            "tcp_port": 5000,
            "bind_address": "127.0.0.1",
        },
    }
    daemon = _daemon((config,))

    with (
        patch("openhop_core.LocalIdentity", _Identity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        server = server_cls.return_value
        server.start = AsyncMock(side_effect=OSError("address already in use"))
        server.stop = AsyncMock()

        with pytest.raises(
            IdentityConfigurationError,
            match=r"frame-client.*127\.0\.0\.1:5000.*address already in use",
        ):
            await daemon._load_companion_identities()

    server.stop.assert_awaited_once()
    bridge.stop.assert_awaited_once()
    assert daemon.companion_bridges == {}
    assert daemon.companion_frame_servers == []
    assert daemon.identity_manager.get_identity_by_name("frame-client") is None


@pytest.mark.asyncio
async def test_hot_add_frame_bind_failure_is_contextual_and_not_published():
    config = {
        "name": "frame-client",
        "identity_key": "35" * 32,
        "settings": {
            "frame_enabled": True,
            "tcp_port": 5001,
            "bind_address": "127.0.0.1",
        },
    }
    daemon = _daemon()

    with (
        patch("openhop_core.LocalIdentity", _Identity),
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
    ):
        bridge = bridge_cls.return_value
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        server = server_cls.return_value
        server.start = AsyncMock(side_effect=OSError("address already in use"))
        server.stop = AsyncMock()

        with pytest.raises(
            IdentityConfigurationError,
            match=r"frame-client.*127\.0\.0\.1:5001.*address already in use",
        ):
            await daemon.add_companion_from_config(config)

    server.stop.assert_awaited_once()
    bridge.stop.assert_awaited_once()
    assert daemon.companion_bridges == {}
    assert daemon.companion_frame_servers == []
    assert daemon.identity_manager.get_identity_by_name("frame-client") is None


@pytest.mark.asyncio
async def test_frame_enabled_requires_an_exact_boolean_before_runtime_setup():
    daemon = _daemon()
    config = {
        "name": "ambiguous",
        "identity_key": "34" * 32,
        "settings": {"frame_enabled": "false"},
    }

    with (
        patch("repeater.companion.RepeaterCompanionBridge") as bridge_cls,
        patch("repeater.companion.CompanionFrameServer") as server_cls,
        pytest.raises(ValueError, match="frame_enabled must be a boolean"),
    ):
        await daemon.add_companion_from_config(config)

    bridge_cls.assert_not_called()
    server_cls.assert_not_called()
