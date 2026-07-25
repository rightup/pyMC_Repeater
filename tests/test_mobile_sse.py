"""Tests for the Mobile Companion API SSE stream (phase 2).

Covers GET /api/v1/companions/{name}/events: replay-equivalence with sync,
live tail delivery, the registration-before-drain overlap dedupe, the
prune-floor -> snapshot_required control event, keepalive emission, and
listener cleanup on stream close. See docs/architecture/mobile-companion-api.md
§8 (SSE stream).

Handlers are invoked directly through ``__wrapped__`` (require_auth uses
functools.wraps), same pattern as tests/test_mobile_endpoints.py. The
``events`` is not itself a generator function -- it returns a close-aware
iterator backed by a generator -- so calling
``events.__wrapped__(self, **kwargs)`` runs synchronously up to that
``return`` (including the first durable page validation) and hands back a
stream the test then pulls frames from with ``next()``. The listener
is registered on the first pull, with one immediate refresh closing that lazy
registration gap.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import (
    CompanionStorageError,
    SQLiteHandler,
)
from repeater.web.auth import lease as auth_lease
from repeater.web.auth.api_tokens import APITokenManager
from repeater.web.mobile_endpoints import CompanionsV1

_HASH_BYTE = 0x01
_HASH = "0x01"
_NAME = "comp-test"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    return SQLiteHandler(tmp_path)


@pytest.fixture
def journal(handler):
    return CompanionEventJournal(handler, _HASH)


class _FakeIdentity:
    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31


class _FakeBridge:
    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31


def _daemon(handler, journal):
    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: [(_NAME, _FakeIdentity(), {})] if t == "companion" else []
    )
    frame_server = SimpleNamespace(companion_hash=_HASH, journal=journal)
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={_HASH_BYTE: _FakeBridge()},
        companion_frame_servers=[frame_server],
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )


@pytest.fixture
def endpoints(handler, journal):
    return CompanionsV1(daemon_instance=_daemon(handler, journal), config={})


@pytest.fixture(autouse=True)
def request_context():
    """Minimal CherryPy request/response state for direct handler calls.

    ``request.user`` defaults to an admin-scope caller: _resolve (called
    from events()) now runs a scope check (design doc §11.1) that would
    otherwise 403 every stream here, none of which are about scope
    enforcement itself (see tests/test_mobile_pairing.py for that).
    """
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.request.user = {"username": "test", "auth_type": "jwt", "scope": "admin"}
    cherrypy.serving.request._openhop_jwt_expires_at = time.time() + 3600
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    try:
        del cherrypy.serving.request._openhop_jwt_expires_at
    except AttributeError:
        pass
    cherrypy.serving.response.status = None


def _open_stream(endpoints, **kwargs):
    """Call events() directly; returns the generator.

    Unlike the JSON handlers, events() has no @require_auth decorator —
    auth is the tool-level require_auth covering the /api tree (see the
    handler docstring) — so there is no __wrapped__ to unwrap here.
    """
    return endpoints.events(companion_name=_NAME, **kwargs)


def _mobile_message(
    index: int = 0,
    *,
    text: str | None = None,
    packet_hash: str | None = None,
) -> dict:
    return {
        "id": index + 1,
        "companion_hash": _HASH,
        "sender_key": "",
        "recipient_key": "",
        "sender_prefix": "",
        "txt_type": 0,
        "timestamp": index,
        "text": f"m{index}" if text is None else text,
        "is_channel": False,
        "channel_idx": 0,
        "path_len": 0,
        "snr": 0.0,
        "rssi": 0,
        "channel_data_type": 0,
        "channel_data_payload": "",
        "packet_hash": f"{index:016X}" if packet_hash is None else packet_hash,
        "created_at": float(index + 1),
        "observation_count": 1,
        "unique_path_count": 1,
        "direction": "in",
        "state": "received",
        "expected_ack": None,
        "source": "radio",
    }


def _seed(handler, count, start_text="m"):
    seqs = []
    for i in range(count):
        seq = handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(i, text=f"{start_text}{i}"),
            packet_hash=f"{i:016x}",
        )
        seqs.append(seq)
    return seqs


def _cursor(handler, seq):
    return f"{handler.companion_journal_epoch()}:{seq}"


def _parse_frame(frame: str) -> dict:
    """Split an ``id:/event:/data:`` SSE frame into its three fields."""
    lines = frame.strip("\n").split("\n")
    parsed = {}
    for line in lines:
        key, _, value = line.partition(": ")
        parsed[key] = value
    parsed["data"] = json.loads(parsed["data"])
    return parsed


# --- Replay equivalence -------------------------------------------------


class TestReplayEquivalence:
    def test_malformed_last_event_id_is_400_before_streaming(
        self,
        endpoints,
        handler,
    ):
        cherrypy.serving.request.headers["Last-Event-ID"] = "é:0"

        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor=_cursor(handler, 0))

        assert exc.value.status == 400
        assert endpoints._sse_total == 0

    def test_replay_matches_sync_for_same_cursor(self, endpoints, handler, journal):
        seqs = _seed(handler, 5)
        sync_data = endpoints.sync.__wrapped__(
            endpoints, companion_name=_NAME, cursor=_cursor(handler, seqs[1])
        )["data"]

        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[1]))
        assert cherrypy.serving.response.headers["Cache-Control"] == (
            "no-store, no-cache, no-transform"
        )
        frames = [_parse_frame(next(gen)) for _ in range(len(seqs) - 2)]

        assert [f["data"]["seq"] for f in frames] == [e["seq"] for e in sync_data["events"]]
        assert [f["data"]["data"] for f in frames] == [e["data"] for e in sync_data["events"]]
        assert [f["event"] for f in frames] == [e["type"] for e in sync_data["events"]]
        assert [f["data"]["packet_hash"] for f in frames] == [
            e["packet_hash"] for e in sync_data["events"]
        ]

    def test_corrupt_first_page_is_503_before_streaming(
        self,
        endpoints,
        handler,
    ):
        with handler._connect() as conn:
            conn.execute(
                """
                INSERT INTO companion_events
                    (companion_hash, event_type, created_at, payload)
                VALUES (?, 'message', 1, '{broken')
                """,
                (_HASH,),
            )
            conn.commit()

        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor=_cursor(handler, 0))

        assert exc.value.status == 503
        assert endpoints._sse_total == 0

    def test_malformed_packet_hash_is_503_before_streaming(
        self,
        endpoints,
        handler,
    ):
        handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(0, text="must not stream"),
            packet_hash="not-a-packet-hash",
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor=_cursor(handler, 0))

        assert exc.value.status == 503
        assert endpoints._sse_total == 0

    def test_malformed_channel_event_is_503_before_streaming(
        self,
        endpoints,
        handler,
    ):
        handler.companion_append_event(
            _HASH,
            "channel",
            {"index": 1, "secret": "must-not-stream"},
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor=_cursor(handler, 0))

        assert exc.value.status == 503
        assert endpoints._sse_total == 0

    def test_malformed_contact_wire_fields_are_503_before_streaming(
        self,
        endpoints,
        handler,
    ):
        handler.companion_append_event(
            _HASH,
            "contact",
            {"public_key": "aa" * 32, "flags": "not-an-integer"},
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor=_cursor(handler, 0))

        assert exc.value.status == 503
        assert endpoints._sse_total == 0

    def test_no_cursor_defaults_to_head_no_replay(self, endpoints, handler, journal, monkeypatch):
        _seed(handler, 3)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))

        gen = _open_stream(endpoints)  # no cursor, no Last-Event-ID
        # Nothing to replay -> straight to live tail -> keepalive.
        frame = next(gen)
        assert frame == ": ka\n\n"


# --- Live tail ------------------------------------------------------------


class TestLiveTail:
    def test_append_after_connect_appears_on_stream(self, endpoints, handler, journal):
        seqs = _seed(handler, 2)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))

        new_seq = journal.record_message(_mobile_message(10, text="live"))
        parsed = _parse_frame(next(gen))
        assert parsed["id"] == _cursor(handler, new_seq)
        assert parsed["data"]["data"]["text"] == "live"

    def test_contact_event_uses_public_contact_shape(
        self,
        endpoints,
        handler,
        journal,
    ):
        seqs = _seed(handler, 1)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))

        contact_seq = journal.record_contact(
            {
                "pubkey": b"\xab" * 32,
                "name": "Bob",
                "adv_type": 1,
                "flags": 1,
                "out_path_len": 1,
                "out_path": b"\x01",
                "last_advert_timestamp": 1,
                "last_advert_packet": b"\x02",
                "lastmod": 2,
                "gps_lat": 1.0,
                "gps_lon": 2.0,
            },
            change="new",
        )

        parsed = _parse_frame(next(gen))
        assert parsed["id"] == _cursor(handler, contact_seq)
        assert parsed["data"]["data"] == {
            "public_key": "ab" * 32,
            "name": "Bob",
            "adv_type": 1,
            "flags": 1,
            "favorite": True,
            "out_path_len": 1,
            "last_advert_timestamp": 1,
            "lastmod": 2,
            "gps_lat": 1.0,
            "gps_lon": 2.0,
            "change": "new",
        }


# --- Overlap dedupe ---------------------------------------------------------


class TestOverlapDedupe:
    def test_event_appended_between_register_and_drain_is_not_duplicated(
        self, endpoints, handler, journal, monkeypatch
    ):
        seqs = _seed(handler, 3)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))

        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[0]))
        # This lands in the journal AND in the listener queue before the
        # generator's backlog scan (which hasn't run yet -- lazy) executes.
        race_seq = journal.record_message(_mobile_message(10, text="race"))

        frames = [_parse_frame(next(gen)) for _ in range(3)]  # seqs[1], seqs[2], race_seq
        got_seqs = [f["data"]["seq"] for f in frames]
        assert got_seqs == [seqs[1], seqs[2], race_seq]

        # Next pull must not re-deliver race_seq via the live queue; it was
        # already sent by the backlog drain, so it's skipped and we fall
        # through to a keepalive instead.
        frame = next(gen)
        assert frame == ": ka\n\n"


# --- snapshot_required --------------------------------------------------


class TestSnapshotRequired:
    def test_cursor_below_prune_floor_emits_control_event_and_closes(
        self, endpoints, handler, journal
    ):
        seqs = _seed(handler, 3)
        with handler._connect() as conn:
            conn.execute(
                """
                INSERT INTO companion_journal_floors
                    (companion_hash, prune_floor)
                VALUES (?, ?)
                ON CONFLICT(companion_hash) DO UPDATE SET
                    prune_floor = excluded.prune_floor
                """,
                (_HASH, seqs[-1]),
            )
            conn.commit()

        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[0]))
        frame = next(gen)
        assert "event: snapshot_required" in frame
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["snapshot_required"] is True
        assert payload["journal_epoch"] == handler.companion_journal_epoch()

        with pytest.raises(StopIteration):
            next(gen)

    def test_no_journal_returns_503(self, handler):
        # journal=None on the frame server simulates storage-disabled.
        daemon = _daemon(handler, journal=None)
        endpoints = CompanionsV1(daemon_instance=daemon, config={})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _open_stream(endpoints, cursor="0")
        assert exc.value.status == 503


# --- rf_reception opt-in filtering (design doc §9) --------------------------


class TestRfReceptionFiltering:
    """rf_reception events are omitted from both the backlog replay and the
    live tail unless ``?include=rf_receptions`` is given (same rule as
    sync)."""

    def test_excluded_from_replay_by_default(self, endpoints, handler, journal):
        _seed(handler, 1)
        handler.companion_append_event(
            _HASH,
            "rf_reception",
            {
                "packet_hash": "1111111111111111",
                "rssi": -80,
                "snr": 2.0,
                "path": ["01"],
                "observed_at": 1.0,
            },
            packet_hash="1111111111111111",
        )
        handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(
                1,
                text="after",
                packet_hash="2222222222222222",
            ),
            packet_hash="2222222222222222",
        )

        gen = _open_stream(endpoints, cursor=_cursor(handler, 0))
        # First seeded event, then straight to the second "message" (the
        # rf_reception in between is skipped, not framed).
        frame1 = _parse_frame(next(gen))
        frame2 = _parse_frame(next(gen))
        assert frame1["event"] == "message"
        assert frame2["event"] == "message"
        assert frame2["data"]["data"] == _mobile_message(
            1,
            text="after",
            packet_hash="2222222222222222",
        )

    def test_included_from_replay_with_include_param(self, endpoints, handler, journal):
        _seed(handler, 1)
        rf_seq = handler.companion_append_event(
            _HASH,
            "rf_reception",
            {
                "packet_hash": "1111111111111111",
                "rssi": -80,
                "snr": 2.0,
                "path": ["01"],
                "observed_at": 1.0,
            },
            packet_hash="1111111111111111",
        )
        handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(
                1,
                text="after",
                packet_hash="2222222222222222",
            ),
            packet_hash="2222222222222222",
        )

        gen = _open_stream(
            endpoints,
            cursor=_cursor(handler, 0),
            include="rf_receptions",
        )
        frame1 = _parse_frame(next(gen))
        frame2 = _parse_frame(next(gen))
        frame3 = _parse_frame(next(gen))
        assert [frame1["event"], frame2["event"], frame3["event"]] == [
            "message",
            "rf_reception",
            "message",
        ]
        assert frame2["id"] == _cursor(handler, rf_seq)

    def test_excluded_from_live_tail_by_default(self, endpoints, handler, journal):
        seqs = _seed(handler, 1)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))

        journal.record_rf_reception(
            {
                "packet_hash": "AAAA000000000000",
                "rssi": -70,
                "snr": 2.0,
                "original_path": [],
                "timestamp": 1.0,
            }
        )
        live_seq = journal.record_message(_mobile_message(10, text="live-after-rf"))

        parsed = _parse_frame(next(gen))
        # The rf_reception is skipped on the live queue; the next frame
        # delivered is the following message event.
        assert parsed["event"] == "message"
        assert parsed["id"] == _cursor(handler, live_seq)

    def test_included_from_live_tail_with_include_param(self, endpoints, handler, journal):
        seqs = _seed(handler, 1)
        gen = _open_stream(
            endpoints,
            cursor=_cursor(handler, seqs[-1]),
            include="rf_receptions",
        )

        rf_seq = journal.record_rf_reception(
            {
                "packet_hash": "BBBB000000000000",
                "rssi": -65,
                "snr": 3.0,
                "original_path": ["11"],
                "timestamp": 2.0,
            }
        )

        parsed = _parse_frame(next(gen))
        assert parsed["event"] == "rf_reception"
        assert parsed["id"] == _cursor(handler, rf_seq)


# --- Keepalive ------------------------------------------------------------


class TestKeepalive:
    def test_keepalive_emitted_on_idle_queue(self, endpoints, handler, journal, monkeypatch):
        seqs = _seed(handler, 1)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))

        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        frame = next(gen)
        assert frame == ": ka\n\n"

    def test_idle_stream_detects_epoch_rotation_before_keepalive(
        self,
        endpoints,
        handler,
        journal,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        old_epoch = handler.companion_journal_epoch()
        original_status = handler.companion_cursor_status
        status_calls = 0

        def rotate_before_idle_status(companion_hash, epoch, seq):
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                handler.companion_journal_rotate_epoch()
            return original_status(companion_hash, epoch, seq)

        monkeypatch.setattr(
            handler,
            "companion_cursor_status",
            rotate_before_idle_status,
        )
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.01))

        gen = _open_stream(endpoints, cursor=f"{old_epoch}:{seqs[-1]}")
        frame = next(gen)
        assert frame.startswith("event: snapshot_required\n")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["snapshot_required"] is True
        assert payload["reset_reason"] == "epoch_mismatch"
        assert payload["journal_epoch"] != old_epoch

        with pytest.raises(StopIteration):
            next(gen)


# --- Listener cleanup -------------------------------------------------------


class TestListenerCleanup:
    def test_hot_removed_companion_closes_idle_stream_and_releases_slot(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.01))
        stream = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        assert next(stream) == ": ka\n\n"
        assert endpoints._sse_total == 1

        endpoints.daemon_instance.companion_bridges.pop(_HASH_BYTE)

        with pytest.raises(StopIteration):
            next(stream)
        assert endpoints._sse_total == 0

    def test_close_before_first_pull_releases_stream_slot(
        self,
        endpoints,
        handler,
    ):
        seqs = _seed(handler, 1)
        stream = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        assert endpoints._sse_total == 1

        stream.close()

        assert endpoints._sse_total == 0

    def test_listener_registration_failure_releases_stream_slot(
        self,
        endpoints,
        handler,
        journal,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)

        def fail_registration(_callback):
            raise RuntimeError("register failed")

        monkeypatch.setattr(journal, "register_listener", fail_registration)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        assert endpoints._sse_total == 1

        with pytest.raises(StopIteration):
            next(gen)

        assert endpoints._sse_total == 0

    def test_listener_cleanup_failure_still_releases_stream_slot(
        self,
        endpoints,
        handler,
        journal,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.01))

        def fail_cleanup(_callback):
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(journal, "unregister_listener", fail_cleanup)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        assert next(gen) == ": ka\n\n"
        assert endpoints._sse_total == 1

        with pytest.raises(RuntimeError, match="cleanup failed"):
            gen.close()

        assert endpoints._sse_total == 0

    def test_listener_unregistered_after_generator_close(
        self, endpoints, handler, journal, monkeypatch
    ):
        # close() only runs the generator's `finally` if the generator frame
        # has actually started (Python no-ops close() on a never-iterated
        # generator), so pull one frame first -- this mirrors CherryPy,
        # which has already begun streaming by the time a client disconnect
        # triggers close().
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))
        seqs = _seed(handler, 1)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[-1]))
        next(gen)  # nothing to replay -> one keepalive frame
        assert len(journal._listeners) == 1

        gen.close()
        assert len(journal._listeners) == 0

    def test_listener_unregistered_after_replay_and_close(
        self, endpoints, handler, journal, monkeypatch
    ):
        """Same cleanup guarantee when the client disconnects mid-replay,
        before the stream ever reaches the live tail / keepalive loop."""
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))
        seqs = _seed(handler, 3)
        gen = _open_stream(endpoints, cursor=_cursor(handler, seqs[0]))
        next(gen)  # first backlog frame
        assert len(journal._listeners) == 1

        gen.close()
        assert len(journal._listeners) == 0


class TestAuthorizationLifetime:
    def test_jwt_expiration_closes_an_already_open_stream_before_replay(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        _seed(handler, 1)
        clock = {"wall": 1000.0}
        monkeypatch.setattr(auth_lease.time, "time", lambda: clock["wall"])
        cherrypy.request._openhop_jwt_expires_at = 1001.0

        stream = _open_stream(endpoints, cursor=_cursor(handler, 0))
        assert endpoints._sse_total == 1

        clock["wall"] = 1002.0
        with pytest.raises(StopIteration):
            next(stream)
        assert endpoints._sse_total == 0

    def test_revoked_api_token_closes_an_idle_stream_at_next_auth_check(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        manager = APITokenManager(handler, "test-lease-secret")
        token_id, _token = manager.create_token("operator", scope="admin")
        cherrypy.request.user = {
            "username": "api_token",
            "auth_type": "api_token",
            "token_id": token_id,
            "scope": "admin",
        }
        del cherrypy.request._openhop_jwt_expires_at
        monkeypatch.setitem(cherrypy.config, "token_manager", manager)
        clock = {"monotonic": 500.0}
        monkeypatch.setattr(
            auth_lease.time,
            "monotonic",
            lambda: clock["monotonic"],
        )

        stream = _open_stream(
            endpoints,
            cursor=_cursor(handler, seqs[-1]),
        )
        assert endpoints._sse_total == 1
        assert manager.revoke_token(token_id) is True
        clock["monotonic"] += 16.0

        with pytest.raises(StopIteration):
            next(stream)
        assert endpoints._sse_total == 0

    def test_auth_storage_outage_closes_an_open_api_token_stream(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        manager = APITokenManager(handler, "test-lease-secret")
        token_id, _token = manager.create_token("operator", scope="admin")
        cherrypy.request.user = {
            "username": "api_token",
            "auth_type": "api_token",
            "token_id": token_id,
            "scope": "admin",
        }
        del cherrypy.request._openhop_jwt_expires_at
        monkeypatch.setitem(cherrypy.config, "token_manager", manager)
        clock = {"monotonic": 700.0}
        monkeypatch.setattr(
            auth_lease.time,
            "monotonic",
            lambda: clock["monotonic"],
        )

        stream = _open_stream(
            endpoints,
            cursor=_cursor(handler, seqs[-1]),
        )
        monkeypatch.setattr(
            manager,
            "get_token",
            lambda _token_id: (_ for _ in ()).throw(
                CompanionStorageError("private database detail")
            ),
        )
        clock["monotonic"] += 16.0

        with pytest.raises(StopIteration):
            next(stream)
        assert endpoints._sse_total == 0

    def test_renamed_exact_scope_rechecks_full_device_identity(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        seqs = _seed(handler, 1)
        manager = APITokenManager(handler, "test-lease-secret")
        plaintext = manager.generate_api_token()
        paired = handler.companion_pair_device(
            _HASH,
            _FakeBridge().get_public_key().hex(),
            "phone-1",
            "Phone",
            "phone-1",
            manager.hash_token(plaintext),
            "companion:old-name",
        )
        token_id = paired["token_id"]
        cherrypy.request.user = {
            "username": "api_token",
            "auth_type": "api_token",
            "token_id": token_id,
            "scope": "companion:old-name",
        }
        del cherrypy.request._openhop_jwt_expires_at
        monkeypatch.setitem(cherrypy.config, "token_manager", manager)
        clock = {"monotonic": 900.0}
        monkeypatch.setattr(
            auth_lease.time,
            "monotonic",
            lambda: clock["monotonic"],
        )

        stream = _open_stream(
            endpoints,
            cursor=_cursor(handler, seqs[-1]),
        )
        monkeypatch.setattr(
            handler,
            "companion_device_get_by_token_strict",
            lambda _token_id: None,
        )
        clock["monotonic"] += 16.0

        with pytest.raises(StopIteration):
            next(stream)
        assert endpoints._sse_total == 0
