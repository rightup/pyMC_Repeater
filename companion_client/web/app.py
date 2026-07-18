"""Web chat client over the Mobile Companion API v1 (``/api/v1``).

This models what a phone app actually does: pair once for a device token,
`snapshot` to bootstrap (self, contacts, channels, recent messages), then
follow the journal with `sync` from the returned cursor, and send with
`POST .../messages` carrying an Idempotency-Key.

It deliberately does **not** use the TCP frame protocol
(:mod:`companion_client.client`). That is a separate, lower-level interface;
the REST tree is the surface a first-party mobile companion lives on, and the
two are not equivalent -- the frame protocol hands out channel PSK secrets,
the REST snapshot does not.

Two modes:

* **sim** (default) -- mounts the real `/api/v1` tree in-process with a real
  SQLiteHandler, journal and push notifier, plus a capture listener. Send
  messages, simulate inbound traffic, and watch the resulting push.
* **live** -- points at a running repeater. Needs an admin API token to mint
  the pairing code; sending transmits over the air.

Run::

    python -m companion_client.web.app
    python -m companion_client.web.app --live --base-url http://127.0.0.1:8000 \\
        --companion "TestCompanion" --admin-token <token>
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

from companion_client.push_listener import PushListener
from companion_client.rest import CompanionRestClient, RestError

logger = logging.getLogger("companion_client.web")

INDEX = Path(__file__).parent / "index.html"

# How often to poll sync. A real app would use background refresh (~minutes)
# or the SSE /events endpoint; this is a demo, so it stays snappy.
SYNC_INTERVAL_SEC = 1.0


class ChatSession:
    def __init__(
        self,
        *,
        live: bool,
        base_url: str,
        companion: str,
        admin_token: Optional[str],
        device_id: str = "web-client",
    ) -> None:
        self.live = live
        self.base_url = base_url
        self.companion = companion
        self.admin_token = admin_token
        self.device_id = device_id

        self.client: Optional[CompanionRestClient] = None
        self.harness = None
        self.listener: Optional[PushListener] = None
        self.notifier = None
        self.journal = None

        self.self_info: dict = {}
        self.channels: list[dict] = []
        self.contacts: list[dict] = []
        self.messages: list[dict] = []
        self.cursor: str = "0"

        self._subscribers: list[asyncio.Queue] = []
        self._push_seen = 0
        self._tasks: list[asyncio.Task] = []

    # -- startup -----------------------------------------------------------

    async def start(self, tmp_dir: Path) -> None:
        if self.live:
            if not self.admin_token:
                raise SystemExit(
                    "live mode needs --admin-token: minting a pairing code "
                    "requires an operator, a device cannot bootstrap itself"
                )
            self.client = CompanionRestClient(self.base_url)
        else:
            await self._start_sim(tmp_dir)

        await asyncio.to_thread(self._pair)
        await asyncio.to_thread(self._bootstrap)
        self._tasks.append(asyncio.create_task(self._sync_loop()))
        if self.listener is not None:
            self._tasks.append(asyncio.create_task(self._push_loop()))

    async def _start_sim(self, tmp_dir: Path) -> None:
        # Imported here so live mode never needs the repeater package.
        from companion_client.rest_simulator import start_rest_harness
        from repeater.companion.journal import CompanionEventJournal
        from repeater.companion.push_notifier import CompanionPushNotifier

        self.harness = await asyncio.to_thread(start_rest_harness, tmp_dir)
        self.base_url = self.harness.base_url
        self.companion = self.harness.companion_name
        self.admin_token = self.harness.admin_token()
        self.client = CompanionRestClient(self.base_url)

        self.listener = PushListener().start()
        self.journal = CompanionEventJournal(self.harness.handler, self.harness.companion_hash)
        # Short interval keeps the demo responsive; production default is 30s.
        self.notifier = CompanionPushNotifier(self.harness.handler, min_interval=3.0)
        self.notifier.start()
        self.journal.register_listener(self.notifier.make_listener(self.harness.companion_hash))

    def _pair(self) -> None:
        code = self.client.pair_start(self.companion, self.admin_token)["code"]
        self.client.pair(code, self.device_id, "Web Client", platform="ios")
        if self.harness is not None:
            # Register push so the notifier has somewhere to deliver.
            self.client.register_push(
                self.device_id,
                push_token="a" * 64,
                push_relay_url=self.listener.url,
                push_detail="count",
                mention_push=True,
                mention_keywords=["adam", "webclient"],
            )

    def _bootstrap(self) -> None:
        data, _etag = self.client.snapshot(self.companion)
        self.self_info = data.get("self", {})
        self.channels = data.get("channels", [])
        self.contacts = data.get("contacts", [])
        self.cursor = str(data.get("cursor", "0"))
        for message in data.get("messages", []):
            self.messages.append(self._to_entry(message, direction="in"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.notifier:
            self.notifier.stop()
        if self.listener:
            self.listener.stop()
        if self.harness is not None:
            from companion_client.rest_simulator import stop_rest_harness

            await asyncio.to_thread(stop_rest_harness, self.harness)

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
        for channel in self.channels:
            if channel.get("index") == idx:
                return channel.get("name", f"channel {idx}")
        return f"channel {idx}"

    def _to_entry(self, message: dict, *, direction: str) -> dict:
        channel = message.get("channel_idx", 0) or 0
        return {
            "text": message.get("text", ""),
            "direction": direction,
            "channel": channel,
            "channel_name": self.channel_name(channel),
            "timestamp": message.get("timestamp"),
        }

    async def _sync_loop(self) -> None:
        """Follow the journal from the snapshot cursor.

        This is the REST equivalent of the frame protocol's MSG_WAITING push:
        the client polls `sync` and applies whatever events arrived.
        """
        while True:
            try:
                await asyncio.sleep(SYNC_INTERVAL_SEC)
                result = await asyncio.to_thread(self.client.sync, self.companion, self.cursor)
                if result.snapshot_required:
                    # Cursor fell below the prune floor: the delta would be
                    # silently incomplete, so re-bootstrap rather than limp on.
                    logger.warning("snapshot_required; re-bootstrapping")
                    await asyncio.to_thread(self._bootstrap)
                    self.emit("resync", {})
                    continue
                self.cursor = result.next_cursor
                for event in result.events:
                    self._apply_event(event)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("sync loop error")

    def _apply_event(self, event: dict) -> None:
        kind = event.get("type")
        data = event.get("data", {}) or {}
        if kind == "message":
            entry = self._to_entry(data, direction="in")
            self.messages.append(entry)
            self.emit("message", entry)
        elif kind == "channel":
            # The event this UI exists to prove: channel changes now reach a
            # syncing client instead of needing a re-snapshot.
            self._apply_channel_change(data)
        elif kind == "contact":
            self.emit("contact", data)

    def _apply_channel_change(self, data: dict) -> None:
        index, name = data.get("index"), data.get("name")
        self.channels = [c for c in self.channels if c.get("index") != index]
        if data.get("change") != "remove" and name:
            self.channels.append({"index": index, "name": name})
        self.channels.sort(key=lambda c: c.get("index", 0))
        self.emit("channels", {"channels": self.channels})

    async def _push_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
                while self._push_seen < len(self.listener.pushes):
                    push = self.listener.pushes[self._push_seen]
                    self._push_seen += 1
                    self.emit("push", {"shape": push.shape, "body": push.body})
        except asyncio.CancelledError:
            return

    # -- actions -----------------------------------------------------------

    async def send(self, text: str, channel: int = 0) -> dict:
        await asyncio.to_thread(self.client.send_message, self.companion, text, channel_idx=channel)
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
        """Simulate inbound mesh traffic (sim mode only).

        Writes the message and journals it, which is what an inbound RF
        message does -- so it flows to this client through `sync` and to the
        push notifier at the same time.
        """
        if self.harness is None:
            raise web.HTTPBadRequest(reason="inject is only available in sim mode")
        message = {
            "text": text,
            "timestamp": int(time.time()),
            "packet_hash": f"web{time.time_ns():x}"[:16],
            "channel_idx": channel,
            "is_channel": True,
        }
        handler = self.harness.handler
        await asyncio.to_thread(
            handler.companion_push_message, self.harness.companion_hash, message, 50
        )
        await asyncio.to_thread(self.journal.record_message, message)

    async def set_channel(self, index: int, name: str) -> None:
        """Journal a channel change, to show it reaching this client via sync."""
        if self.harness is None:
            raise web.HTTPBadRequest(reason="only available in sim mode")
        change = "remove" if not name else "update"
        await asyncio.to_thread(self.journal.record_channel, index, name or None, change)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX.read_text(encoding="utf-8"), content_type="text/html")


async def state(request: web.Request) -> web.Response:
    session: ChatSession = request.app["session"]
    return web.json_response(
        {
            "mode": "live" if session.live else "sim",
            "node_name": session.self_info.get("node_name"),
            "companion_hash": session.self_info.get("public_key", "")[:2],
            "companion": session.companion,
            "transport": "rest",
            "messages": session.messages,
            "channels": session.channels,
            "contacts": session.contacts,
            "cursor": session.cursor,
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
                await response.write(b": keepalive\n\n")
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
    except RestError as exc:
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


async def set_channel(request: web.Request) -> web.Response:
    session: ChatSession = request.app["session"]
    body = await request.json()
    await session.set_channel(int(body.get("index", 0)), (body.get("name") or "").strip())
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
            web.post("/api/set_channel", set_channel),
        ]
    )
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="OpenHop companion web chat client (REST)")
    parser.add_argument("--live", action="store_true", help="use a running repeater")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="repeater base URL")
    parser.add_argument("--companion", default="", help="companion identity name (live mode)")
    parser.add_argument("--admin-token", default=None, help="admin API token (live mode)")
    parser.add_argument("--device-id", default="web-client")
    parser.add_argument("--web-port", type=int, default=8800)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="companion-web-"))
    session = ChatSession(
        live=args.live,
        base_url=args.base_url,
        companion=args.companion,
        admin_token=args.admin_token,
        device_id=args.device_id,
    )

    async def on_startup(app):
        await session.start(tmp_dir)
        logger.info(
            "paired with %s via REST -- open http://127.0.0.1:%s",
            session.companion,
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
