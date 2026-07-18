"""A stand-in relay that captures what the push notifier sends.

The client registers this listener's URL as its ``push_relay_url``, so the
notifier POSTs here instead of to a real relay. That makes the whole chain
assertable without APNs, a signing key, or a device:

    inbound message -> journal append -> notifier debounce -> POST -> here

Point ``push_relay_url`` at a real relay instead and the same client covers
relay routing and APNs payload mapping too; this is the isolated-notifier end
of that spectrum.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


@dataclass
class CapturedPush:
    body: dict
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
        self.pushes: list[CapturedPush] = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = {"_unparseable": raw.decode("utf-8", errors="replace")}
                listener._record(body)
                self.send_response(listener.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass  # keep pytest output clean

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://{host}:{self.port}/notify"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, body: dict) -> None:
        with self._condition:
            self.pushes.append(CapturedPush(body))
            self._condition.notify_all()

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
