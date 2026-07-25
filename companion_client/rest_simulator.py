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
from openhop_core.companion.models import SentResult

from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.http_server import _json_error_page_v1
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


class _FakeContactStore:
    """In-memory contact store, keyed by public key.

    ``max_contacts`` is an instance attribute so a test can shrink it to
    exercise the full-store path without every other test tripping over it.
    """

    def __init__(self, contacts: list, max_contacts: int = 64) -> None:
        self._by_key = {bytes(c.public_key): c for c in contacts}
        self.max_contacts = max_contacts

    def get_by_key(self, pub_key):
        return self._by_key.get(bytes(pub_key))

    def add(self, contact) -> bool:
        key = bytes(contact.public_key)
        if key not in self._by_key and len(self._by_key) >= self.max_contacts:
            return False  # store full -> 507
        self._by_key[key] = contact
        return True

    def remove(self, pub_key) -> bool:
        return self._by_key.pop(bytes(pub_key), None) is not None

    def values(self):
        return list(self._by_key.values())


class RestFakeBridge:
    def __init__(self, hash_byte: int = DEFAULT_HASH_BYTE) -> None:
        self._pubkey = bytes([hash_byte]) + b"\x22" * 31
        self.prefs = SimpleNamespace(
            node_name="TestNode",
            adv_type=1,
            latitude=47.6,
            longitude=-122.3,
            autoadd_config=0,
            autoadd_max_hops=0,
            path_hash_mode=0,
            rx_delay_base=0.0,
            airtime_factor=1.0,
            client_repeat=0,
            manual_add_contacts=0,
            telemetry_mode_base=0,
            telemetry_mode_location=0,
            telemetry_mode_environment=0,
            advert_loc_policy=0,
            multi_acks=0,
            default_scope_name="",
        )
        self._channel_map = {
            0: SimpleNamespace(name="Public", secret=b"\x00" * 16),
            1: SimpleNamespace(name="#howltest", secret=b"\x11" * 16),
        }
        self.channels = _FakeChannels(self._channel_map)
        self.sent: list = []
        self._login_connections: set[bytes] = set()
        self.contacts = _FakeContactStore(
            [
                SimpleNamespace(
                    public_key=b"\xaa" * 32,
                    name="Alice",
                    adv_type=1,
                    flags=0,
                    out_path_len=-1,
                    out_path=b"",
                    last_advert_timestamp=123,
                    lastmod=124,
                    gps_lat=0.0,
                    gps_lon=0.0,
                )
            ]
        )

    def get_public_key(self) -> bytes:
        return self._pubkey

    def get_self_info(self):
        return self.prefs

    def get_contacts(self):
        return self.contacts.values()

    def add_update_contact(self, contact) -> bool:
        return self.contacts.add(contact)

    def remove_contact(self, pub_key) -> bool:
        return self.contacts.remove(pub_key)

    def get_channel(self, idx: int):
        return self._channel_map.get(idx)

    def reset_path(self, pub_key) -> bool:
        """Clear a contact's learned outbound path."""
        contact = self.contacts.get_by_key(pub_key)
        if contact is None:
            return False
        contact.out_path_len = -1
        contact.out_path = b""
        return True

    def set_channel(self, idx: int, name: str, secret: bytes) -> bool:
        """Mirrors the real bridge: an empty name creates an EMPTY-NAMED
        channel, it does not remove the slot. Removing is remove_channel().

        This faithfulness matters -- an earlier version of this double treated
        an empty name as a delete, which masked a real bug where the clear
        endpoint left an empty channel behind on a live repeater.
        """
        if idx >= _FakeChannels.max_channels:
            return False
        self._channel_map[idx] = SimpleNamespace(name=name[:32], secret=secret)
        return True

    def remove_channel(self, idx: int) -> bool:
        return self._channel_map.pop(idx, None) is not None

    async def send_channel_message(self, channel_idx: int, text: str, **kwargs):
        """Record an outbound channel send. No radio, so nothing transmits.

        Async because the endpoint awaits the bridge call on the daemon's
        event loop (``_send_and_capture``).
        """
        self.sent.append({"channel_idx": channel_idx, "text": text})
        return True

    async def send_text_message(self, pub_key, text: str, **kwargs):
        self.sent.append({"to": bytes(pub_key).hex(), "text": text})
        return SentResult(success=True, is_flood=True)

    async def send_text(self, pub_key, text: str, **kwargs):
        """Compatibility alias for older client-harness callers."""

        return await self.send_text_message(pub_key, text, **kwargs)

    async def send_login(self, pub_key, password: str):
        key = bytes(pub_key)
        self._login_connections.add(key)
        self.sent.append({"login": key.hex(), "password": password})
        return {"logged_in": True}

    def has_login_connection(self, pub_key) -> bool:
        return bytes(pub_key) in self._login_connections

    async def send_logout(self, pub_key):
        key = bytes(pub_key)
        self._login_connections.discard(key)
        self.sent.append({"logout": key.hex()})
        return True

    async def send_status_request(self, pub_key, *, timeout: float):
        key = bytes(pub_key)
        self.sent.append({"status_request": key.hex(), "timeout": timeout})
        return {"status": "ok"}

    async def send_telemetry_request(
        self,
        pub_key,
        *,
        want_base: bool,
        want_location: bool,
        want_environment: bool,
        timeout: float,
    ):
        key = bytes(pub_key)
        self.sent.append(
            {
                "telemetry_request": key.hex(),
                "want_base": want_base,
                "want_location": want_location,
                "want_environment": want_environment,
                "timeout": timeout,
            }
        )
        return {
            "base": {"battery_mv": 4200} if want_base else None,
            "location": {"latitude": 47.6, "longitude": -122.3} if want_location else None,
            "environment": {"temperature_c": 20.0} if want_environment else None,
        }

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
    event_loop_thread: Optional[threading.Thread] = None

    def admin_token(self, name: str = "admin-token") -> str:
        """Mint an admin-scope API token, standing in for an operator's JWT.

        Pairing has to be started by an authenticated operator; a device token
        cannot bootstrap itself.
        """
        _token_id, plaintext = self.token_manager.create_token(name=name, scope="admin")
        return plaintext


def _start_rest_harness(
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
    journal = CompanionEventJournal(handler, companion_hash)

    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: (
            [(companion_name, _FakeIdentity(hash_byte), {})] if t == "companion" else []
        )
    )
    daemon = SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={hash_byte: bridge},
        companion_journals={companion_hash: journal},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )

    # Fixed secrets are intentional because this server is a local simulator.
    token_manager = APITokenManager(
        handler,
        secret_key="test-secret-not-for-production",  # nosec B106
    )
    jwt_handler = JWTHandler(
        secret="test-secret-not-for-production-only",  # nosec B106
    )

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

    try:
        root = SimpleNamespace()
        root.api = SimpleNamespace()
        root.api.v1 = MobileAPIEndpoints(
            daemon_instance=daemon,
            config={
                "mobile_api": {
                    "rf_burst": 1_000,
                    "rf_per_minute": 1_000,
                    "rf_global_burst": 1_000,
                    "rf_global_per_minute": 1_000,
                }
            },
            event_loop=loop,
        )
        cherrypy.tree.mount(
            root,
            "/",
            {
                "/": {"request.dispatch": cherrypy.dispatch.Dispatcher()},
                "/api/v1": {"error_page.default": _json_error_page_v1},
            },
        )
        cherrypy.engine.start()
    except Exception:
        cherrypy.engine.exit()
        cherrypy.tree.apps.clear()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        if not thread.is_alive():
            loop.close()
        raise

    return RestHarness(
        base_url=f"http://127.0.0.1:{port}",
        handler=handler,
        bridge=bridge,
        companion_name=companion_name,
        companion_hash=companion_hash,
        token_manager=token_manager,
        jwt_handler=jwt_handler,
        event_loop=loop,
        event_loop_thread=thread,
    )


_HARNESS_STARTING = object()
_harness_lock = threading.Lock()
_active_harness: object = None


def start_rest_harness(
    tmp_path,
    *,
    companion_name: str = DEFAULT_COMPANION_NAME,
    hash_byte: int = DEFAULT_HASH_BYTE,
) -> RestHarness:
    """Start the one process-global CherryPy harness allowed at a time."""

    global _active_harness
    with _harness_lock:
        if _active_harness is not None:
            raise RuntimeError("a REST simulator harness is already active")
        _active_harness = _HARNESS_STARTING
    try:
        harness = _start_rest_harness(
            tmp_path,
            companion_name=companion_name,
            hash_byte=hash_byte,
        )
    except Exception:
        with _harness_lock:
            _active_harness = None
        raise
    with _harness_lock:
        _active_harness = harness
    return harness


def stop_rest_harness(harness: Optional[RestHarness] = None) -> None:
    """Stop CherryPy and fully join/close the harness event loop."""

    global _active_harness
    with _harness_lock:
        active = _active_harness
        if harness is not None:
            if active is None and (harness.event_loop is None or harness.event_loop.is_closed()):
                return
            if harness is not active:
                raise ValueError("the supplied REST simulator harness is not active")
            target = harness
        elif isinstance(active, RestHarness):
            target = active
        elif active is _HARNESS_STARTING:
            raise RuntimeError("the REST simulator harness is still starting")
        else:
            return

    cherrypy.engine.exit()
    cherrypy.tree.apps.clear()
    if target is not None and target.event_loop is not None:
        loop = target.event_loop
        thread = target.event_loop_thread
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("REST simulator event loop did not stop")
        if not loop.is_closed():
            loop.close()
    with _harness_lock:
        if target is _active_harness:
            _active_harness = None
