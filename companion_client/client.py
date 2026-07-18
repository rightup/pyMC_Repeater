"""Async companion frame client.

A reference client for the repeater's companion TCP interface (default port
15050). It is deliberately free of any ``repeater.*`` import: it speaks the
wire protocol and nothing else, so it exercises the server the way a real
phone app would rather than reaching inside it.

The protocol multiplexes two kinds of server→client frame on one socket:

* **responses** to a command we sent (codes < 0x80), strictly in order;
* **pushes** (codes >= 0x80) that arrive unsolicited at any time.

So a single reader task owns the socket, routing responses to whoever is
awaiting a command and pushes to registered handlers. Commands are serialised
behind a lock because the protocol has no request IDs -- the only thing tying
a response to a command is ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable, Optional

from openhop_core.companion.constants import (
    PUSH_CODE_MSG_WAITING,
    PUSH_CODE_SEND_CONFIRMED,
    RESP_CODE_CURR_TIME,
)

from . import protocol
from .protocol import ProtocolError, ReceivedMessage, SelfInfo

logger = logging.getLogger("companion_client")

PushHandler = Callable[[int, bytes], Optional[Awaitable[None]]]
MessageHandler = Callable[[ReceivedMessage], Optional[Awaitable[None]]]

DEFAULT_PORT = 15050


class CompanionClientError(Exception):
    pass


class CommandError(CompanionClientError):
    """The server answered a command with RESP_CODE_ERR."""

    def __init__(self, code: int) -> None:
        super().__init__(f"server returned error code {code}")
        self.code = code


class CompanionClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        response_timeout: float = 10.0,
        auto_sync: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.response_timeout = response_timeout
        # When set, a MSG_WAITING push drains the queue automatically and feeds
        # on_message -- which is how a real chat app behaves.
        self.auto_sync = auto_sync

        self.self_info: Optional[SelfInfo] = None
        self.messages: list[ReceivedMessage] = []

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._decoder = protocol.FrameDecoder()
        self._responses: asyncio.Queue[bytes] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._closing = False

        self._push_handlers: list[PushHandler] = []
        self._message_handlers: list[MessageHandler] = []
        # Pushes seen, for assertions in tests.
        self.pushes: list[tuple[int, bytes]] = []

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, *, app_target_ver: int = 3) -> SelfInfo:
        """Open the socket and perform the opening handshake.

        DEVICE_QUERY is sent before APP_START because it is what sets the
        server's app target version, which decides whether later message frames
        are the V3 form. APP_START then returns SELF_INFO.
        """
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._closing = False
        self._reader_task = asyncio.create_task(self._read_loop(), name="companion-client-reader")

        await self._command(protocol.cmd_device_query(app_target_ver))
        payload = await self._command(protocol.cmd_app_start())
        self.self_info = protocol.parse_self_info(payload)
        logger.info(
            "connected to %s:%s as companion 0x%s (%s)",
            self.host,
            self.port,
            self.self_info.companion_hash,
            self.self_info.node_name,
        )
        return self.self_info

    async def close(self) -> None:
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None

    async def __aenter__(self) -> CompanionClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    # -- handlers ----------------------------------------------------------

    def on_push(self, handler: PushHandler) -> PushHandler:
        self._push_handlers.append(handler)
        return handler

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        self._message_handlers.append(handler)
        return handler

    # -- commands ----------------------------------------------------------

    async def send_channel_message(self, channel_idx: int, text: str, *, timestamp=None) -> None:
        """Send a channel message. Raises :class:`CommandError` if rejected.

        Note the server reports *any* channel-send failure as NOT_FOUND, to
        match firmware -- so a NOT_FOUND here may mean an unknown channel or a
        radio that could not transmit.
        """
        payload = protocol.cmd_send_channel_text(
            channel_idx, text, int(timestamp if timestamp is not None else time.time())
        )
        await self._command(payload, expect_ok=True)

    async def send_direct_message(self, pubkey_prefix: bytes, text: str, *, timestamp=None) -> None:
        """Send a DM to a contact identified by its public key prefix."""
        payload = protocol.cmd_send_text(
            pubkey_prefix, text, int(timestamp if timestamp is not None else time.time())
        )
        # A DM returns RESP_CODE_SENT (with tag/timeout), not plain OK.
        await self._command(payload)

    async def sync_next_message(self) -> Optional[ReceivedMessage]:
        """Pull one queued message, or None when the queue is empty."""
        payload = await self._command(protocol.cmd_sync_next_message())
        return protocol.parse_message(payload)

    async def drain_messages(self, *, limit: int = 200) -> list[ReceivedMessage]:
        """Pull queued messages until the server says there are no more."""
        drained: list[ReceivedMessage] = []
        for _ in range(limit):
            message = await self.sync_next_message()
            if message is None:
                break
            drained.append(message)
            await self._dispatch_message(message)
        return drained

    async def _command(self, payload: bytes, *, expect_ok: bool = False) -> bytes:
        """Send one command and await its response.

        Serialised: the protocol has no request IDs, so two commands in flight
        at once would be impossible to match up to their replies.
        """
        if self._writer is None:
            raise CompanionClientError("not connected")
        async with self._cmd_lock:
            # Drop any stale response left by a previous timeout, so we don't
            # match this command against the wrong frame.
            while not self._responses.empty():
                stale = self._responses.get_nowait()
                logger.warning("discarding stale response frame 0x%02x", stale[0] if stale else -1)

            self._writer.write(protocol.encode_frame(payload))
            await self._writer.drain()
            try:
                response = await asyncio.wait_for(
                    self._responses.get(), timeout=self.response_timeout
                )
            except asyncio.TimeoutError as exc:
                raise CompanionClientError(
                    f"timed out waiting for response to command 0x{payload[0]:02x}"
                ) from exc

        code = protocol.error_code(response)
        if code is not None:
            raise CommandError(code)
        if expect_ok and not protocol.is_ok(response):
            raise CompanionClientError(
                f"expected OK, got frame 0x{response[0]:02x}" if response else "empty response"
            )
        return response

    # -- reader ------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    if not self._closing:
                        logger.warning("companion connection closed by server")
                    return
                for payload in self._decoder.feed(data):
                    await self._route(payload)
        except asyncio.CancelledError:
            raise
        except ProtocolError:
            logger.exception("protocol error; closing")
        except Exception:
            if not self._closing:
                logger.exception("reader loop failed")

    async def _route(self, payload: bytes) -> None:
        if not payload:
            return
        code = payload[0]

        # The server emits CURR_TIME as an unsolicited heartbeat when idle. It
        # is below the push range but is not a reply, so routing it to the
        # response queue would desynchronise every later command.
        if code == RESP_CODE_CURR_TIME:
            logger.debug("heartbeat")
            return

        if protocol.is_push(payload):
            self.pushes.append((code, payload))
            for handler in self._push_handlers:
                result = handler(code, payload)
                if result is not None:
                    await result
            if code == PUSH_CODE_MSG_WAITING and self.auto_sync:
                # Drain on a separate task: we may be inside the reader loop,
                # and draining issues commands that need the reader to run.
                asyncio.create_task(self._drain_safely())
            elif code == PUSH_CODE_SEND_CONFIRMED:
                logger.debug("send confirmed")
            return

        await self._responses.put(payload)

    async def _drain_safely(self) -> None:
        try:
            await self.drain_messages()
        except Exception:
            logger.exception("auto-sync drain failed")

    async def _dispatch_message(self, message: ReceivedMessage) -> None:
        self.messages.append(message)
        for handler in self._message_handlers:
            result = handler(message)
            if result is not None:
                await result
