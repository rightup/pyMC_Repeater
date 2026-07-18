"""Harness: a real CompanionFrameServer over a real socket.

Existing companion tests build the frame server with ``__new__`` and hand-set
attributes, which is fine for unit-testing persistence hooks but never opens a
port. This harness runs the actual server so ``companion_client`` can drive it
over TCP -- closing the gap the handoff flags twice as untestable ("needs a
companion frame client, TCP 15050, out of scope for a curl smoke").

The bridge is a double. Everything above it -- framing, command dispatch,
SQLite persistence, the journal, and the push notifier -- is real.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


@dataclass
class FakePrefs:
    node_name: str = "TestNode"
    tx_power_dbm: int = 20
    latitude: float = 47.6062
    longitude: float = -122.3321
    multi_acks: int = 0
    advert_loc_policy: int = 0
    telemetry_mode_base: int = 0
    telemetry_mode_location: int = 0
    telemetry_mode_environment: int = 0
    manual_add_contacts: int = 0
    frequency_hz: int = 906_875_000
    bandwidth_hz: int = 250_000
    spreading_factor: int = 10
    coding_rate: int = 5


class FakeMessageQueue:
    """Stands in for the bridge's inbound queue."""

    def __init__(self, max_size: int = 50) -> None:
        self.max_size = max_size
        self.popped = 0
        self._items: list = []

    def push(self, item) -> None:
        self._items.append(item)

    def pop_last(self) -> None:
        self.popped += 1
        if self._items:
            self._items.pop()


class _FakeContactStore:
    max_contacts = 1000

    def __len__(self):
        return 0


class _FakeChannelStore:
    max_channels = 40


@dataclass
class FakeChannel:
    idx: int
    name: str = "Public"
    secret: bytes = b"\x00" * 16


# Mirrors the shape of a real deployment: slot 0 is the default Public channel
# and the rest are hash-prefixed group channels. Taken from the channel table a
# live dev repeater actually reports, so the simulator exercises the same
# indexing (sparse, name-keyed) rather than a single hardcoded channel.
DEFAULT_CHANNELS: tuple[tuple[int, str], ...] = (
    (0, "Public"),
    (1, "#howltest"),
    (2, "#seattle"),
    (3, "#weather"),
)


class FakeBridge:
    """Minimal bridge satisfying the frame server's command handlers.

    Outbound sends are recorded rather than transmitted -- there is no radio.
    """

    def __init__(self, public_key: Optional[bytes] = None, channels=None) -> None:
        # First byte is the companion hash the repeater keys everything by.
        self.public_key = public_key or (bytes([0xF5]) + bytes(range(31)))
        self.prefs = FakePrefs()
        self.message_queue = FakeMessageQueue()
        self.channels_by_idx = {
            idx: FakeChannel(idx, name, bytes([idx]) * 16)
            for idx, name in (channels if channels is not None else DEFAULT_CHANNELS)
        }
        self.sent_channel_messages: list[tuple[int, str, int]] = []
        self.sent_direct_messages: list[tuple[bytes, str, int]] = []
        self._pending_sync: list = []
        self.channel_send_ok = True
        self.callbacks: dict = {}
        self.contacts = _FakeContactStore()
        self.channels = _FakeChannelStore()

    def __getattr__(self, name: str):
        """Accept the event-registration surface without stubbing each one.

        The server wires up a dozen ``on_*`` callbacks at start-up. They are
        pure registration -- nothing here needs to fire them except via the
        explicit inject helpers -- so record and move on. Deliberately limited
        to ``on_*``: any other missing attribute is a real gap in this double
        and should raise rather than silently no-op.
        """
        if name.startswith("on_"):

            def _register(callback=None, *args, **kwargs):
                self.callbacks[name] = callback
                return None

            return _register
        raise AttributeError(name)

    def clear_push_callbacks(self) -> None:
        self.callbacks.clear()

    def get_max_tx_power_dbm(self) -> int:
        return 30

    def get_contacts(self):
        # The server persists contacts/channels on stop; without these it logs
        # a warning and carries on. Provide them to keep test output clean.
        return []

    def get_channels(self):
        return list(self.channels_by_idx.values())

    # -- identity ----------------------------------------------------------

    def get_self_info(self) -> FakePrefs:
        return self.prefs

    def get_public_key(self) -> bytes:
        return self.public_key

    def get_time(self) -> int:
        import time

        return int(time.time())

    # -- channels ----------------------------------------------------------

    def get_channel(self, idx: int):
        return self.channels_by_idx.get(idx)

    def set_channel(self, idx: int, name: str, secret: bytes) -> bool:
        """Add, rename, or clear a slot. An empty name clears it, matching the
        firmware convention the server's CHANNEL_INFO encoding relies on."""
        if idx >= _FakeChannelStore.max_channels:
            return False
        if not name:
            self.channels_by_idx.pop(idx, None)
        else:
            self.channels_by_idx[idx] = FakeChannel(idx, name, secret)
        return True

    async def send_channel_message(self, channel_idx: int, text: str, timestamp: int = 0) -> bool:
        self.sent_channel_messages.append((channel_idx, text, timestamp))
        return self.channel_send_ok

    # -- messages ----------------------------------------------------------

    def sync_next_message(self):
        return self._pending_sync.pop(0) if self._pending_sync else None

    def queue_for_sync(self, message) -> None:
        self._pending_sync.append(message)


@dataclass
class Harness:
    server: CompanionFrameServer
    bridge: FakeBridge
    handler: SQLiteHandler
    journal: CompanionEventJournal
    companion_hash: str
    port: int = 0
    _notifier: object = field(default=None, repr=False)

    async def inject_inbound_message(
        self, text: str, packet_hash: str, timestamp: int = 0, channel_idx: int = 0
    ) -> dict:
        """Simulate a message arriving from the mesh.

        This is the only step that has to reach inside the server: journal
        'message' events -- the thing the push notifier listens for -- come from
        inbound RF, and there is no radio here. Everything downstream (SQLite
        persistence, journal append, notifier debounce, relay POST) is real.
        """
        msg = {
            "text": text,
            "packet_hash": packet_hash,
            "timestamp": timestamp,
            "channel_idx": channel_idx,
            "is_channel": True,
        }
        await self.server._persist_companion_message(msg)
        return msg

    async def stop(self) -> None:
        await self.server.stop()


async def start_harness(
    tmp_path, *, companion_hash: str = "f5", port: int = 0, channels=None
) -> Harness:
    """Start a real frame server on ``port`` (0 = pick a free one)."""
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, companion_hash)
    bridge = FakeBridge(
        public_key=bytes([int(companion_hash, 16)]) + bytes(range(31)), channels=channels
    )

    server = CompanionFrameServer(
        bridge=bridge,
        companion_hash=companion_hash,
        port=port,
        bind_address="127.0.0.1",
        sqlite_handler=handler,
        journal=journal,
    )
    await server.start()
    bound_port = server._server.sockets[0].getsockname()[1]

    return Harness(
        server=server,
        bridge=bridge,
        handler=handler,
        journal=journal,
        companion_hash=companion_hash,
        port=bound_port,
    )


async def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Poll until ``predicate()`` is true or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
