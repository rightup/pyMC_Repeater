"""Tests for the Mobile Companion API SSE stream (phase 2).

Covers GET /api/v1/companions/{name}/events: replay-equivalence with sync,
live tail delivery, the registration-before-drain overlap dedupe, the
prune-floor -> snapshot_required control event, keepalive emission, and
listener cleanup on stream close. See docs/architecture/mobile-companion-api.md
§8 (SSE stream).

Handlers are invoked directly through ``__wrapped__`` (require_auth uses
functools.wraps), same pattern as tests/test_mobile_endpoints.py. The
``events`` handler is not itself a generator function -- it returns a
generator (``generate()`` or the snapshot_required stream) -- so calling
``events.__wrapped__(self, **kwargs)`` runs synchronously up to that
``return`` (registering the journal listener eagerly) and hands back a
generator object the test then pulls frames from with ``next()``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
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
        get_identities_by_type=lambda t: (
            [(_NAME, _FakeIdentity(), {})] if t == "companion" else []
        )
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
    """Minimal CherryPy request/response state for direct handler calls."""
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    cherrypy.serving.response.status = None


def _open_stream(endpoints, **kwargs):
    """Call events() directly; returns the generator.

    Unlike the JSON handlers, events() has no @require_auth decorator —
    auth is the tool-level require_auth covering the /api tree (see the
    handler docstring) — so there is no __wrapped__ to unwrap here.
    """
    return endpoints.events(companion_name=_NAME, **kwargs)


def _seed(handler, count, start_text="m"):
    seqs = []
    for i in range(count):
        seq = handler.companion_append_event(
            _HASH, "message", {"text": f"{start_text}{i}"}, packet_hash=f"ph-{i}"
        )
        seqs.append(seq)
    return seqs


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
    def test_replay_matches_sync_for_same_cursor(self, endpoints, handler, journal):
        seqs = _seed(handler, 5)
        sync_data = endpoints.sync.__wrapped__(
            endpoints, companion_name=_NAME, cursor=str(seqs[1])
        )["data"]

        gen = _open_stream(endpoints, cursor=str(seqs[1]))
        frames = [_parse_frame(next(gen)) for _ in range(len(seqs) - 2)]

        assert [f["data"]["seq"] for f in frames] == [e["seq"] for e in sync_data["events"]]
        assert [f["data"]["data"] for f in frames] == [e["data"] for e in sync_data["events"]]
        assert [f["event"] for f in frames] == [e["type"] for e in sync_data["events"]]
        assert [f["data"]["packet_hash"] for f in frames] == [
            e["packet_hash"] for e in sync_data["events"]
        ]

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
        gen = _open_stream(endpoints, cursor=str(seqs[-1]))

        new_seq = journal.record_message({"text": "live"})
        parsed = _parse_frame(next(gen))
        assert parsed["id"] == str(new_seq)
        assert parsed["data"]["data"]["text"] == "live"


# --- Overlap dedupe ---------------------------------------------------------


class TestOverlapDedupe:
    def test_event_appended_between_register_and_drain_is_not_duplicated(
        self, endpoints, handler, journal, monkeypatch
    ):
        seqs = _seed(handler, 3)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))

        gen = _open_stream(endpoints, cursor=str(seqs[0]))  # listener registered now
        # This lands in the journal AND in the listener queue before the
        # generator's backlog scan (which hasn't run yet -- lazy) executes.
        race_seq = journal.record_message({"text": "race"})

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
        handler.companion_journal_meta_set("prune_floor", str(seqs[-1]))

        gen = _open_stream(endpoints, cursor=str(seqs[0]))
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


# --- Keepalive ------------------------------------------------------------


class TestKeepalive:
    def test_keepalive_emitted_on_idle_queue(self, endpoints, handler, journal, monkeypatch):
        seqs = _seed(handler, 1)
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))

        gen = _open_stream(endpoints, cursor=str(seqs[-1]))  # nothing to replay
        frame = next(gen)
        assert frame == ": ka\n\n"


# --- Listener cleanup -------------------------------------------------------


class TestListenerCleanup:
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
        gen = _open_stream(endpoints, cursor=str(seqs[-1]))
        assert len(journal._listeners) == 1
        next(gen)  # nothing to replay -> one keepalive frame

        gen.close()
        assert len(journal._listeners) == 0

    def test_listener_unregistered_after_replay_and_close(
        self, endpoints, handler, journal, monkeypatch
    ):
        """Same cleanup guarantee when the client disconnects mid-replay,
        before the stream ever reaches the live tail / keepalive loop."""
        monkeypatch.setattr(endpoints, "_sse_settings", lambda: (64, 0.05))
        seqs = _seed(handler, 3)
        gen = _open_stream(endpoints, cursor=str(seqs[0]))
        assert len(journal._listeners) == 1
        next(gen)  # first backlog frame

        gen.close()
        assert len(journal._listeners) == 0
