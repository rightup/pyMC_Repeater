"""A stand-in relay that captures what the push notifier sends.

Configure the notifier with this listener's URL, then register a device push
token. The notifier POSTs here instead of to a real relay, which makes the
whole chain assertable without APNs, a signing key, or a device:

    inbound message -> journal append -> notifier debounce -> POST -> here

Configure ``companion.push.relay_url`` with a real relay in production. Relay
selection is operator-owned; paired devices cannot make the repeater request
an arbitrary URL.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from companion_client.rest import strict_json_loads

MAX_PUSH_BODY_BYTES = 16 * 1024
MAX_CAPTURED_PUSHES = 256


@dataclass
class CapturedPush:
    body: dict
    sequence: int
    received_at: float = field(default_factory=time.time)

    @property
    def shape(self) -> str:
        """Classify the payload the same way the relay does."""
        if self.body.get("mention"):
            return "mention"
        if "badge_hint" in self.body:
            return "count"
        if "alert" in self.body:
            return "preview"
        return "wake"


class PushListener:
    """Captures notifier POSTs; can be told to answer with a chosen status.

    ``status`` is settable because the notifier branches on it: 410 makes it
    clear the device's push_token, 5xx makes it retry. Being able to script
    that is the point of having our own listener.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, status: int = 200) -> None:
        self.status = status
        self.pushes: deque[CapturedPush] = deque(maxlen=MAX_CAPTURED_PUSHES)
        self._push_sequence = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def _send_empty(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    self.close_connection = True
                    self._send_empty(411)
                    return
                try:
                    if not raw_length.isdecimal():
                        raise ValueError
                    length = int(raw_length)
                except (TypeError, ValueError, OverflowError):
                    self.close_connection = True
                    self._send_empty(400)
                    return
                if length > MAX_PUSH_BODY_BYTES:
                    self.close_connection = True
                    self._send_empty(413)
                    return

                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.close_connection = True
                    self._send_empty(400)
                    return
                try:
                    body = strict_json_loads(raw)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    self._send_empty(400)
                    return
                if not isinstance(body, dict):
                    self._send_empty(400)
                    return
                listener._record(body)
                self._send_empty(listener.status)

            def log_message(self, *args):
                pass  # keep pytest output clean

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://{host}:{self.port}/notify"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, body: dict) -> None:
        with self._condition:
            self._push_sequence += 1
            self.pushes.append(CapturedPush(body, self._push_sequence))
            self._condition.notify_all()

    def captured_after(self, sequence: int) -> tuple[int, list[CapturedPush]]:
        """Return retained pushes newer than one monotonic capture sequence."""

        with self._lock:
            return (
                self._push_sequence,
                [push for push in self.pushes if push.sequence > sequence],
            )

    def start(self) -> PushListener:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> PushListener:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- assertions --------------------------------------------------------

    def wait_for_push(self, count: int = 1, timeout: float = 5.0) -> bool:
        """Block until at least ``count`` pushes have arrived."""
        if type(count) is not int or not 1 <= count <= MAX_CAPTURED_PUSHES:
            raise ValueError(
                f"count must be an integer between 1 and {MAX_CAPTURED_PUSHES}"
            )
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.pushes) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def last(self) -> Optional[CapturedPush]:
        with self._lock:
            return self.pushes[-1] if self.pushes else None

    def clear(self) -> None:
        with self._lock:
            self.pushes.clear()
