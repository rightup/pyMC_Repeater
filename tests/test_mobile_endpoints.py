"""Tests for the Mobile Companion API v1 endpoints (phase 1).

Covers /api/v1/companions list, snapshot (cursor + ETag/304), sync (delta
ordering, has_more, prune-floor -> snapshot_required, bad cursor), and
message history paging. Handlers are invoked directly through
``__wrapped__`` (require_auth uses functools.wraps), with a minimal
CherryPy request context set up per test; storage is a real SQLiteHandler
on tmp_path, matching the other companion storage tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.mobile_endpoints import CompanionsV1, MobileAPIEndpoints

_HASH_BYTE = 0x01
_HASH = "0x01"
_NAME = "comp-test"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    return SQLiteHandler(tmp_path)


class _FakeIdentity:
    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31


class _FakeChannels:
    max_channels = 3

    def get(self, idx):
        if idx == 0:
            return SimpleNamespace(name="Public", secret=b"\x00" * 16)
        return None


class _FakeBridge:
    def __init__(self):
        self.prefs = SimpleNamespace(
            node_name="TestNode", adv_type=1, latitude=47.6, longitude=-122.3
        )
        self.channels = _FakeChannels()

    def get_public_key(self):
        return bytes([_HASH_BYTE]) + b"\x22" * 31

    def get_self_info(self):
        return self.prefs

    def get_contacts(self):
        return [
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


def _daemon(handler):
    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: (
            [(_NAME, _FakeIdentity(), {})] if t == "companion" else []
        )
    )
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={_HASH_BYTE: _FakeBridge()},
        repeater_handler=SimpleNamespace(
            storage=SimpleNamespace(sqlite_handler=handler)
        ),
    )


@pytest.fixture
def endpoints(handler):
    return CompanionsV1(daemon_instance=_daemon(handler), config={})


@pytest.fixture(autouse=True)
def request_context():
    """Minimal CherryPy request/response state for direct handler calls.

    ``request.user`` defaults to an admin-scope caller: these tests exercise
    snapshot/sync/messages, not scope enforcement itself (see
    tests/test_mobile_pairing.py for that), and _resolve now runs a scope
    check (design doc §11.1) that would otherwise 403 every call here.
    """
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.request.user = {"username": "test", "auth_type": "jwt", "scope": "admin"}
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    cherrypy.serving.response.status = None


def _call(bound_method, **kwargs):
    """Invoke an endpoint bypassing require_auth (via functools.wraps chain)."""
    return bound_method.__wrapped__(bound_method.__self__, **kwargs)


def _seed_events(handler, count, event_type="message"):
    seqs = []
    for i in range(count):
        seq = handler.companion_append_event(
            _HASH, event_type, {"n": i}, packet_hash=f"ph-{i}"
        )
        seqs.append(seq)
    return seqs


# --- Companions list ---------------------------------------------------------


class TestCompanionsList:
    def test_lists_configured_companions(self, endpoints):
        result = _call(endpoints.index)
        assert result["success"] is True
        assert len(result["data"]) == 1
        item = result["data"][0]
        assert item["name"] == _NAME
        assert item["companion_hash"] == _HASH
        assert item["node_name"] == "TestNode"

    def test_mounts_under_root(self, handler):
        root = MobileAPIEndpoints(daemon_instance=_daemon(handler))
        assert isinstance(root.companions, CompanionsV1)


# --- Snapshot ----------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_shape_and_cursor_is_head(self, endpoints, handler):
        seqs = _seed_events(handler, 3)
        handler.companion_push_message(
            _HASH, {"text": "hello", "timestamp": 1, "packet_hash": "msg-1"}
        )
        result = _call(endpoints.snapshot, companion_name=_NAME)
        data = result["data"]
        assert data["cursor"] == str(seqs[-1])
        assert data["journal_epoch"] == handler.companion_journal_epoch()
        assert data["self"]["node_name"] == "TestNode"
        assert len(data["contacts"]) == 1
        assert data["channels"] == [{"index": 0, "name": "Public"}]
        assert len(data["messages"]) == 1
        assert data["messages"][0]["text"] == "hello"
        assert "secret" not in data["channels"][0]

    def test_snapshot_messages_oldest_first(self, endpoints, handler):
        for i in range(3):
            handler.companion_push_message(
                _HASH, {"text": f"m{i}", "timestamp": i, "packet_hash": f"m-{i}"}
            )
        data = _call(endpoints.snapshot, companion_name=_NAME)["data"]
        assert [m["text"] for m in data["messages"]] == ["m0", "m1", "m2"]

    def test_snapshot_etag_304(self, endpoints, handler):
        _seed_events(handler, 1)
        _call(endpoints.snapshot, companion_name=_NAME)
        etag = cherrypy.serving.response.headers["ETag"]

        cherrypy.serving.request.headers = {"If-None-Match": etag}
        result = _call(endpoints.snapshot, companion_name=_NAME)
        assert result is None
        assert cherrypy.serving.response.status == 304

    def test_snapshot_etag_changes_with_new_events(self, endpoints, handler):
        _seed_events(handler, 1)
        _call(endpoints.snapshot, companion_name=_NAME)
        etag = cherrypy.serving.response.headers["ETag"]

        _seed_events(handler, 1)
        cherrypy.serving.request.headers = {"If-None-Match": etag}
        result = _call(endpoints.snapshot, companion_name=_NAME)
        assert result is not None
        assert cherrypy.serving.response.status != 304

    def test_unknown_companion_404(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.snapshot, companion_name="nope")
        assert exc.value.status == 404


# --- Sync --------------------------------------------------------------------


class TestSync:
    def test_delta_ordering_and_next_cursor(self, endpoints, handler):
        seqs = _seed_events(handler, 5)
        data = _call(endpoints.sync, companion_name=_NAME, cursor=str(seqs[1]))["data"]
        assert [e["seq"] for e in data["events"]] == seqs[2:]
        assert data["next_cursor"] == str(seqs[-1])
        assert data["has_more"] is False
        assert data["snapshot_required"] is False
        assert data["events"][0]["type"] == "message"
        assert data["events"][0]["data"] == {"n": 2}
        assert data["events"][0]["packet_hash"] == "ph-2"

    def test_has_more_pagination(self, endpoints, handler):
        seqs = _seed_events(handler, 5)
        data = _call(endpoints.sync, companion_name=_NAME, cursor="0", limit="2")["data"]
        assert len(data["events"]) == 2
        assert data["has_more"] is True
        assert data["next_cursor"] == str(seqs[1])

        data2 = _call(
            endpoints.sync, companion_name=_NAME, cursor=data["next_cursor"], limit="3"
        )["data"]
        assert [e["seq"] for e in data2["events"]] == seqs[2:]
        assert data2["has_more"] is False

    def test_empty_delta_when_up_to_date(self, endpoints, handler):
        seqs = _seed_events(handler, 2)
        data = _call(endpoints.sync, companion_name=_NAME, cursor=str(seqs[-1]))["data"]
        assert data["events"] == []
        assert data["next_cursor"] == str(seqs[-1])
        assert data["has_more"] is False

    def test_cursor_below_prune_floor_requires_snapshot(self, endpoints, handler):
        _seed_events(handler, 3)
        handler.companion_journal_meta_set("prune_floor", "2")
        data = _call(endpoints.sync, companion_name=_NAME, cursor="1")["data"]
        assert data["snapshot_required"] is True
        assert data["events"] == []

        data_ok = _call(endpoints.sync, companion_name=_NAME, cursor="2")["data"]
        assert data_ok["snapshot_required"] is False

    def test_missing_or_bad_cursor_400(self, endpoints, handler):
        for bad in (None, "abc", "-3"):
            with pytest.raises(cherrypy.HTTPError) as exc:
                _call(endpoints.sync, companion_name=_NAME, cursor=bad)
            assert exc.value.status == 400

    def test_sync_etag_304(self, endpoints, handler):
        _seed_events(handler, 2)
        _call(endpoints.sync, companion_name=_NAME, cursor="0")
        etag = cherrypy.serving.response.headers["ETag"]

        cherrypy.serving.request.headers = {"If-None-Match": etag}
        result = _call(endpoints.sync, companion_name=_NAME, cursor="0")
        assert result is None
        assert cherrypy.serving.response.status == 304


# --- Sync: rf_reception opt-in filtering (design doc §9) --------------------


class TestSyncRfReceptionFiltering:
    """rf_reception events (opt-in firehose) are excluded from sync's
    ``events`` list unless ``?include=rf_receptions`` is given, but the
    cursor still advances past filtered rows (design doc §9)."""

    @staticmethod
    def _seed_mixed(handler):
        seqs = []
        seqs.append(
            handler.companion_append_event(_HASH, "message", {"n": 0}, packet_hash="ph-0")
        )
        seqs.append(
            handler.companion_append_event(
                _HASH, "rf_reception", {"n": 1}, packet_hash="ph-1"
            )
        )
        seqs.append(
            handler.companion_append_event(_HASH, "message", {"n": 2}, packet_hash="ph-2")
        )
        return seqs

    def test_excluded_by_default(self, endpoints, handler):
        seqs = self._seed_mixed(handler)
        data = _call(endpoints.sync, companion_name=_NAME, cursor="0")["data"]
        assert [e["type"] for e in data["events"]] == ["message", "message"]
        # Cursor still advances past the filtered-out rf_reception row.
        assert data["next_cursor"] == str(seqs[-1])

    def test_included_with_include_param(self, endpoints, handler):
        seqs = self._seed_mixed(handler)
        data = _call(
            endpoints.sync, companion_name=_NAME, cursor="0", include="rf_receptions"
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "rf_reception", "message"]
        assert data["next_cursor"] == str(seqs[-1])

    def test_unknown_include_tokens_ignored(self, endpoints, handler):
        self._seed_mixed(handler)
        data = _call(
            endpoints.sync, companion_name=_NAME, cursor="0", include="bogus,other"
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "message"]

    def test_include_with_multiple_tokens_still_matches(self, endpoints, handler):
        self._seed_mixed(handler)
        data = _call(
            endpoints.sync, companion_name=_NAME, cursor="0", include="foo,rf_receptions"
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "rf_reception", "message"]

    def test_has_more_unaffected_by_filtering(self, endpoints, handler):
        # 3 rows in the page (limit=3), all scanned regardless of filtering;
        # has_more reflects the unfiltered row count against the limit.
        self._seed_mixed(handler)
        data = _call(
            endpoints.sync, companion_name=_NAME, cursor="0", limit="3"
        )["data"]
        assert data["has_more"] is False


# --- Messages ----------------------------------------------------------------


class TestMessages:
    def test_paging_with_before_id(self, endpoints, handler):
        for i in range(5):
            handler.companion_push_message(
                _HASH, {"text": f"m{i}", "timestamp": i, "packet_hash": f"pm-{i}"}
            )
        page1 = _call(endpoints.messages, companion_name=_NAME, limit="2")["data"]
        assert [m["text"] for m in page1["messages"]] == ["m4", "m3"]
        assert page1["next_before_id"] == page1["messages"][-1]["id"]

        page2 = _call(
            endpoints.messages,
            companion_name=_NAME,
            before_id=str(page1["next_before_id"]),
            limit="2",
        )["data"]
        assert [m["text"] for m in page2["messages"]] == ["m2", "m1"]

    def test_consumed_messages_still_listed(self, endpoints, handler):
        handler.companion_push_message(
            _HASH, {"text": "popped", "timestamp": 1, "packet_hash": "pc-1"}
        )
        assert handler.companion_pop_message(_HASH) is not None
        data = _call(endpoints.messages, companion_name=_NAME)["data"]
        assert len(data["messages"]) == 1
        assert data["messages"][0]["consumed_at"] is not None

    def test_bad_before_id_400(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME, before_id="xyz")
        assert exc.value.status == 400
