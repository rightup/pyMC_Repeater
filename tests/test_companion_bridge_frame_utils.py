from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.companion.constants import PUSH_CODE_MSG_WAITING, RESP_CODE_NO_MORE_MESSAGES
from openhop_core.companion.models import MessageEvent

from repeater.companion.bridge import RepeaterCompanionBridge, _to_json_safe
from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.utils import normalize_companion_identity_key, validate_companion_node_name


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


def test_bridge_load_prefs_merges_known_fields_with_type_conversion():
    @dataclass
    class _Prefs:
        node_name: str = "orig"
        retries: int = 1
        enabled: bool = False
        ratio: float = 0.5

    stored = {
        "node_name": "new-name",
        "retries": "7",
        "enabled": 1,
        "ratio": "1.25",
        "unknown": "ignore",
        "retries_bad": "NaN",
    }
    sqlite = SimpleNamespace(companion_load_prefs=lambda _h: stored)

    bridge = object.__new__(RepeaterCompanionBridge)
    bridge._sqlite_handler = sqlite
    bridge._companion_hash = "hash"
    bridge.prefs = cast(Any, _Prefs())

    bridge._load_prefs()

    assert bridge.prefs.node_name == "new-name"
    assert cast(Any, bridge.prefs).retries == 7
    assert cast(Any, bridge.prefs).enabled is True
    assert cast(Any, bridge.prefs).ratio == 1.25


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
async def test_frame_server_persistence_paths_and_stop():
    sqlite = SimpleNamespace(
        companion_push_message=MagicMock(),
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
    bridge = SimpleNamespace(
        message_queue=SimpleNamespace(pop_last=MagicMock()),
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

        await srv._persist_companion_message({"text": "x"})
        sqlite.companion_push_message.assert_called_once_with("h", {"text": "x"}, None)
        bridge.message_queue.pop_last.assert_called_once()

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

        sqlite.companion_save_contacts.assert_called_once()
        sqlite.companion_save_channels.assert_called_once_with(
            "h", [{"channel_idx": 1, "name": "c1", "secret": "s"}]
        )
        base_stop.assert_awaited_once()


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
async def test_rejected_queue_callback_skips_sqlite_persistence_but_notifies_client():
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

    server._persist_companion_message.assert_not_awaited()
    server._enqueue_frame.assert_called_once_with(bytes([PUSH_CODE_MSG_WAITING]))


def test_companion_utils_validation_and_normalization():
    assert normalize_companion_identity_key(" 0xAABB ") == "AABB"
    assert validate_companion_node_name("  node-1  ") == "node-1"

    with pytest.raises(ValueError):
        validate_companion_node_name(cast(Any, 123))
    with pytest.raises(ValueError):
        validate_companion_node_name("   ")
    with pytest.raises(ValueError):
        validate_companion_node_name("x" * 32)
    with pytest.raises(ValueError):
        validate_companion_node_name("bad\nname")
