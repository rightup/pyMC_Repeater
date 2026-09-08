"""Persistence must remove exactly the entry it persisted, not the queue tail.

Offline-queue pushes happen synchronously in sibling receive tasks while
``_persist_companion_message`` is suspended inside its ``asyncio.to_thread``
SQLite write. Removing the persisted entry with ``pop_last`` therefore races: a
newer entry pushed during the await becomes the tail and gets evicted instead,
duplicating the persisted message and losing the newer one. These tests pin the
identity-based removal that closes that window.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from openhop_core.companion.message_queue import MessageQueue
from openhop_core.companion.models import QueuedMessage
from openhop_core.protocol.constants import TXT_TYPE_PLAIN

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"
_TO_THREAD = "repeater.companion.frame_server.asyncio.to_thread"


class _Bridge:
    """Minimal bridge exposing a real in-memory offline queue."""

    def __init__(self, max_size: int = 100):
        self.message_queue = MessageQueue(max_size=max_size)


def _server(handler: SQLiteHandler, bridge: _Bridge) -> CompanionFrameServer:
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    fs.bridge = bridge
    return fs


def _msg(text: str, key_byte: bytes, packet_hash: str):
    """A queue entry and the matching persistence dict for one direct message."""
    entry = QueuedMessage(
        sender_key=key_byte * 32,
        txt_type=TXT_TYPE_PLAIN,
        timestamp=1000,
        text=text,
        is_channel=False,
        path_len=0xFF,
    )
    msg_dict = {
        "sender_key": entry.sender_key,
        "text": text,
        "timestamp": entry.timestamp,
        "txt_type": entry.txt_type,
        "is_channel": False,
        "channel_idx": 0,
        "path_len": entry.path_len,
        "packet_hash": packet_hash,
        "snr": 0.0,
        "rssi": 0,
        "sender_prefix": b"",
    }
    return entry, msg_dict


def _memory_texts(queue: MessageQueue) -> set[str]:
    """Drain the queue and return the set of retained message texts."""
    texts = set()
    while True:
        msg = queue.pop()
        if msg is None:
            break
        texts.add(msg.text)
    return texts


def _persisted_texts(handler: SQLiteHandler) -> set[str]:
    return {m["text"] for m in handler.companion_load_messages(_HASH)}


class _Gate:
    """Deterministic stand-in for ``asyncio.to_thread``.

    Each call registers a release event and suspends until the test releases it,
    then runs the wrapped function inline. This lets the test interleave two
    persistence coroutines and control which completes first.
    """

    def __init__(self):
        self.releases: list[asyncio.Event] = []

    async def to_thread(self, func, /, *args, **kwargs):
        rel = asyncio.Event()
        self.releases.append(rel)
        await rel.wait()
        return func(*args, **kwargs)

    async def release(self, idx: int) -> None:
        self.releases[idx].set()
        # Let the resumed coroutine run its synchronous remove/pop tail.
        for _ in range(3):
            await asyncio.sleep(0)


async def _settle() -> None:
    """Yield enough for a freshly created task to reach its ``to_thread`` gate."""
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["a_first", "b_first"])
async def test_interleaved_persist_no_dup_no_loss(tmp_path, order):
    """Two receive tasks persist concurrently; regardless of completion order the
    older message ends up only in SQLite and the newer only in memory."""
    handler = SQLiteHandler(tmp_path)
    bridge = _Bridge()
    fs = _server(handler, bridge)
    # Same packet_hash: the newer push is rejected as a SQLite duplicate, the
    # damage case where pop_last would duplicate the older and drop the newer.
    entry_old, dict_old = _msg("older", b"\x11", "DUP-HASH")
    entry_new, dict_new = _msg("newer", b"\x22", "DUP-HASH")

    gate = _Gate()
    with patch(_TO_THREAD, gate.to_thread):
        # Task A: base_events pushed the older entry, then fired persist.
        bridge.message_queue.push(entry_old)
        task_a = asyncio.create_task(fs._persist_companion_message(dict_old, entry_old))
        await _settle()
        # Task B (sibling): a newer message pushed during A's SQLite write.
        bridge.message_queue.push(entry_new)
        task_b = asyncio.create_task(fs._persist_companion_message(dict_new, entry_new))
        await _settle()

        if order == "a_first":
            await gate.release(0)
            await gate.release(1)
        else:
            await gate.release(1)
            await gate.release(0)
        await asyncio.gather(task_a, task_b)

    memory = _memory_texts(bridge.message_queue)
    persisted = _persisted_texts(handler)
    # Exactly one message persisted, the other retained: no message in both
    # stores (no duplication) and none dropped (no loss). Which of the two wins
    # the SQLite insert depends on completion order; the invariant does not.
    assert len(persisted) == 1
    assert memory & persisted == set()
    assert memory | persisted == {"older", "newer"}


@pytest.mark.asyncio
async def test_asymmetric_rejection_removes_persisted_not_newer(tmp_path):
    """msg1 persists, msg2 is rejected: msg1 is removed by identity and msg2 stays,
    even though msg2 is the queue tail pop_last would have taken."""
    handler = SQLiteHandler(tmp_path)
    bridge = _Bridge()
    fs = _server(handler, bridge)
    entry1, dict1 = _msg("msg1", b"\x11", "SAME-HASH")
    entry2, dict2 = _msg("msg2", b"\x22", "SAME-HASH")  # duplicate hash -> rejected

    bridge.message_queue.push(entry1)
    bridge.message_queue.push(entry2)

    await fs._persist_companion_message(dict1, entry1)  # accepted
    await fs._persist_companion_message(dict2, entry2)  # rejected (duplicate)

    memory = _memory_texts(bridge.message_queue)
    assert memory == {"msg2"}  # newer retained, msg1 removed exactly
    assert _persisted_texts(handler) == {"msg1"}  # msg1 persisted once, not duplicated


@pytest.mark.asyncio
async def test_cancelled_sibling_entry_survives_other_persist(tmp_path):
    """A sibling task cancelled after its push must keep its queue entry when
    another task's persist completes."""
    handler = SQLiteHandler(tmp_path)
    bridge = _Bridge()
    fs = _server(handler, bridge)
    entry1, dict1 = _msg("msg1", b"\x11", "HASH-1")
    entry2, dict2 = _msg("msg2", b"\x22", "HASH-2")

    gate = _Gate()
    with patch(_TO_THREAD, gate.to_thread):
        bridge.message_queue.push(entry1)
        task_a = asyncio.create_task(fs._persist_companion_message(dict1, entry1))
        await _settle()
        # Sibling pushed its entry, then its persist task is cancelled mid-write.
        bridge.message_queue.push(entry2)
        task_b = asyncio.create_task(fs._persist_companion_message(dict2, entry2))
        await _settle()
        task_b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_b
        # Task A now finishes its persist.
        await gate.release(0)
        await task_a

    memory = _memory_texts(bridge.message_queue)
    assert "msg2" in memory  # cancelled sibling's message retained
    assert "msg1" not in memory  # A removed only its own entry
    assert _persisted_texts(handler) == {"msg1"}
