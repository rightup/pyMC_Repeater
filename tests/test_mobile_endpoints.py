"""Tests for the Mobile Companion API v1 endpoints (phase 1).

Covers /api/v1/companions list, snapshot (cursor + ETag/304), sync (delta
ordering, has_more, prune-floor -> snapshot_required, bad cursor), and
message history paging. Handlers are invoked directly through
``__wrapped__`` (require_auth uses functools.wraps), with a minimal
CherryPy request context set up per test; storage is a real SQLiteHandler
on tmp_path, matching the other companion storage tests.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import cherrypy
import pytest

import repeater.web.mobile_endpoints as mobile_module
from repeater.companion.bridge import PUBLIC_PREF_FIELDS
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import CompanionStorageError, SQLiteHandler
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
        get_identities_by_type=lambda t: [(_NAME, _FakeIdentity(), {})] if t == "companion" else []
    )
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges={_HASH_BYTE: _FakeBridge()},
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )


@pytest.fixture
def endpoints(handler):
    endpoint = CompanionsV1(
        daemon_instance=_daemon(handler),
        config={},
        event_loop=object(),
    )
    endpoint._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
    return endpoint


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


def _mobile_message(
    index: int = 0,
    *,
    text: str | None = None,
    packet_hash: str | None = None,
    **extra,
) -> dict:
    """One complete, documented MobileMessage with optional private extras."""

    message = {
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
    message.update(extra)
    return message


def _mobile_contact_event(**extra) -> dict:
    contact = {
        "pubkey": "aa" * 32,
        "name": "Alice",
        "adv_type": 1,
        "flags": 0,
        "out_path_len": -1,
        "last_advert_timestamp": 123,
        "lastmod": 124,
        "gps_lat": 0.0,
        "gps_lon": 0.0,
        "change": "update",
    }
    contact.update(extra)
    return contact


def _seed_events(handler, count, event_type="message"):
    seqs = []
    for i in range(count):
        payload = _mobile_message(i) if event_type == "message" else {"text": f"m{i}"}
        seq = handler.companion_append_event(
            _HASH,
            event_type,
            payload,
            packet_hash=f"{i:016x}",
        )
        seqs.append(seq)
    return seqs


def _cursor(handler, seq):
    return f"{handler.companion_journal_epoch()}:{seq}"


# --- Companions list ---------------------------------------------------------


class TestCompanionsList:
    def test_lists_configured_companions(self, endpoints):
        result = _call(endpoints.index)
        assert result["success"] is True
        assert cherrypy.serving.response.headers["Cache-Control"] == "no-store"
        assert len(result["data"]) == 1
        item = result["data"][0]
        assert item["name"] == _NAME
        assert item["companion_hash"] == _HASH
        assert item["node_name"] == "TestNode"
        assert item["capabilities"] == {"max_channels": 3}

    def test_mounts_under_root(self, handler):
        root = MobileAPIEndpoints(daemon_instance=_daemon(handler))
        assert isinstance(root.companions, CompanionsV1)


# --- Snapshot ----------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_shape_and_cursor_is_head(self, endpoints, handler):
        seqs = _seed_events(handler, 3)
        handler.companion_push_message(
            _HASH,
            {
                "text": "hello",
                "timestamp": 1,
                "packet_hash": "1111111111111111",
            },
        )
        result = _call(endpoints.snapshot, companion_name=_NAME)
        data = result["data"]
        assert cherrypy.serving.response.headers["Cache-Control"] == (
            "private, no-store, no-cache, no-transform"
        )
        assert data["cursor"] == _cursor(handler, seqs[-1])
        assert data["journal_epoch"] == handler.companion_journal_epoch()
        assert data["self"]["node_name"] == "TestNode"
        assert set(data["self"]) == {"public_key", *PUBLIC_PREF_FIELDS}
        assert len(data["contacts"]) == 1
        assert data["channels"] == [{"index": 0, "name": "Public"}]
        assert len(data["messages"]) == 1
        assert data["messages"][0]["text"] == "hello"
        assert "consumed_at" not in data["messages"][0]
        assert "pending_for_frame" not in data["messages"][0]
        assert "secret" not in data["channels"][0]

    def test_snapshot_messages_oldest_first(self, endpoints, handler):
        for i in range(3):
            handler.companion_push_message(
                _HASH,
                {
                    "text": f"m{i}",
                    "timestamp": i,
                    "packet_hash": f"{100 + i:016x}",
                },
            )
        data = _call(endpoints.snapshot, companion_name=_NAME)["data"]
        assert [m["text"] for m in data["messages"]] == ["m0", "m1", "m2"]

    def test_snapshot_fails_closed_on_malformed_message_hash(self, endpoints, handler):
        handler.companion_push_message(
            _HASH,
            {
                "text": "legacy",
                "timestamp": 1,
                "packet_hash": "legacy-not-hex",
            },
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.snapshot, companion_name=_NAME)
        assert exc.value.status == 503

    def test_snapshot_etag_304(self, endpoints, handler):
        _seed_events(handler, 1)
        _call(endpoints.snapshot, companion_name=_NAME)
        etag = cherrypy.serving.response.headers["ETag"]

        cherrypy.serving.request.headers = {"If-None-Match": etag}
        result = _call(endpoints.snapshot, companion_name=_NAME)
        assert result is None
        assert cherrypy.serving.response.status == 304
        assert cherrypy.serving.response.headers["Cache-Control"] == (
            "private, no-store, no-cache, no-transform"
        )

    def test_snapshot_etag_changes_with_new_events(self, endpoints, handler):
        _seed_events(handler, 1)
        _call(endpoints.snapshot, companion_name=_NAME)
        etag = cherrypy.serving.response.headers["ETag"]

        _seed_events(handler, 1)
        cherrypy.serving.request.headers = {"If-None-Match": etag}
        result = _call(endpoints.snapshot, companion_name=_NAME)
        assert result is not None
        assert cherrypy.serving.response.status != 304

    def test_snapshot_etag_changes_with_server_version(
        self,
        endpoints,
        handler,
        monkeypatch,
    ):
        _seed_events(handler, 1)
        monkeypatch.setattr(mobile_module, "_REPEATER_VERSION", "1.0.0")
        _call(endpoints.snapshot, companion_name=_NAME)
        old_etag = cherrypy.serving.response.headers["ETag"]

        monkeypatch.setattr(mobile_module, "_REPEATER_VERSION", "1.0.1")
        cherrypy.serving.request.headers = {"If-None-Match": old_etag}
        result = _call(endpoints.snapshot, companion_name=_NAME)

        assert result is not None
        assert result["data"]["server"] == {"version": "1.0.1"}
        assert cherrypy.serving.response.status != 304

    def test_frame_delivery_does_not_change_public_snapshot_or_etag(self, endpoints, handler):
        handler.companion_push_message(
            _HASH,
            {
                "text": "queued",
                "timestamp": 1,
                "packet_hash": "EEEEEEEEEEEEEEEE",
            },
        )
        first = _call(endpoints.snapshot, companion_name=_NAME)["data"]
        etag = cherrypy.serving.response.headers["ETag"]
        assert "consumed_at" not in first["messages"][0]
        assert "pending_for_frame" not in first["messages"][0]

        assert handler.companion_pop_message(_HASH) is not None
        cherrypy.serving.request.headers = {"If-None-Match": etag}

        assert _call(endpoints.snapshot, companion_name=_NAME) is None
        assert cherrypy.serving.response.status == 304

    def test_unknown_companion_404(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.snapshot, companion_name="nope")
        assert exc.value.status == 404

    def test_message_storage_failure_is_503_not_empty_snapshot(
        self, endpoints, handler, monkeypatch
    ):
        def unavailable(*args, **kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(handler, "companion_get_messages_strict", unavailable)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.snapshot, companion_name=_NAME)
        assert exc.value.status == 503


# --- Sync --------------------------------------------------------------------


class TestSync:
    def test_delta_ordering_and_next_cursor(self, endpoints, handler):
        seqs = _seed_events(handler, 5)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, seqs[1]),
        )["data"]
        assert cherrypy.serving.response.headers["Cache-Control"] == "no-store"
        assert [e["seq"] for e in data["events"]] == seqs[2:]
        assert data["next_cursor"] == _cursor(handler, seqs[-1])
        assert data["has_more"] is False
        assert data["snapshot_required"] is False
        assert data["events"][0]["type"] == "message"
        assert data["events"][0]["data"] == _mobile_message(2)
        assert data["events"][0]["packet_hash"] == "0000000000000002"

    def test_full_packet_hash_is_canonicalized(self, endpoints, handler):
        full_hash = "0x" + "abcdef0123456789" + ("de" * 24)
        seq = handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(0, text="valid", packet_hash=full_hash),
            packet_hash=full_hash,
        )

        events = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
        )["data"]["events"]

        assert events == [
            {
                "seq": seq,
                "type": "message",
                "ts": events[0]["ts"],
                "packet_hash": "ABCDEF0123456789",
                "data": _mobile_message(
                    0,
                    text="valid",
                    packet_hash="ABCDEF0123456789",
                ),
            }
        ]

    def test_malformed_event_packet_hash_is_503(self, endpoints, handler):
        handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(0, text="hidden"),
            packet_hash="not-a-packet-hash",
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=_cursor(handler, 0),
            )

        assert exc.value.status == 503

    def test_corrupt_event_is_503_and_same_cursor_can_retry(self, endpoints, handler):
        with handler._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO companion_events
                    (companion_hash, event_type, created_at, payload)
                VALUES (?, 'message', 1, '{broken')
                """,
                (_HASH,),
            )
            seq = int(inserted.lastrowid)
            conn.commit()
        cursor = _cursor(handler, 0)

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=cursor,
            )
        assert exc.value.status == 503

        with handler._connect() as conn:
            conn.execute(
                """
                UPDATE companion_events
                SET payload = ?, packet_hash = ?
                WHERE seq = ?
                """,
                (
                    json.dumps(_mobile_message(0, text="repaired")),
                    "0000000000000000",
                    seq,
                ),
            )
            conn.commit()
        event = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=cursor,
        )["data"]["events"][0]
        assert event["seq"] == seq
        assert event["data"] == _mobile_message(0, text="repaired")

    def test_has_more_pagination(self, endpoints, handler):
        seqs = _seed_events(handler, 5)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
            limit="2",
        )["data"]
        assert len(data["events"]) == 2
        assert data["has_more"] is True
        assert data["next_cursor"] == _cursor(handler, seqs[1])

        data2 = _call(endpoints.sync, companion_name=_NAME, cursor=data["next_cursor"], limit="3")[
            "data"
        ]
        assert [e["seq"] for e in data2["events"]] == seqs[2:]
        assert data2["has_more"] is False

    def test_empty_delta_when_up_to_date(self, endpoints, handler):
        seqs = _seed_events(handler, 2)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, seqs[-1]),
        )["data"]
        assert data["events"] == []
        assert data["next_cursor"] == _cursor(handler, seqs[-1])
        assert data["has_more"] is False

    def test_cursor_below_prune_floor_requires_snapshot(self, endpoints, handler):
        _seed_events(handler, 3)
        with handler._connect() as conn:
            conn.execute(
                """
                INSERT INTO companion_journal_floors
                    (companion_hash, prune_floor)
                VALUES (?, ?)
                ON CONFLICT(companion_hash) DO UPDATE SET
                    prune_floor = excluded.prune_floor
                """,
                (_HASH, 2),
            )
            conn.commit()
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 1),
        )["data"]
        assert data["snapshot_required"] is True
        assert data["events"] == []

        data_ok = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 2),
        )["data"]
        assert data_ok["snapshot_required"] is False

    def test_missing_or_bad_cursor_400(self, endpoints, handler):
        for bad in (
            None,
            "abc",
            "-3",
            "é:0",
            "ABC:0",
            "abc:+1",
            " abc:0",
            "abc:9223372036854775808",
            "9223372036854775808",
        ):
            with pytest.raises(cherrypy.HTTPError) as exc:
                _call(endpoints.sync, companion_name=_NAME, cursor=bad)
            assert exc.value.status == 400

    def test_malformed_contact_event_wire_fields_are_503(self, endpoints, handler):
        handler.companion_append_event(
            _HASH,
            "contact",
            {"public_key": "aa" * 32, "flags": "not-an-integer"},
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=_cursor(handler, 0),
            )

        assert exc.value.status == 503

    def test_sync_cursor_is_the_only_cache_condition(self, endpoints, handler):
        _seed_events(handler, 2)
        result = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
        )
        assert result["data"]["events"]
        assert "ETag" not in cherrypy.serving.response.headers

    def test_message_events_hide_frame_delivery_bookkeeping(self, endpoints, handler):
        handler.companion_append_event(
            _HASH,
            "message",
            _mobile_message(
                6,
                text="hello",
                consumed_at=123.0,
                pending_for_frame=False,
            ),
            packet_hash="0000000000000006",
        )

        event = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
        )["data"]["events"][0]

        assert event["data"] == _mobile_message(6, text="hello")

    def test_contact_events_match_snapshot_wire_shape(self, endpoints, handler):
        handler.companion_append_event(
            _HASH,
            "contact",
            _mobile_contact_event(
                flags=0x91,
                out_path_len=2,
                out_path="0102",
                last_advert_packet="deadbeef",
                sync_since=5,
            ),
        )

        event = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
        )["data"]["events"][0]

        assert event["data"] == {
            "public_key": "aa" * 32,
            "name": "Alice",
            "adv_type": 1,
            "flags": 0x91,
            "favorite": True,
            "out_path_len": 2,
            "last_advert_timestamp": 123,
            "lastmod": 124,
            "gps_lat": 0.0,
            "gps_lon": 0.0,
            "change": "update",
        }

    @pytest.mark.parametrize(
        ("event_type", "payload", "expected"),
        [
            (
                "message",
                _mobile_message(
                    0,
                    text="hello",
                    pending_for_frame=True,
                    private="x",
                ),
                _mobile_message(0, text="hello"),
            ),
            (
                "contact",
                _mobile_contact_event(out_path="01", private="x"),
                {
                    "public_key": "aa" * 32,
                    "name": "Alice",
                    "adv_type": 1,
                    "flags": 0,
                    "favorite": False,
                    "out_path_len": -1,
                    "last_advert_timestamp": 123,
                    "lastmod": 124,
                    "gps_lat": 0.0,
                    "gps_lon": 0.0,
                    "change": "update",
                },
            ),
            (
                "channel",
                {
                    "index": 1,
                    "name": "#chat",
                    "change": "update",
                    "secret": "do-not-send",
                },
                {"index": 1, "name": "#chat", "change": "update"},
            ),
            (
                "prefs",
                {"node_name": "Ridge", "private_key": "do-not-send"},
                {"node_name": "Ridge"},
            ),
            (
                "message_reception",
                {
                    "message_id": 1,
                    "packet_hash": "1111111111111111",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                    "observation_count": 2,
                    "unique_path_count": 1,
                    "private": "x",
                },
                {
                    "message_id": 1,
                    "packet_hash": "1111111111111111",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                    "observation_count": 2,
                    "unique_path_count": 1,
                },
            ),
            (
                "message_send_state",
                {
                    "message_id": 1,
                    "state": "transmitted",
                    "packet_hash": "2222222222222222",
                    "expected_ack": 7,
                    "private": "x",
                },
                {
                    "message_id": 1,
                    "state": "transmitted",
                    "packet_hash": "2222222222222222",
                    "expected_ack": 7,
                },
            ),
            (
                "rf_reception",
                {
                    "packet_hash": "3333333333333333",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                    "private": "x",
                },
                {
                    "packet_hash": "3333333333333333",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                },
            ),
        ],
    )
    def test_known_event_payloads_expose_only_public_fields(
        self,
        event_type,
        payload,
        expected,
    ):
        event = CompanionsV1._event_to_wire(
            {
                "seq": 1,
                "event_type": event_type,
                "created_at": 1.0,
                "packet_hash": (
                    expected.get("packet_hash")
                    if event_type
                    in {
                        "message",
                        "message_reception",
                        "message_send_state",
                        "rf_reception",
                    }
                    else None
                ),
                "payload": payload,
            }
        )

        assert event["data"] == expected

    def test_unknown_server_event_type_keeps_envelope_but_sanitizes_payload(self):
        event = CompanionsV1._event_to_wire(
            {
                "seq": 1,
                "event_type": "future.event-v2",
                "created_at": 1.0,
                "packet_hash": None,
                "payload": {"private": True},
            }
        )

        assert event["type"] == "future.event-v2"
        assert event["data"] == {}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("seq", 0),
            ("seq", True),
            ("created_at", float("nan")),
            ("packet_hash", "not-a-packet-hash"),
        ],
    )
    def test_event_envelope_rejects_malformed_wire_fields(self, field, value):
        row = {
            "seq": 1,
            "event_type": "message",
            "created_at": 1.0,
            "packet_hash": None,
            "payload": _mobile_message(0, text="hidden"),
        }
        row[field] = value

        with pytest.raises(ValueError):
            CompanionsV1._event_to_wire(row)

    @pytest.mark.parametrize(
        ("event_type", "payload"),
        [
            (
                "message",
                _mobile_message(0, packet_hash="2222222222222222"),
            ),
            (
                "message_reception",
                {
                    "message_id": 1,
                    "packet_hash": "2222222222222222",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                    "observation_count": 1,
                    "unique_path_count": 1,
                },
            ),
            (
                "message_send_state",
                {
                    "message_id": 1,
                    "state": "transmitted",
                    "packet_hash": "2222222222222222",
                    "expected_ack": None,
                },
            ),
            (
                "rf_reception",
                {
                    "packet_hash": "2222222222222222",
                    "path": ["01"],
                    "rssi": -80,
                    "snr": 2.0,
                    "observed_at": 1.0,
                },
            ),
        ],
    )
    def test_event_envelope_packet_hash_must_match_known_payload(
        self,
        event_type,
        payload,
    ):
        with pytest.raises(ValueError, match="does not match"):
            CompanionsV1._event_to_wire(
                {
                    "seq": 1,
                    "event_type": event_type,
                    "created_at": 1.0,
                    "packet_hash": "1111111111111111",
                    "payload": payload,
                }
            )

    def test_send_state_allows_matching_null_packet_hashes(self):
        event = CompanionsV1._event_to_wire(
            {
                "seq": 1,
                "event_type": "message_send_state",
                "created_at": 1.0,
                "packet_hash": None,
                "payload": {
                    "message_id": 1,
                    "state": "pending",
                    "packet_hash": None,
                    "expected_ack": None,
                },
            }
        )

        assert event["packet_hash"] is None
        assert event["data"]["packet_hash"] is None

    def test_unsafe_event_type_is_503_before_it_can_break_sse_framing(
        self,
        endpoints,
        handler,
    ):
        handler.companion_append_event(
            _HASH,
            "message\ndata: injected",
            _mobile_message(0, text="hidden"),
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=_cursor(handler, 0),
            )

        assert exc.value.status == 503

    def test_reserved_snapshot_required_journal_type_is_503(self, endpoints, handler):
        handler.companion_append_event(
            _HASH,
            "snapshot_required",
            {"private": "must-not-become-an-SSE-control"},
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=_cursor(handler, 0),
            )

        assert exc.value.status == 503


# --- Sync: rf_reception opt-in filtering (design doc §9) --------------------


class TestSyncRfReceptionFiltering:
    """rf_reception events (opt-in firehose) are excluded from sync's
    ``events`` list unless ``?include=rf_receptions`` is given, but the
    cursor still advances past filtered rows (design doc §9)."""

    @staticmethod
    def _seed_mixed(handler):
        seqs = []
        seqs.append(
            handler.companion_append_event(
                _HASH,
                "message",
                _mobile_message(0),
                packet_hash="0000000000000000",
            )
        )
        seqs.append(
            handler.companion_append_event(
                _HASH,
                "rf_reception",
                {
                    "packet_hash": "0000000000000001",
                    "rssi": -80,
                    "snr": 2.0,
                    "path": ["01"],
                    "observed_at": 1.0,
                },
                packet_hash="0000000000000001",
            )
        )
        seqs.append(
            handler.companion_append_event(
                _HASH,
                "message",
                _mobile_message(2),
                packet_hash="0000000000000002",
            )
        )
        return seqs

    def test_excluded_by_default(self, endpoints, handler):
        seqs = self._seed_mixed(handler)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "message"]
        # Cursor still advances past the filtered-out rf_reception row.
        assert data["next_cursor"] == _cursor(handler, seqs[-1])

    def test_included_with_include_param(self, endpoints, handler):
        seqs = self._seed_mixed(handler)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
            include="rf_receptions",
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "rf_reception", "message"]
        assert data["next_cursor"] == _cursor(handler, seqs[-1])

    @pytest.mark.parametrize(
        "include",
        ["bogus", "bogus,other", "", "rf_receptions,", ",rf_receptions"],
    )
    def test_unknown_or_blank_include_tokens_are_rejected(
        self,
        endpoints,
        handler,
        include,
    ):
        self._seed_mixed(handler)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.sync,
                companion_name=_NAME,
                cursor=_cursor(handler, 0),
                include=include,
            )
        assert exc.value.status == 400

    def test_duplicate_known_include_token_is_harmless(self, endpoints, handler):
        self._seed_mixed(handler)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
            include="rf_receptions, rf_receptions",
        )["data"]
        assert [e["type"] for e in data["events"]] == ["message", "rf_reception", "message"]

    def test_has_more_unaffected_by_filtering(self, endpoints, handler):
        # 3 rows in the page (limit=3), all scanned regardless of filtering;
        # has_more reflects the unfiltered row count against the limit.
        self._seed_mixed(handler)
        data = _call(
            endpoints.sync,
            companion_name=_NAME,
            cursor=_cursor(handler, 0),
            limit="3",
        )["data"]
        assert data["has_more"] is False


# --- Messages ----------------------------------------------------------------


class TestMessages:
    def test_paging_with_before_id(self, endpoints, handler):
        for i in range(5):
            handler.companion_push_message(
                _HASH,
                {
                    "text": f"m{i}",
                    "timestamp": i,
                    "packet_hash": f"{200 + i:016x}",
                },
            )
        page1 = _call(endpoints.messages, companion_name=_NAME, limit="2")["data"]
        assert cherrypy.serving.response.headers["Cache-Control"] == "no-store"
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
            _HASH,
            {
                "text": "popped",
                "timestamp": 1,
                "packet_hash": "CCCCCCCCCCCCCCCC",
            },
        )
        assert handler.companion_pop_message(_HASH) is not None
        data = _call(endpoints.messages, companion_name=_NAME)["data"]
        assert len(data["messages"]) == 1
        assert data["messages"][0]["text"] == "popped"
        assert "consumed_at" not in data["messages"][0]
        assert "pending_for_frame" not in data["messages"][0]

    def test_bad_before_id_400(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME, before_id="xyz")
        assert exc.value.status == 400

    def test_before_id_is_bounded_to_sqlite_integer_range(self, endpoints):
        maximum = (1 << 63) - 1
        result = _call(
            endpoints.messages,
            companion_name=_NAME,
            before_id=str(maximum),
        )
        assert result["success"] is True

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.messages,
                companion_name=_NAME,
                before_id=str(maximum + 1),
            )
        assert exc.value.status == 400

    def test_storage_failure_is_503_not_empty_history(self, endpoints, handler, monkeypatch):
        def unavailable(*args, **kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(handler, "companion_get_messages_strict", unavailable)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 503

    def test_corrupt_message_row_is_503_not_coerced_history(self, endpoints, handler):
        handler.companion_push_message(
            _HASH,
            {
                "text": "corrupt",
                "timestamp": 1,
                "packet_hash": "ABCDEF0123456789",
            },
        )
        with handler._connect() as conn:
            conn.execute(
                """
                UPDATE companion_messages
                SET is_channel = 2
                WHERE companion_hash = ?
                """,
                (_HASH,),
            )
            conn.commit()

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.messages, companion_name=_NAME)
        assert exc.value.status == 503


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_success_normalizes_unavailable_non_finite_sensor_values(value):
    assert CompanionsV1._success({"value": value}) == {
        "success": True,
        "data": {"value": None},
    }


def test_every_real_journal_payload_shape_passes_mobile_wire_validation(handler):
    journal = CompanionEventJournal(handler, _HASH)
    outbound_hash = "1111111111111111"
    outbound = journal.store_outbound_message(
        {
            "recipient_key": b"\x22" * 32,
            "text": "outbound",
            "timestamp": 1,
            "packet_hash": outbound_hash,
        },
        "rest",
        "transmitted",
    )
    journal.update_outbound_state(
        outbound["message_id"],
        "confirmed",
        packet_hash=outbound_hash,
        expected_ack=7,
    )
    journal.record_outbound_heard_repeat(
        {
            "message_id": outbound["message_id"],
            "packet_hash": outbound_hash,
            "path": ["01"],
            "terminal_hash": "01",
            "rssi": -70,
            "snr": 3.0,
            "observed_at": 2.0,
            "heard_repeat_count": 1,
            "unique_repeater_count": 1,
        }
    )

    inbound_hash = "2222222222222222"
    inbound = journal.store_inbound_message(
        {
            "sender_key": b"\x33" * 32,
            "text": "inbound",
            "timestamp": 2,
            "packet_hash": inbound_hash,
        }
    )
    journal.record_inbound_reception(
        {
            "message_id": inbound["message_id"],
            "packet_hash": inbound_hash,
            "path": ["02"],
            "rssi": -80,
            "snr": 2.0,
            "observed_at": 3.0,
            "observation_count": 2,
            "unique_path_count": 2,
        }
    )
    journal.store_contact(
        {
            "pubkey": b"\x44" * 32,
            "name": "Contact",
            "adv_type": 1,
            "flags": 0,
            "out_path_len": -1,
            "last_advert_timestamp": 0,
            "lastmod": 0,
            "gps_lat": 0.0,
            "gps_lon": 0.0,
        },
        "new",
    )
    journal.store_channel(1, "#chat", b"\x55" * 16)
    journal.store_prefs({"node_name": "Node"}, {"node_name": "Node"})
    journal.record_rf_reception(
        {
            "packet_hash": "3333333333333333",
            "original_path": ["03"],
            "rssi": -90,
            "snr": 1.0,
            "timestamp": 4.0,
        }
    )

    rows = handler.companion_sync_page(
        _HASH,
        handler.companion_journal_epoch(),
        0,
        100,
    )["events"]
    wire = [CompanionsV1._event_to_wire(row) for row in rows]

    assert {event["type"] for event in wire} == {
        "message",
        "message_send_state",
        "message_reception",
        "contact",
        "channel",
        "prefs",
        "rf_reception",
    }
    assert all("secret" not in event["data"] for event in wire)
