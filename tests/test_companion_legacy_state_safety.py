"""Concurrency and durability checks for the legacy companion REST surface."""

from __future__ import annotations

import asyncio
import copy
import queue
import threading
import time
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cherrypy
import pytest
from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Contact

from repeater.companion.bridge import (
    RepeaterCompanionBridge,
    outbound_message_source,
)
from repeater.web.auth import lease as auth_lease
from repeater.web.companion_endpoints import CompanionAPIEndpoints


class _Bridge:
    def __init__(self, contacts=(), *, max_contacts=10):
        self.state_mutation_lock = asyncio.Lock()
        self.contacts = ContactStore(max_contacts=max_contacts)
        self.contacts.load_from(copy.deepcopy(list(contacts)))
        self.persisted = []
        self.notifications = []
        self.persistence_error = None
        self.prefs_error = None
        self.prefs = SimpleNamespace(
            node_name="before",
            latitude=0.0,
            longitude=0.0,
        )

    _contact_storage_dict = staticmethod(RepeaterCompanionBridge._contact_storage_dict)
    _contact_changes = staticmethod(RepeaterCompanionBridge._contact_changes)

    async def _persist_contact_changes(self, changes):
        assert self.state_mutation_lock.locked()
        if self.persistence_error is not None:
            raise self.persistence_error
        self.persisted.append(copy.deepcopy(changes))

    async def _notify_observers(self, event_name, change, contact):
        assert not self.state_mutation_lock.locked()
        self.notifications.append((event_name, change, copy.deepcopy(contact)))

    def get_contacts(self, since=0):
        return self.contacts.get_all(since)

    def reset_path(self, public_key):
        contact = self.contacts.get_by_key(public_key)
        if contact is None:
            return False
        contact.out_path_len = -1
        contact.out_path = b""
        self.contacts.update(contact)
        return True

    def clear_prefs_save_error(self):
        self.prefs_error = None

    def consume_prefs_save_error(self):
        error = self.prefs_error
        self.prefs_error = None
        return error

    def set_advert_name(self, name):
        assert self.state_mutation_lock.locked()
        self.prefs.node_name = name

    def set_advert_latlon(self, latitude, longitude):
        assert self.state_mutation_lock.locked()
        if not -90 <= latitude <= 90:
            raise ValueError("Latitude out of range")
        if not -180 <= longitude <= 180:
            raise ValueError("Longitude out of range")
        self.prefs.latitude = latitude
        self.prefs.longitude = longitude


def _endpoint(bridge, neighbours=None):
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    endpoint._get_bridge = lambda **_params: bridge
    endpoint._get_sqlite_handler = lambda: SimpleNamespace(
        get_neighbors=lambda: dict(neighbours or {})
    )
    return endpoint


def test_legacy_direct_send_preserves_upstream_ack_wait():
    """The compatibility route keeps upstream's human-facing ACK contract."""
    bridge = _Bridge()
    seen_sources = []

    async def _send(*_args, **_kwargs):
        seen_sources.append(outbound_message_source.get())
        return SimpleNamespace(
            success=True,
            is_flood=False,
            expected_ack=123,
        )

    bridge.send_text_message = AsyncMock(side_effect=_send)
    endpoint = _endpoint(bridge)
    endpoint._require_post = lambda: None
    endpoint._get_json_body = lambda: {
        "pub_key": (b"\xaa" * 32).hex(),
        "text": "hello",
    }
    endpoint._run_async = lambda coro, timeout=30.0: asyncio.run(coro)

    response = CompanionAPIEndpoints.send_text.__wrapped__(endpoint)

    assert response["data"]["sent"] is True
    assert seen_sources == ["operator"]
    bridge.send_text_message.assert_awaited_once_with(
        b"\xaa" * 32,
        "hello",
        txt_type=0,
        wait_for_ack=True,
    )


def test_legacy_channel_send_is_attributed_to_operator():
    bridge = _Bridge()
    seen_sources = []

    async def _send(*_args, **_kwargs):
        seen_sources.append(outbound_message_source.get())
        return True

    bridge.send_channel_message = AsyncMock(side_effect=_send)
    endpoint = _endpoint(bridge)
    endpoint._require_post = lambda: None
    endpoint._get_json_body = lambda: {
        "channel_idx": 0,
        "text": "hello",
    }
    endpoint._run_async = lambda coro, timeout=30.0: asyncio.run(coro)

    response = CompanionAPIEndpoints.send_channel_message.__wrapped__(endpoint)

    assert response["data"]["sent"] is True
    assert seen_sources == ["operator"]


@pytest.mark.asyncio
async def test_state_read_waits_for_committed_state():
    bridge = _Bridge()
    endpoint = _endpoint(bridge)
    await bridge.state_mutation_lock.acquire()

    task = asyncio.create_task(
        endpoint._read_bridge_state({}, lambda resolved: resolved.prefs.node_name)
    )
    await asyncio.sleep(0)
    assert not task.done()

    bridge.state_mutation_lock.release()
    assert await task == "before"


@pytest.mark.asyncio
async def test_rf_operation_is_not_held_behind_state_lock():
    bridge = _Bridge()
    endpoint = _endpoint(bridge)
    await bridge.state_mutation_lock.acquire()

    async def _operation(resolved):
        assert resolved is bridge
        return "sent"

    try:
        assert await endpoint._call_bridge({}, _operation) == "sent"
    finally:
        bridge.state_mutation_lock.release()


@pytest.mark.asyncio
async def test_reset_path_commits_before_observer_notification():
    public_key = b"\x01" * 32
    bridge = _Bridge(
        [
            Contact(
                public_key=public_key,
                name="node",
                adv_type=1,
                out_path_len=2,
                out_path=b"\x10\x11",
            )
        ]
    )
    endpoint = _endpoint(bridge)

    assert await endpoint._reset_path_durable({}, public_key) is True
    contact = bridge.contacts.get_by_key(public_key)
    assert (contact.out_path_len, contact.out_path) == (-1, b"")
    assert bridge.persisted[0][0]["change"] == "path"
    assert bridge.notifications[0][0:2] == ("contact_committed", "path")


@pytest.mark.asyncio
async def test_reset_path_rolls_back_when_storage_rejects_change():
    public_key = b"\x02" * 32
    bridge = _Bridge(
        [
            Contact(
                public_key=public_key,
                name="node",
                adv_type=1,
                out_path_len=1,
                out_path=b"\x10",
            )
        ]
    )
    bridge.persistence_error = RuntimeError("disk failed")
    endpoint = _endpoint(bridge)

    with pytest.raises(RuntimeError, match="disk failed"):
        await endpoint._reset_path_durable({}, public_key)

    contact = bridge.contacts.get_by_key(public_key)
    assert (contact.out_path_len, contact.out_path) == (1, b"\x10")
    assert bridge.notifications == []


@pytest.mark.asyncio
async def test_import_is_bounded_atomic_and_favourite_aware():
    favourite_key = b"\x03" * 32
    newest_key = b"\x04" * 32
    older_key = b"\x05" * 32
    bridge = _Bridge(
        [
            Contact(
                public_key=favourite_key,
                name="favourite",
                adv_type=1,
                flags=1,
                lastmod=1,
            )
        ],
        max_contacts=2,
    )
    endpoint = _endpoint(
        bridge,
        {
            newest_key.hex(): {
                "node_name": "newest",
                "contact_type": "companion",
                "last_seen": 20,
            },
            older_key.hex(): {
                "node_name": "older",
                "contact_type": "repeater",
                "last_seen": 10,
            },
        },
    )

    result = await endpoint._import_repeater_contacts(
        {},
        contact_types=None,
        hours=None,
        limit=None,
    )

    assert result == {
        "imported": 2,
        "added": 1,
        "updated": 0,
        "retained": 0,
        "removed": 1,
    }
    assert {contact.public_key for contact in bridge.contacts.get_all()} == {
        favourite_key,
        newest_key,
    }
    assert len(bridge.persisted) == 1
    assert len(bridge.notifications) == len(bridge.persisted[0])


@pytest.mark.asyncio
async def test_repeat_import_preserves_chat_owned_contact_state():
    public_key = b"\x08" * 32
    bridge = _Bridge(
        [
            Contact(
                public_key=public_key,
                name="old advert",
                adv_type=1,
                flags=1,
                out_path_len=2,
                out_path=b"\xaa\xbb",
                sync_since=123,
                last_advert_packet=b"signed-advert",
                last_advert_timestamp=10,
                lastmod=10,
            )
        ],
        max_contacts=2,
    )
    endpoint = _endpoint(
        bridge,
        {
            public_key.hex(): {
                "node_name": "new advert",
                "contact_type": "repeater",
                "last_seen": 20,
                "latitude": 12.5,
                "longitude": -3.25,
            }
        },
    )

    result = await endpoint._import_repeater_contacts(
        {},
        contact_types=None,
        hours=None,
        limit=None,
    )

    contact = bridge.contacts.get_by_key(public_key)
    assert result == {
        "imported": 1,
        "added": 0,
        "updated": 1,
        "retained": 0,
        "removed": 0,
    }
    assert contact.name == "new advert"
    assert contact.adv_type == 2
    assert (contact.flags, contact.out_path_len, contact.out_path) == (
        1,
        2,
        b"\xaa\xbb",
    )
    assert contact.sync_since == 123
    assert contact.last_advert_packet == b"signed-advert"


@pytest.mark.asyncio
async def test_import_rolls_memory_back_when_atomic_commit_fails():
    original_key = b"\x06" * 32
    imported_key = b"\x07" * 32
    bridge = _Bridge(
        [Contact(public_key=original_key, name="original", adv_type=1)],
        max_contacts=2,
    )
    bridge.persistence_error = RuntimeError("disk failed")
    endpoint = _endpoint(
        bridge,
        {
            imported_key.hex(): {
                "node_name": "imported",
                "contact_type": "sensor",
                "last_seen": 20,
            }
        },
    )

    with pytest.raises(RuntimeError, match="disk failed"):
        await endpoint._import_repeater_contacts(
            {},
            contact_types=None,
            hours=None,
            limit=None,
        )

    assert [contact.public_key for contact in bridge.contacts.get_all()] == [original_key]
    assert bridge.notifications == []


@pytest.mark.asyncio
async def test_preference_mutations_use_the_shared_state_lock():
    bridge = _Bridge()
    endpoint = _endpoint(bridge)

    assert await endpoint._set_advert_name({}, "after") == "after"
    assert await endpoint._set_advert_location({}, 12.5, -45.25) == {
        "latitude": 12.5,
        "longitude": -45.25,
    }


@pytest.mark.asyncio
async def test_sse_events_are_scoped_to_the_selected_companion():
    class _EventBridge:
        def __init__(self, companion_hash):
            self._companion_hash = companion_hash
            self.callbacks = {}

        def __getattr__(self, name):
            if not name.startswith("on_"):
                raise AttributeError(name)

            def _register(callback):
                self.callbacks[name[3:]] = callback

            return _register

    first = _EventBridge("0x01")
    second = _EventBridge("0x02")
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    endpoint._callback_registrations = weakref.WeakKeyDictionary()
    endpoint._sse_clients = []
    endpoint._sse_lock = threading.Lock()
    endpoint._get_bridge = lambda name=None, **_params: {
        "first": first,
        "second": second,
    }[name]

    first_key = await endpoint._register_callbacks({"name": "first"})
    second_key = await endpoint._register_callbacks({"name": "second"})
    first_queue = queue.Queue()
    second_queue = queue.Queue()
    endpoint._sse_clients.extend([(first_key, first_queue), (second_key, second_queue)])

    first.callbacks["message_received"]("hello")

    assert first_queue.get_nowait()["companion_hash"] == "0x01"
    with pytest.raises(queue.Empty):
        second_queue.get_nowait()


def _legacy_stream_endpoint(monkeypatch, *, max_connections=2, queue_size=2):
    endpoint = CompanionAPIEndpoints(
        config={
            "http": {
                "sse_queue_maxsize": 32,
                "sse_keepalive_sec": 15,
            },
            "mobile_api": {"sse_max_connections": max_connections},
        }
    )
    endpoint._sse_queue_maxsize = queue_size
    bridge_key = object()
    monkeypatch.setattr(endpoint, "_ensure_callbacks", lambda _params: bridge_key)
    monkeypatch.setattr(cherrypy.request, "method", "GET", raising=False)
    monkeypatch.setattr(
        cherrypy.request,
        "user",
        {
            "auth_type": "jwt",
            "username": "operator",
            "client_id": "browser-one",
            "scope": "admin",
        },
        raising=False,
    )
    monkeypatch.setattr(
        cherrypy.request,
        "_openhop_jwt_expires_at",
        time.time() + 3600,
        raising=False,
    )
    return endpoint, bridge_key


def test_legacy_sse_close_before_first_pull_releases_capacity(monkeypatch):
    endpoint, _bridge_key = _legacy_stream_endpoint(monkeypatch)

    stream = endpoint.events()
    assert cherrypy.serving.response.headers["Cache-Control"] == (
        "no-store, no-cache, no-transform"
    )
    assert endpoint._sse_total == 1
    assert len(endpoint._sse_clients) == 1

    stream.close()

    assert endpoint._sse_total == 0
    assert endpoint._sse_clients == []


def test_legacy_sse_hot_remove_closes_exact_bridge_stream(monkeypatch):
    endpoint, bridge_key = _legacy_stream_endpoint(monkeypatch)
    bridge = SimpleNamespace(
        get_public_key=lambda: b"\x01" + b"\x22" * 31,
    )
    endpoint.daemon_instance = SimpleNamespace(companion_bridges={0x01: bridge})
    endpoint._callback_bridges = {bridge_key: bridge}

    stream = endpoint.events()
    assert next(stream).startswith('data: {"event": "connected"')
    assert endpoint._sse_total == 1

    endpoint.daemon_instance.companion_bridges.pop(0x01)

    with pytest.raises(StopIteration):
        next(stream)
    assert endpoint._sse_total == 0
    assert endpoint._sse_clients == []


def test_legacy_sse_closes_when_its_jwt_expires(monkeypatch):
    endpoint, _bridge_key = _legacy_stream_endpoint(monkeypatch)
    expiration = cherrypy.request._openhop_jwt_expires_at
    stream = endpoint.events()
    assert next(stream).startswith('data: {"event": "connected"')

    monkeypatch.setattr(auth_lease.time, "time", lambda: expiration + 1)

    with pytest.raises(StopIteration):
        next(stream)
    assert endpoint._sse_total == 0
    assert endpoint._sse_clients == []


def test_legacy_sse_allows_only_one_stream_per_principal_and_companion(
    monkeypatch,
):
    endpoint, _bridge_key = _legacy_stream_endpoint(monkeypatch)
    first = endpoint.events()
    cherrypy.response.headers = {}

    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint.events()

    assert exc.value.status == 429
    assert cherrypy.response.headers["Retry-After"] == "5"
    assert "Content-Type" not in cherrypy.response.headers
    first.close()
    assert endpoint._sse_total == 0


def test_legacy_sse_enforces_process_surface_capacity(monkeypatch):
    endpoint, _bridge_key = _legacy_stream_endpoint(
        monkeypatch,
        max_connections=1,
    )
    first = endpoint.events()
    cherrypy.request.user = {
        "auth_type": "jwt",
        "username": "other",
        "client_id": "browser-two",
        "scope": "admin",
    }

    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint.events()

    assert exc.value.status == 429
    first.close()


def test_legacy_sse_disconnects_a_slow_consumer_on_queue_overflow(
    monkeypatch,
):
    endpoint, bridge_key = _legacy_stream_endpoint(
        monkeypatch,
        queue_size=1,
    )
    stream = endpoint.events()
    assert next(stream).startswith('data: {"event": "connected"')

    endpoint._broadcast_sse(bridge_key, {"event": "first"})
    endpoint._broadcast_sse(bridge_key, {"event": "overflow"})

    with pytest.raises(StopIteration):
        next(stream)
    assert endpoint._sse_total == 0
    assert endpoint._sse_clients == []


def test_legacy_sse_rejects_non_get_requests(monkeypatch):
    endpoint, _bridge_key = _legacy_stream_endpoint(monkeypatch)
    cherrypy.request.method = "POST"

    with pytest.raises(cherrypy.HTTPError) as exc:
        endpoint.events()

    assert exc.value.status == 405
    assert cherrypy.response.headers["Allow"] == "GET"
    assert endpoint._sse_total == 0
