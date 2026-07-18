"""Mounts the real Mobile Companion API v1 tree on a real HTTP port.

The existing endpoint tests call handlers through ``__wrapped__``, which
bypasses ``require_auth`` and CherryPy entirely. That is the right shape for
unit-testing handler logic, but it means nothing exercises the surface a phone
app actually meets: HTTP routing, the auth gate, pairing-code exchange, bearer
tokens, scope enforcement, ETag/304 round trips, or the JSON envelope.

This harness stands up ``MobileAPIEndpoints`` under ``/api/v1`` with a real
``SQLiteHandler``, a real token manager and a real JWT handler, so
``companion_client.rest`` can drive it over the wire.

Like :mod:`companion_client.simulator`, the bridge is a double -- everything
above it is the shipping code.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import cherrypy

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.mobile_endpoints import MobileAPIEndpoints

logger = logging.getLogger("companion_client.rest_simulator")

DEFAULT_COMPANION_NAME = "comp-test"
DEFAULT_HASH_BYTE = 0x01


class _FakeIdentity:
    def __init__(self, hash_byte: int) -> None:
        self._pubkey = bytes([hash_byte]) + b"\x22" * 31

    def get_public_key(self) -> bytes:
        return self._pubkey


class _FakeChannels:
    """Channel table exposed to the snapshot endpoint."""

    max_channels = 8

    def __init__(self, channels: dict) -> None:
        self._channels = channels

    def get(self, idx: int):
        return self._channels.get(idx)


class RestFakeBridge:
    def __init__(self, hash_byte: int = DEFAULT_HASH_BYTE) -> None:
        self._pubkey = bytes([hash_byte]) + b"\x22" * 31
        self.prefs = SimpleNamespace(
            node_name="TestNode", adv_type=1, latitude=47.6, longitude=-122.3
        )
        self._channel_map = {
            0: SimpleNamespace(name="Public", secret=b"\x00" * 16),
            1: SimpleNamespace(name="#howltest", secret=b"\x11" * 16),
        }
        self.channels = _FakeChannels(self._channel_map)
        self.sent: list = []
        self.contacts_list = [
            SimpleNamespace(
                public_key=b"\xaa" * 32,
                name="Alice",
                adv_type=1,
                flags=0,
                out_path_len=-1,
                last_advert_timestamp=123,
                lastmod=124,
                gps_lat=0.0,
                gps_lon=0.0,
            )
        ]

    def get_public_key(self) -> bytes:
        return self._pubkey

    def get_self_info(self):
        return self.prefs

    def get_contacts(self):
        return self.contacts_list

    async def send_channel_message(self, channel_idx: int, text: str, **kwargs):
        """Record an outbound channel send. No radio, so nothing transmits.

        Async because the endpoint awaits the bridge call on the daemon's
        event loop (``_send_and_capture``).
        """
        self.sent.append({"channel_idx": channel_idx, "text": text})
        return True

    async def send_text(self, pub_key, text: str, **kwargs):
        self.sent.append({"to": bytes(pub_key).hex(), "text": text})
        return True

    def set_channel_entry(self, idx: int, name: Optional[str]) -> None:
        """Mutate the channel table the snapshot reads, for drift tests."""
        if name is None:
            self._channel_map.pop(idx, None)
        else:
            self._channel_map[idx] = SimpleNamespace(name=name, secret=b"\x22" * 16)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class RestHarness:
    base_url: str
    handler: SQLiteHandler
    bridge: RestFakeBridge
    companion_name: str
    companion_hash: str
    token_manager: object
    jwt_handler: object
    event_loop: object = None

    def admin_token(self, name: str = "admin-token") -> str:
        """Mint an admin-scope API token, standing in for an operator's JWT.

        Pairing has to be started by an authenticated operator; a device token
        cannot bootstrap itself.
        """
        _token_id, plaintext = self.token_manager.create_token(name=name, scope="admin")
        return plaintext


def start_rest_harness(
    tmp_path,
    *,
    companion_name: str = DEFAULT_COMPANION_NAME,
    hash_byte: int = DEFAULT_HASH_BYTE,
) -> RestHarness:
    """Mount /api/v1 on a free port and return a handle to it."""
    from repeater.web.auth.api_tokens import APITokenManager
    from repeater.web.auth.jwt_handler import JWTHandler

    handler = SQLiteHandler(tmp_path)
    bridge = RestFakeBridge(hash_byte)
    companion_hash = f"0x{hash_byte:02x}"

    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: (
            [(companion_name, _FakeIdentity(hash_byte), {})] if t == "companion" else []
        )
    )
    daemon = SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={hash_byte: bridge},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )

    token_manager = APITokenManager(handler, secret_key="test-secret-not-for-production")
    jwt_handler = JWTHandler(secret="test-secret-not-for-production")

    port = _free_port()
    cherrypy.config.update(
        {
            "server.socket_host": "127.0.0.1",
            "server.socket_port": port,
            "engine.autoreload.on": False,
            "log.screen": False,
            "log.access_file": "",
            "log.error_file": "",
            # Match production (web/http_server.py): without this CherryPy
            # 301-redirects object paths like /pair to /pair/, and stock HTTP
            # clients downgrade POST to GET when following it. The real server
            # disables it globally, so a harness that omits it is unfaithful
            # and invents failures the deployment does not have.
            "tools.trailing_slash.on": False,
            "jwt_handler": jwt_handler,
            "token_manager": token_manager,
        }
    )

    # POST /messages dispatches a coroutine onto the daemon's loop
    # (_send_and_capture), so the harness needs a real one running.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="rest-harness-loop")
    thread.start()

    root = SimpleNamespace()
    root.api = SimpleNamespace()
    root.api.v1 = MobileAPIEndpoints(daemon_instance=daemon, config={}, event_loop=loop)

    cherrypy.tree.mount(root, "/", {"/": {"request.dispatch": cherrypy.dispatch.Dispatcher()}})
    cherrypy.engine.start()

    return RestHarness(
        base_url=f"http://127.0.0.1:{port}",
        handler=handler,
        bridge=bridge,
        companion_name=companion_name,
        companion_hash=companion_hash,
        token_manager=token_manager,
        jwt_handler=jwt_handler,
        event_loop=loop,
    )


def stop_rest_harness(harness: Optional[RestHarness] = None) -> None:
    cherrypy.engine.exit()
    cherrypy.tree.apps.clear()
    if harness is not None and harness.event_loop is not None:
        harness.event_loop.call_soon_threadsafe(harness.event_loop.stop)


_lock = threading.Lock()
