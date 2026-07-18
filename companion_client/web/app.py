"""Web chat client -- a browser UI over the same client library the tests use.

Two modes:

* **sim** (default) -- starts an in-process frame server, journal, push notifier
  and capture listener, so you can send messages, inject inbound traffic, and
  *watch the push fire* as it happens. This is the mode that models the
  experience end to end without a radio.
* **live** -- connects to a real repeater's companion port. Sending is real;
  receiving depends on actual RF traffic, and pushes go wherever that device's
  registered relay points.

Run::

    python -m companion_client.web.app            # sim mode on :8800
    python -m companion_client.web.app --live --host 192.168.1.50 --port 15050

Browser updates arrive over SSE, which is a better fit than WebSockets here:
the stream is one-way (server -> page) and sending is an ordinary POST.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

from companion_client.client import CompanionClient
from companion_client.push_listener import PushListener

logger = logging.getLogger("companion_client.web")

INDEX = Path(__file__).parent / "index.html"


class ChatSession:
    """Holds the client, the simulator (if any), and the browser event stream."""

    def __init__(self, *, live: bool, host: str, port: int) -> None:
        self.live = live
        self.host = host
        self.port = port
        self.client: Optional[CompanionClient] = None
        self.listener: Optional[PushListener] = None
        self.harness = None
        self.notifier = None
        self.messages: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._push_seen = 0
        self._push_task: Optional[asyncio.Task] = None

    # -- startup -----------------------------------------------------------

    async def start(self, tmp_dir: Path) -> None:
        if self.live:
            self.client = CompanionClient(self.host, self.port)
            await self.client.connect()
        else:
            await self._start_sim(tmp_dir)

        self.client.on_message(self._handle_message)
        await self.client.list_channels()

    async def _start_sim(self, tmp_dir: Path) -> None:
        # Imported here so live mode never needs the repeater package.
        from companion_client.simulator import start_harness
        from repeater.companion.push_notifier import CompanionPushNotifier

        self.harness = await start_harness(tmp_dir)
        self.listener = PushListener().start()

        handler = self.harness.handler
        token_id = handler.create_api_token("web", "web-hash", scope="companion:x")
        handler.companion_device_create(
            self.harness.companion_hash, "web-device", "Web Client", token_id, platform="ios"
        )
        handler.companion_device_set_push(
            "web-device",
            "a" * 64,
            push_relay_url=self.listener.url,
            push_detail="count",
            mention_push=True,
            mention_keywords=["adam", "webclient"],
        )

        # A short interval keeps the demo responsive; production default is 30s.
        self.notifier = CompanionPushNotifier(handler, min_interval=3.0)
        self.notifier.start()
        self.harness.journal.register_listener(
            self.notifier.make_listener(self.harness.companion_hash)
        )

        self.client = CompanionClient("127.0.0.1", self.harness.port)
        await self.client.connect()
        self._push_task = asyncio.create_task(self._poll_pushes())

    async def stop(self) -> None:
        if self._push_task:
            self._push_task.cancel()
        if self.client:
            await self.client.close()
        if self.notifier:
            self.notifier.stop()
        if self.listener:
            self.listener.stop()
        if self.harness:
            await self.harness.stop()

    # -- events ------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def emit(self, kind: str, data: dict) -> None:
        event = {"kind": kind, "data": data, "at": time.time()}
        for queue in self._subscribers:
            queue.put_nowait(event)

    def channel_name(self, idx) -> str:
        for channel in self.client.channels or []:
            if channel.idx == idx:
                return channel.name
        return f"channel {idx}"

    def _handle_message(self, message) -> None:
        entry = {
            "text": message.text,
            "direction": "in",
            "channel": message.channel_idx,
            "channel_name": self.channel_name(message.channel_idx),
            "timestamp": message.timestamp,
            "snr": message.snr,
        }
        self.messages.append(entry)
        self.emit("message", entry)

    async def _poll_pushes(self) -> None:
        """Surface captured pushes to the browser.

        Polled rather than callback-driven because PushListener runs on its own
        threads; this keeps everything on the event loop.
        """
        try:
            while True:
                await asyncio.sleep(0.2)
                if self.listener is None:
                    continue
                while self._push_seen < len(self.listener.pushes):
                    push = self.listener.pushes[self._push_seen]
                    self._push_seen += 1
                    self.emit("push", {"shape": push.shape, "body": push.body})
        except asyncio.CancelledError:
            pass

    # -- actions -----------------------------------------------------------

    async def send(self, text: str, channel: int = 0) -> dict:
        await self.client.send_channel_message(channel, text)
        entry = {
            "text": text,
            "direction": "out",
            "channel": channel,
            "channel_name": self.channel_name(channel),
            "timestamp": int(time.time()),
        }
        self.messages.append(entry)
        self.emit("message", entry)
        return entry

    async def inject(self, text: str, channel: int = 0) -> None:
        """Simulate a message arriving from the mesh (sim mode only)."""
        if self.harness is None:
            raise web.HTTPBadRequest(reason="inject is only available in sim mode")
        await self.harness.inject_inbound_message(
            text, f"web-{time.time_ns()}", int(time.time()), channel_idx=channel
        )
        await self.client.drain_messages()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX.read_text(encoding="utf-8"), content_type="text/html")


async def state(request: web.Request) -> web.Response:
    session: ChatSession = request.app["session"]
    info = session.client.self_info
    return web.json_response(
        {
            "mode": "live" if session.live else "sim",
            "node_name": info.node_name if info else None,
            "companion_hash": info.companion_hash if info else None,
            "messages": session.messages,
            "channels": [{"idx": c.idx, "name": c.name} for c in (session.client.channels or [])],
            "can_inject": session.harness is not None,
        }
    )


async def events(request: web.Request) -> web.StreamResponse:
    session: ChatSession = request.app["session"]
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await response.prepare(request)
    queue = session.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")  # keep proxies from closing us
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        session.unsubscribe(queue)
    return response


async def send(request: web.Request) -> web.Response:
    session: ChatSession = request.app["session"]
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(reason="text is required")
    try:
        entry = await session.send(text, int(body.get("channel", 0)))
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(entry)


async def inject(request: web.Request) -> web.Response:
    session: ChatSession = request.app["session"]
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(reason="text is required")
    await session.inject(text, int(body.get("channel", 0)))
    return web.json_response({"ok": True})


def build_app(session: ChatSession) -> web.Application:
    app = web.Application()
    app["session"] = session
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", state),
            web.get("/api/events", events),
            web.post("/api/send", send),
            web.post("/api/inject", inject),
        ]
    )
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OpenHop companion web chat client")
    parser.add_argument("--live", action="store_true", help="connect to a real repeater")
    parser.add_argument("--host", default="127.0.0.1", help="repeater host (live mode)")
    parser.add_argument("--port", type=int, default=15050, help="companion port (live mode)")
    parser.add_argument("--web-port", type=int, default=8800, help="port to serve the UI on")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="companion-web-"))
    session = ChatSession(live=args.live, host=args.host, port=args.port)

    async def on_startup(app):
        await session.start(tmp_dir)
        info = session.client.self_info
        logger.info(
            "companion %s (0x%s) ready -- open http://127.0.0.1:%s",
            info.node_name,
            info.companion_hash,
            args.web_port,
        )

    async def on_cleanup(app):
        await session.stop()

    app = build_app(session)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="127.0.0.1", port=args.web_port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
