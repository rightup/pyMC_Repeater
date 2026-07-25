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
        --companion "test-companion"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import math
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional

try:
    from aiohttp import web
except ModuleNotFoundError:  # REST library use does not require the optional web demo.
    web = None

from companion_client.push_listener import PushListener
from companion_client.rest import CompanionRestClient, RestError

logger = logging.getLogger("companion_client.web")

INDEX = Path(__file__).parent / "index.html"

# How often to poll sync. A real app would use background refresh (~minutes)
# or the SSE /events endpoint; this is a demo, so it stays snappy.
SYNC_INTERVAL_SEC = 1.0
MAX_EVENT_SUBSCRIBERS = 16
MAX_LOCAL_REQUEST_BYTES = 64 * 1024
MAX_RENDERED_MESSAGES = 1_000
MAX_LOCAL_SEND_RESULTS = 1_000
_DEFINITE_NO_SEND_STATUSES = {400, 401, 403, 404, 405, 413, 415, 422, 429}


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
        # Scope browser crash-recovery records to this pairing ceremony as well
        # as the stable server-side device principal. The opaque generation is
        # local metadata: it keeps a later web session from offering an older
        # session's pending draft after an operator has resolved that session.
        self.pairing_generation = secrets.token_urlsafe(24)

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
        self._send_lock = asyncio.Lock()
        self._sent_entries: dict[str, tuple[str, int, dict]] = {}
        self._unresolved_send_keys: set[str] = set()
        self._stopped = False

    # -- startup -----------------------------------------------------------

    async def start(self, tmp_dir: Path) -> None:
        try:
            if self.live:
                if not self.admin_token:
                    raise ValueError(
                        "live mode needs an admin token: minting a pairing code "
                        "requires an operator, a device cannot bootstrap itself"
                    )
                self.client = CompanionRestClient(self.base_url)
            else:
                await self._start_sim(tmp_dir)

            try:
                await asyncio.to_thread(self._pair)
            finally:
                # The operator credential exists only to mint this pairing
                # ceremony. The paired device token owns every later call.
                self.admin_token = None
            await asyncio.to_thread(self._bootstrap)
            self._tasks.append(asyncio.create_task(self._sync_loop()))
            if self.listener is not None:
                self._tasks.append(asyncio.create_task(self._push_loop()))
        except BaseException:
            await self.stop()
            raise

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
        self.notifier = CompanionPushNotifier(
            self.harness.handler,
            min_interval=3.0,
            relay_url=self.listener.url,
            allow_insecure_http=True,
        )
        self.notifier.start()
        self.journal.register_listener(
            self.notifier.make_listener(
                self.harness.companion_hash,
                self.harness.bridge.get_public_key().hex(),
            )
        )

    def _pair(self) -> None:
        operator = CompanionRestClient(
            self.client.base_url,
            token=self.admin_token,
            timeout=self.client.timeout,
        )
        if any(
            device.get("device_id") == self.device_id
            for device in operator.devices()
        ):
            raise RuntimeError(
                f"device_id {self.device_id!r} is already paired. An earlier "
                "web demo may not have shut down cleanly. Reconcile any "
                "unresolved send in durable history, revoke that device as "
                "an operator, then restart; do not switch device ids merely "
                "to bypass this check."
            )
        started = self.client.pair_start(self.companion, self.admin_token)
        try:
            self.client.pair(
                started["code"],
                self.device_id,
                "Web Client",
                platform="ios",
                expected_fingerprint=started["fingerprint"],
            )
        except RestError as exc:
            if exc.status == 409:
                raise RuntimeError(
                    f"device_id {self.device_id!r} became paired while this "
                    "demo was starting. Reconcile and revoke that device, "
                    "then restart with a fresh pairing code."
                ) from exc
            raise
        if self.harness is not None:
            # Register push so the notifier has somewhere to deliver.
            self.client.register_push(
                self.device_id,
                push_token="a" * 64,
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
        self.messages = [
            self._to_entry(message)
            for message in data.get("messages", [])[-MAX_RENDERED_MESSAGES:]
        ]

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self.client is not None and self.client.token:
            if self._unresolved_send_keys:
                logger.error(
                    "not revoking the demo device because %d send outcome(s) "
                    "remain unresolved; reconcile history before replacing it",
                    len(self._unresolved_send_keys),
                )
            else:
                try:
                    await asyncio.to_thread(
                        self.client.revoke_device,
                        self.device_id,
                    )
                except Exception:
                    logger.warning(
                        "could not revoke ephemeral demo device %s",
                        self.device_id,
                        exc_info=True,
                    )
        if self.notifier:
            try:
                self.notifier.stop()
            except Exception:
                logger.warning("could not stop push notifier", exc_info=True)
            self.notifier = None
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                logger.warning("could not stop push listener", exc_info=True)
            self.listener = None
        if self.harness is not None:
            from companion_client.rest_simulator import stop_rest_harness

            try:
                await asyncio.to_thread(stop_rest_harness, self.harness)
            except Exception:
                logger.warning("could not stop REST simulator", exc_info=True)
            self.harness = None

    # -- events ------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        if len(self._subscribers) >= MAX_EVENT_SUBSCRIBERS:
            raise RuntimeError("too many local event streams")
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def emit(self, kind: str, data: dict) -> None:
        event = {"kind": kind, "data": data, "at": time.time()}
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    {
                        "kind": "resync",
                        "data": {"reason": "subscriber_overflow"},
                        "at": time.time(),
                    }
                )

    def channel_name(self, idx) -> str:
        for channel in self.channels:
            if channel.get("index") == idx:
                return channel.get("name", f"channel {idx}")
        return f"channel {idx}"

    def _to_entry(self, message: dict, *, direction: Optional[str] = None) -> dict:
        channel = message.get("channel_idx", 0) or 0
        wire_direction = message.get("direction")
        rendered_direction = direction or ("out" if wire_direction == "out" else "in")
        return {
            "id": message.get("id", message.get("message_id")),
            "text": message.get("text", ""),
            "direction": rendered_direction,
            "channel": channel,
            "channel_name": self.channel_name(channel),
            "timestamp": message.get("timestamp"),
            "state": message.get("state"),
            "packet_hash": message.get("packet_hash"),
        }

    def _message_by_id(self, message_id) -> Optional[dict]:
        if message_id is None:
            return None
        return next(
            (message for message in self.messages if message.get("id") == message_id),
            None,
        )

    def _trim_messages(self) -> None:
        excess = len(self.messages) - MAX_RENDERED_MESSAGES
        if excess > 0:
            del self.messages[:excess]

    def _remember_send_result(
        self,
        idempotency_key: str,
        text: str,
        channel: int,
        entry: dict,
    ) -> None:
        self._sent_entries[idempotency_key] = (text, channel, entry)
        while len(self._sent_entries) > MAX_LOCAL_SEND_RESULTS:
            self._sent_entries.pop(next(iter(self._sent_entries)))

    async def _sync_loop(self) -> None:
        """Follow the journal from the snapshot cursor.

        This is the REST equivalent of the frame protocol's MSG_WAITING push:
        the client polls `sync` and applies whatever events arrived.
        """
        while True:
            try:
                await asyncio.sleep(SYNC_INTERVAL_SEC)
                await self._sync_to_head()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("sync loop error")

    async def _sync_to_head(self) -> None:
        """Drain every available page so sustained ingress cannot outrun us."""

        while True:
            result = await asyncio.to_thread(
                self.client.sync,
                self.companion,
                self.cursor,
            )
            if result.snapshot_required:
                # Cursor fell below the prune floor: the delta would be
                # silently incomplete, so re-bootstrap rather than limp on.
                logger.warning("snapshot_required; re-bootstrapping")
                await asyncio.to_thread(self._bootstrap)
                self.emit("resync", {})
                return
            for event in result.events:
                self._apply_event(event)
            self.cursor = result.next_cursor
            if not result.has_more:
                return

    def _apply_event(self, event: dict) -> None:
        kind = event.get("type")
        data = event.get("data", {}) or {}
        if kind == "message":
            entry = self._to_entry(data)
            if self._message_by_id(entry.get("id")) is not None:
                return
            self.messages.append(entry)
            self._trim_messages()
            self.emit("message", entry)
        elif kind == "message_send_state":
            existing = self._message_by_id(data.get("message_id"))
            if existing is not None:
                for field in ("state", "packet_hash"):
                    if field in data:
                        existing[field] = data[field]
                self.emit("message_state", data)
        elif kind == "channel":
            # The event this UI exists to prove: channel changes now reach a
            # syncing client instead of needing a re-snapshot.
            self._apply_channel_change(data)
        elif kind == "contact":
            self._apply_contact_change(data)
            self.emit("contact", data)
            self.emit("state", _state_data(self))
        elif kind == "prefs":
            self.self_info.update(data)
            self.emit("state", _state_data(self))

    def _apply_contact_change(self, data: dict) -> None:
        public_key = data.get("public_key")
        if not isinstance(public_key, str) or not public_key:
            return
        existing = next(
            (
                contact
                for contact in self.contacts
                if contact.get("public_key") == public_key
            ),
            None,
        )
        if data.get("change") == "remove":
            self.contacts = [
                contact
                for contact in self.contacts
                if contact.get("public_key") != public_key
            ]
            return
        contact_update = {
            field: value for field, value in data.items() if field != "change"
        }
        if existing is None:
            self.contacts.append(contact_update)
        else:
            existing.update(contact_update)
        self.contacts.sort(key=lambda contact: contact.get("name", "").casefold())

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
                latest_sequence, pushes = self.listener.captured_after(
                    self._push_seen
                )
                self._push_seen = latest_sequence
                for push in pushes:
                    self.emit("push", {"shape": push.shape, "body": push.body})
        except asyncio.CancelledError:
            return

    # -- actions -----------------------------------------------------------

    async def send(self, text: str, channel: int, idempotency_key: str) -> dict:
        """Send one browser draft, safely replaying the same draft key.

        The browser persists the key before this method runs. The small local
        cache prevents a lost browser-facing response from rendering the same
        server replay twice in this demo.
        """
        async with self._send_lock:
            previous = self._sent_entries.get(idempotency_key)
            if previous is not None:
                previous_text, previous_channel, previous_entry = previous
                if (previous_text, previous_channel) != (text, channel):
                    raise ValueError(
                        "idempotency_key is already bound to another draft"
                    )
                return previous_entry

            self._unresolved_send_keys.add(idempotency_key)
            try:
                result = await asyncio.to_thread(
                    self.client.send_message,
                    self.companion,
                    text,
                    channel_idx=channel,
                    idempotency_key=idempotency_key,
                )
            except RestError as exc:
                if exc.status in _DEFINITE_NO_SEND_STATUSES:
                    self._unresolved_send_keys.discard(idempotency_key)
                raise
            except ValueError:
                # Client-side validation happens before network I/O, so the
                # draft is definitively unsent and must not block cleanup.
                self._unresolved_send_keys.discard(idempotency_key)
                raise
            self._unresolved_send_keys.discard(idempotency_key)
            message_id = result["message_id"]
            entry = self._message_by_id(message_id)
            if entry is None:
                entry = {
                    "id": message_id,
                    "text": text,
                    "direction": "out",
                    "channel": channel,
                    "channel_name": self.channel_name(channel),
                    "timestamp": int(time.time()),
                    "state": result.get("state"),
                    "packet_hash": result.get("packet_hash"),
                }
                self.messages.append(entry)
                self._trim_messages()
                self.emit("message", entry)
            else:
                entry.update(
                    {
                        "state": result.get("state"),
                        "packet_hash": result.get("packet_hash"),
                    }
                )
            self._remember_send_result(idempotency_key, text, channel, entry)
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

SESSION_KEY = web.AppKey("session", ChatSession) if web is not None else "session"


def _is_local_hostname(value: Optional[str]) -> bool:
    if not value:
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


async def local_only(request: web.Request, handler):
    """Reject cross-site and DNS-rebinding access to the loopback demo."""

    try:
        request_host = urllib.parse.urlsplit(f"//{request.host}").hostname
    except ValueError as exc:
        raise web.HTTPForbidden(reason="valid loopback Host required") from exc
    if not _is_local_hostname(request_host):
        raise web.HTTPForbidden(reason="loopback Host required")
    origin = request.headers.get("Origin")
    if origin:
        try:
            parsed_origin = urllib.parse.urlsplit(origin)
        except ValueError as exc:
            raise web.HTTPForbidden(reason="valid same-origin request required") from exc
        if (
            parsed_origin.scheme.lower() != request.scheme.lower()
            or not _is_local_hostname(parsed_origin.hostname)
            or parsed_origin.netloc.lower() != request.host.lower()
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.username is not None
            or parsed_origin.password is not None
        ):
            raise web.HTTPForbidden(reason="same-origin request required")
    return await handler(request)


async def prepare_security_headers(
    request: web.Request,
    response: web.StreamResponse,
) -> None:
    """Keep the loopback control UI isolated when opened in a browser."""

    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
        "object-src 'none'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'",
    )


async def _json_object(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(reason="Content-Type must be application/json")

    def reject_duplicate_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value):
        raise ValueError(f"non-finite JSON number: {value}")

    def parse_finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number: {value}")
        return parsed

    try:
        body = json.loads(
            await request.text(),
            object_pairs_hook=reject_duplicate_fields,
            parse_constant=reject_nonfinite,
            parse_float=parse_finite_float,
        )
    except (LookupError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise web.HTTPBadRequest(reason="body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="body must be a JSON object")
    return body


async def index(request: web.Request) -> web.Response:
    return web.Response(
        text=INDEX.read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def _state_data(session: ChatSession) -> dict:
    return {
        "mode": "live" if session.live else "sim",
        "node_name": session.self_info.get("node_name"),
        "companion_hash": session.self_info.get("public_key", "")[:2],
        "companion": session.companion,
        "transport": "rest",
        "api_base_url": session.client.base_url,
        "companion_identity": session.self_info.get("public_key"),
        "device_id": session.device_id,
        "pairing_generation": session.pairing_generation,
        "messages": session.messages,
        "channels": session.channels,
        "contacts": session.contacts,
        "cursor": session.cursor,
        "can_inject": session.harness is not None,
    }


async def state(request: web.Request) -> web.Response:
    session: ChatSession = request.app[SESSION_KEY]
    return web.json_response(
        _state_data(session),
        headers={"Cache-Control": "no-store"},
    )


async def events(request: web.Request) -> web.StreamResponse:
    session: ChatSession = request.app[SESSION_KEY]
    try:
        queue = session.subscribe()
    except RuntimeError as exc:
        raise web.HTTPServiceUnavailable(
            reason=str(exc),
            headers={"Retry-After": "1"},
        ) from exc
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    try:
        await response.prepare(request)
        initial = {"kind": "state", "data": _state_data(session), "at": time.time()}
        await response.write(f"data: {json.dumps(initial)}\n\n".encode())
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await response.write(f"data: {json.dumps(event)}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except ConnectionError:
        pass
    except asyncio.CancelledError:
        raise
    finally:
        session.unsubscribe(queue)
    return response


async def send(request: web.Request) -> web.Response:
    session: ChatSession = request.app[SESSION_KEY]
    body = await _json_object(request)
    text_value = body.get("text")
    if not isinstance(text_value, str) or not text_value.strip():
        raise web.HTTPBadRequest(reason="text is required")
    text = text_value.strip()
    if body.get("companion") != session.companion:
        raise web.HTTPBadRequest(reason="companion does not match this session")
    if body.get("api_base_url") != session.client.base_url:
        raise web.HTTPBadRequest(reason="API server does not match this session")
    if body.get("companion_identity") != session.self_info.get("public_key"):
        raise web.HTTPBadRequest(reason="companion identity does not match this session")
    if body.get("device_id") != session.device_id:
        raise web.HTTPBadRequest(reason="device principal does not match this session")
    if body.get("pairing_generation") != session.pairing_generation:
        raise web.HTTPBadRequest(reason="pairing generation does not match this session")
    idempotency_key = body.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise web.HTTPBadRequest(reason="idempotency_key is required")
    if (
        len(idempotency_key) > 128
        or any(
            not 0x21 <= ord(character) <= 0x7E
            for character in idempotency_key
        )
    ):
        raise web.HTTPBadRequest(reason="idempotency_key is invalid")
    channel_value = body.get("channel", 0)
    if type(channel_value) is not int or channel_value < 0:
        raise web.HTTPBadRequest(reason="channel must be a non-negative integer")
    try:
        entry = await session.send(
            text,
            channel_value,
            idempotency_key,
        )
    except ValueError as exc:
        raise web.HTTPConflict(reason=str(exc)) from exc
    except RestError as exc:
        safe_to_edit = exc.status in _DEFINITE_NO_SEND_STATUSES
        return web.json_response(
            {
                "error": str(exc),
                "status": exc.status,
                "data": exc.data,
                "retry_after": exc.headers.get("Retry-After"),
                "safe_to_edit": safe_to_edit,
            },
            status=502,
        )
    return web.json_response(entry)


async def inject(request: web.Request) -> web.Response:
    session: ChatSession = request.app[SESSION_KEY]
    body = await _json_object(request)
    text_value = body.get("text")
    if not isinstance(text_value, str) or not text_value.strip():
        raise web.HTTPBadRequest(reason="text is required")
    channel = body.get("channel", 0)
    if type(channel) is not int or channel < 0:
        raise web.HTTPBadRequest(reason="channel must be a non-negative integer")
    await session.inject(text_value.strip(), channel)
    return web.json_response({"ok": True})


async def set_channel(request: web.Request) -> web.Response:
    session: ChatSession = request.app[SESSION_KEY]
    body = await _json_object(request)
    index_value = body.get("index", 0)
    name_value = body.get("name", "")
    if type(index_value) is not int or index_value < 0:
        raise web.HTTPBadRequest(reason="index must be a non-negative integer")
    if not isinstance(name_value, str):
        raise web.HTTPBadRequest(reason="name must be a string")
    await session.set_channel(index_value, name_value.strip())
    return web.json_response({"ok": True})


def build_app(session: ChatSession) -> web.Application:
    if web is None:
        raise RuntimeError(
            "the web demo needs aiohttp; install the companion-web optional dependency"
        )
    app = web.Application(
        middlewares=[web.middleware(local_only)],
        client_max_size=MAX_LOCAL_REQUEST_BYTES,
    )
    app.on_response_prepare.append(prepare_security_headers)
    app[SESSION_KEY] = session
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
    parser.add_argument("--device-id", default="web-client")
    parser.add_argument("--web-port", type=int, default=8800)
    args = parser.parse_args(argv)
    if web is None:
        parser.error(
            "the web demo needs aiohttp; install with: pip install -e '.[companion-web]'"
        )
    if args.live and not args.companion.strip():
        parser.error("--companion is required in live mode")
    if not 1 <= args.web_port <= 65535:
        parser.error("--web-port must be between 1 and 65535")
    # Consume rather than retain the non-interactive credential so it cannot
    # leak to any child process started later in this demo's lifetime.
    admin_token = os.environ.pop("OPENHOP_ADMIN_TOKEN", None)
    if args.live and not admin_token:
        admin_token = getpass.getpass("Admin token: ").strip()
        if not admin_token:
            parser.error("live mode needs an admin token")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="companion-web-") as tmp_dir_text:
        tmp_dir = Path(tmp_dir_text)
        session = ChatSession(
            live=args.live,
            base_url=args.base_url,
            companion=args.companion.strip(),
            admin_token=admin_token,
            device_id=args.device_id,
        )
        admin_token = None

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
