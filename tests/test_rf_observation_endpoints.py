"""Tests for the Mobile Companion API v1 RF observation surface (phase 3).

Covers design doc §10: window parse/clamp (§10.1), receptions (§10.1),
contact paths (§10.1), transmission repeats (§10.3 predicate), path-hash
resolution (§10.5), and observations_pruned (§10.6). Handlers are invoked
directly through ``__wrapped__`` (require_auth uses functools.wraps), same
pattern as tests/test_mobile_pairing.py / test_mobile_endpoints.py. Storage
is a real SQLiteHandler on tmp_path so packets/companion_messages round-trip
for real.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.companion.path_resolution import resolve_path
from repeater.companion.rf_window import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    MIN_WINDOW_SECONDS,
    observations_pruned,
    parse_window_seconds,
)
from repeater.data_acquisition.sqlite_handler import CompanionStorageError, SQLiteHandler
from repeater.web.mobile_endpoints import CompanionsV1

_HASH_BYTE = 0x01
_HASH = "0x01"
_OTHER_HASH_BYTE = 0x02
_OTHER_HASH = "0x02"
_NAME = "comp-test"
_OTHER_NAME = "comp-other"

_SENDER_PUBKEY_HEX = "aa" * 32
_SENDER_KEY_BYTES = bytes.fromhex(_SENDER_PUBKEY_HEX)


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    return SQLiteHandler(tmp_path)


class _FakeIdentity:
    def __init__(self, hash_byte):
        self._hash_byte = hash_byte

    def get_public_key(self):
        return bytes([self._hash_byte]) + b"\x22" * 31


class _FakeBridge:
    def __init__(self, hash_byte):
        self._hash_byte = hash_byte

    def get_public_key(self):
        return bytes([self._hash_byte]) + b"\x22" * 31


_HASH_BYTES_BY_NAME = {_NAME: _HASH_BYTE, _OTHER_NAME: _OTHER_HASH_BYTE}


def _daemon(handler, names=(_NAME, _OTHER_NAME)):
    identities = [(name, _FakeIdentity(_HASH_BYTES_BY_NAME[name]), {}) for name in names]
    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: identities if t == "companion" else []
    )
    bridges = {_HASH_BYTES_BY_NAME[name]: _FakeBridge(_HASH_BYTES_BY_NAME[name]) for name in names}
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges=bridges,
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )


@pytest.fixture
def daemon(handler):
    return _daemon(handler)


@pytest.fixture
def endpoints(daemon):
    return CompanionsV1(daemon_instance=daemon, config={})


@pytest.fixture(autouse=True)
def request_context():
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    cherrypy.serving.request.user = {
        "username": "adam",
        "auth_type": "jwt",
        "scope": "admin",
    }
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    cherrypy.serving.response.status = None


def _set_user(scope=None, **extra):
    user = dict(extra)
    if scope is not None:
        user["scope"] = scope
    cherrypy.serving.request.user = user


def _call(bound_method, **kwargs):
    return bound_method.__wrapped__(bound_method.__self__, **kwargs)


def _store_packet(handler, **overrides):
    record = {
        "timestamp": time.time(),
        "type": 1,
        "route": 0,
        "length": 10,
        "rssi": -80,
        "snr": 5.0,
        "transmitted": False,
        "is_duplicate": False,
        "packet_hash": "ABCDEF0123456789",
        "original_path": ["71"],
        "forwarded_path": [],
    }
    record.update(overrides)
    packet_id = handler.store_packet(record)
    if record.get("transmitted"):
        packet_hash = str(record.get("packet_hash") or "")[:16]
        if handler.companion_outbound_message_get_by_hash(_HASH, packet_hash) is None:
            handler.companion_store_outbound_message(
                _HASH,
                {
                    "sender_key": bytes([_HASH_BYTE]) + b"\x22" * 31,
                    "timestamp": int(record["timestamp"]),
                    "text": "outbound test message",
                    "is_channel": True,
                    "channel_idx": 0,
                    "packet_hash": packet_hash,
                },
                "rest",
                "transmitted",
            )
    return packet_id


def _push_message(handler, companion_hash=_HASH, **overrides):
    msg = {
        "sender_key": _SENDER_KEY_BYTES,
        "sender_prefix": _SENDER_KEY_BYTES[:4],
        "txt_type": 0,
        "timestamp": int(time.time()),
        "text": "hello",
        "is_channel": False,
        "channel_idx": 0,
        "path_len": 1,
        "snr": 5.0,
        "rssi": -80,
        "packet_hash": "ABCDEF0123456789" + "00" * 24,  # 64-char full hash
    }
    msg.update(overrides)
    handler.companion_push_message(companion_hash, msg)
    rows = handler.companion_get_messages(companion_hash, limit=1)
    return rows[0]["id"]


# --- Window parsing/clamping (§10.1) ------------------------------------------


class TestWindowParsing:
    def test_default_when_missing(self):
        assert parse_window_seconds(None) == DEFAULT_WINDOW_SECONDS

    def test_default_when_blank(self):
        assert parse_window_seconds("") == DEFAULT_WINDOW_SECONDS

    def test_bare_seconds(self):
        assert parse_window_seconds("120") == 120

    def test_minutes(self):
        assert parse_window_seconds("30m") == 30 * 60

    def test_hours(self):
        assert parse_window_seconds("24h") == 24 * 3600

    def test_days(self):
        assert parse_window_seconds("7d") == 7 * 86400

    def test_case_insensitive_unit(self):
        assert parse_window_seconds("24H") == 24 * 3600

    def test_clamps_above_max(self):
        assert parse_window_seconds("30d") == MAX_WINDOW_SECONDS

    def test_clamps_below_min(self):
        assert parse_window_seconds("1s") == MIN_WINDOW_SECONDS

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            parse_window_seconds("not-a-window")

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError):
            parse_window_seconds("5x")


class TestObservationsPruned:
    def test_within_retention_is_false(self):
        now = time.time()
        config = {"storage": {"retention": {"sqlite_cleanup_days": 31}}}
        assert observations_pruned(now - 3600, config) is False

    def test_past_retention_is_true(self):
        now = time.time()
        config = {"storage": {"retention": {"sqlite_cleanup_days": 31}}}
        window_start = now - (32 * 86400)
        assert observations_pruned(window_start, config) is True

    def test_default_retention_used_when_missing(self):
        now = time.time()
        assert observations_pruned(now - (32 * 86400), {}) is True
        assert observations_pruned(now - 3600, {}) is False


# --- Path resolution (§10.5) ---------------------------------------------------


class TestPathResolution:
    _CONTACTS = [
        {"pubkey": bytes.fromhex("71aa" + "00" * 30), "name": "Everett North"},
        {"pubkey": bytes.fromhex("71bb" + "00" * 30), "name": "Everett South"},
        {"pubkey": bytes.fromhex("9900" + "00" * 30), "name": "Solo Node"},
    ]

    def test_unique_single_byte_match(self):
        result = resolve_path(["99"], self._CONTACTS)
        assert result[0]["resolution"] == "unique"
        assert result[0]["candidates"] == [{"pubkey": "9900" + "00" * 30, "name": "Solo Node"}]

    def test_ambiguous_multiple_candidates(self):
        result = resolve_path(["71"], self._CONTACTS)
        assert result[0]["resolution"] == "ambiguous"
        assert len(result[0]["candidates"]) == 2

    def test_unknown_no_match(self):
        result = resolve_path(["ff"], self._CONTACTS)
        assert result[0]["resolution"] == "unknown"
        assert result[0]["candidates"] == []

    def test_multi_byte_disambiguates(self):
        result = resolve_path(["71aa"], self._CONTACTS)
        assert result[0]["resolution"] == "unique"
        assert result[0]["candidates"][0]["name"] == "Everett North"

    def test_case_insensitive(self):
        result = resolve_path(["71AA"], self._CONTACTS)
        assert result[0]["resolution"] == "unique"

    def test_empty_raw_hash_is_unknown(self):
        result = resolve_path([""], self._CONTACTS)
        assert result[0]["resolution"] == "unknown"

    def test_truncated_flag_when_over_cap(self):
        contacts = [
            {"pubkey": bytes.fromhex("71" + f"{i:02x}" + "00" * 29), "name": f"n{i}"}
            for i in range(8)
        ]
        result = resolve_path(["71"], contacts)
        assert result[0]["resolution"] == "ambiguous"
        assert len(result[0]["candidates"]) == 5
        assert result[0].get("truncated") is True

    def test_preserves_raw_hash_field(self):
        result = resolve_path(["ZZ"], self._CONTACTS)
        assert result[0]["raw_hash"] == "ZZ"
        assert result[0]["resolution"] == "unknown"


# --- GET .../messages/{id}/receptions -----------------------------------------


class TestReceptions:
    def test_happy_path_exact_counts(self, endpoints, handler):
        msg_id = _push_message(handler)
        ph16 = "ABCDEF0123456789"
        now = time.time()
        _store_packet(
            handler, packet_hash=ph16, timestamp=now - 100, original_path=["71"], rssi=-70
        )
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 50,
            original_path=["99"],
            is_duplicate=True,
            rssi=-90,
        )

        result = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id)
        data = result["data"]
        assert data["message_id"] == msg_id
        assert data["packet_hash"] == ph16
        assert len(data["receptions"]) == 2
        assert data["observation_count"] == 2
        assert data["unique_path_count"] == 2
        assert data["truncated"] is False
        # ordered ascending by time
        assert data["receptions"][0]["observed_at"] <= data["receptions"][1]["observed_at"]
        assert data["receptions"][0]["path"][0]["raw_hash"] == "71"

    def test_duplicates_same_path_count_once_unique(self, endpoints, handler):
        msg_id = _push_message(handler)
        ph16 = "ABCDEF0123456789"
        now = time.time()
        _store_packet(handler, packet_hash=ph16, timestamp=now - 10, original_path=["71"])
        _store_packet(
            handler, packet_hash=ph16, timestamp=now - 5, original_path=["71"], is_duplicate=True
        )

        result = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id)
        data = result["data"]
        assert data["observation_count"] == 2
        assert data["unique_path_count"] == 1

    def test_outside_window_excluded(self, endpoints, handler):
        msg_id = _push_message(handler)
        ph16 = "ABCDEF0123456789"
        now = time.time()
        _store_packet(handler, packet_hash=ph16, timestamp=now - 2 * 3600)

        result = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id, window="30m")
        assert result["data"]["receptions"] == []

    def test_no_packet_hash_returns_empty(self, endpoints, handler):
        msg_id = _push_message(handler, packet_hash=None)
        result = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id)
        data = result["data"]
        assert data["packet_hash"] is None
        assert data["receptions"] == []
        assert data["observation_count"] == 0
        assert data["truncated"] is False

    def test_storage_failure_is_503_not_not_found(self, endpoints, handler, monkeypatch):
        def unavailable(*_args, **_kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_message_get_by_id_strict",
            unavailable,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.receptions, companion_name=_NAME, message_id=1)
        assert exc.value.status == 503

    def test_truncated_flag_makes_returned_counts_explicit(self, endpoints, handler, monkeypatch):
        msg_id = _push_message(handler)

        def bounded(*_args, **_kwargs):
            return (
                [
                    {
                        "timestamp": time.time(),
                        "rssi": -80,
                        "snr": 5.0,
                        "original_path": ["71"],
                        "is_duplicate": False,
                        "transmitted": False,
                    }
                ],
                True,
            )

        monkeypatch.setattr(handler, "packets_receptions_strict", bounded)
        data = _call(
            endpoints.receptions,
            companion_name=_NAME,
            message_id=msg_id,
        )["data"]
        assert data["observation_count"] == 1
        assert data["truncated"] is True

    def test_unknown_message_id_404(self, endpoints, handler):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.receptions, companion_name=_NAME, message_id=999999)
        assert exc.value.status == 404

    def test_invalid_message_id_400(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.receptions, companion_name=_NAME, message_id="not-an-int")
        assert exc.value.status == 400

    def test_message_id_is_bounded_to_sqlite_integer_range(self, endpoints):
        maximum = (1 << 63) - 1
        with pytest.raises(cherrypy.HTTPError) as exact_max:
            _call(
                endpoints.receptions,
                companion_name=_NAME,
                message_id=str(maximum),
            )
        assert exact_max.value.status == 404

        with pytest.raises(cherrypy.HTTPError) as overflow:
            _call(
                endpoints.receptions,
                companion_name=_NAME,
                message_id=str(maximum + 1),
            )
        assert overflow.value.status == 400

    def test_message_from_other_companion_404(self, endpoints, handler):
        msg_id = _push_message(handler, companion_hash=_OTHER_HASH)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id)
        assert exc.value.status == 404

    def test_observations_pruned_true_past_retention(self, endpoints, handler):
        msg_id = _push_message(handler)
        result = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id, window="7d")
        # default retention 31d > 7d window, so not pruned
        assert result["data"]["observations_pruned"] is False

        endpoints.config = {"storage": {"retention": {"sqlite_cleanup_days": 1}}}
        result2 = _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id, window="7d")
        assert result2["data"]["observations_pruned"] is True

    def test_scope_404_folding(self, endpoints, handler):
        msg_id = _push_message(handler)
        _set_user(scope=f"companion:{_OTHER_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.receptions, companion_name=_NAME, message_id=msg_id)
        assert exc.value.status == 404


# --- GET .../contacts/{pubkey}/paths --------------------------------------------


class TestContactPaths:
    def test_happy_path_aggregation(self, endpoints, handler):
        now = time.time()
        _push_message(handler, packet_hash="ABCDEF0123456789" + "00" * 24, timestamp=int(now))
        _store_packet(
            handler,
            packet_hash="ABCDEF0123456789",
            timestamp=now - 100,
            original_path=["71"],
            rssi=-70,
            snr=5.0,
        )
        _store_packet(
            handler,
            packet_hash="ABCDEF0123456789",
            timestamp=now - 50,
            original_path=["71"],
            rssi=-80,
            snr=3.0,
            is_duplicate=True,
        )
        _store_packet(
            handler,
            packet_hash="ABCDEF0123456789",
            timestamp=now - 25,
            original_path=["99", "71"],
            rssi=-60,
            snr=8.0,
            is_duplicate=True,
        )

        result = _call(endpoints.paths, companion_name=_NAME, contact_pubkey=_SENDER_PUBKEY_HEX)
        data = result["data"]
        assert data["contact_pubkey"] == _SENDER_PUBKEY_HEX
        assert data["total_observations"] == 3
        assert len(data["paths"]) == 2
        top = data["paths"][0]
        assert top["count"] == 2
        assert top["rssi_min"] == -80
        assert top["rssi_max"] == -70
        assert top["rssi_avg"] == pytest.approx(-75.0)
        assert top["first_hop"]["raw_hash"] == "71"
        assert top["last_hop"]["raw_hash"] == "71"
        assert data["truncated"] is False

    def test_contact_need_not_be_saved(self, endpoints, handler):
        # No companion_contacts row for this pubkey at all -- resolution
        # comes solely from companion_messages.sender_key.
        now = time.time()
        _push_message(handler, packet_hash="ABCDEF0123456789" + "00" * 24, timestamp=int(now))
        _store_packet(handler, packet_hash="ABCDEF0123456789", timestamp=now - 10)

        result = _call(endpoints.paths, companion_name=_NAME, contact_pubkey=_SENDER_PUBKEY_HEX)
        assert result["data"]["total_observations"] == 1

    def test_invalid_pubkey_400(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.paths, companion_name=_NAME, contact_pubkey="not-hex")
        assert exc.value.status == 400

    def test_no_messages_empty_result(self, endpoints, handler):
        result = _call(endpoints.paths, companion_name=_NAME, contact_pubkey=_SENDER_PUBKEY_HEX)
        assert result["data"]["paths"] == []
        assert result["data"]["total_observations"] == 0

    def test_message_limit_reported(self, endpoints, handler):
        result = _call(endpoints.paths, companion_name=_NAME, contact_pubkey=_SENDER_PUBKEY_HEX)
        assert result["data"]["message_limit"] == 200
        assert result["data"]["observation_limit"] == 500

    def test_sender_query_failure_is_503(self, endpoints, handler, monkeypatch):
        def unavailable(*_args, **_kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_messages_by_sender_strict",
            unavailable,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(
                endpoints.paths,
                companion_name=_NAME,
                contact_pubkey=_SENDER_PUBKEY_HEX,
            )
        assert exc.value.status == 503

    def test_sender_limit_propagates_truncated(self, endpoints, handler, monkeypatch):
        monkeypatch.setattr(
            handler,
            "companion_messages_by_sender_strict",
            lambda *_args, **_kwargs: ([], True),
        )
        data = _call(
            endpoints.paths,
            companion_name=_NAME,
            contact_pubkey=_SENDER_PUBKEY_HEX,
        )["data"]
        assert data["total_observations"] == 0
        assert data["truncated"] is True

    def test_scope_404_folding(self, endpoints):
        _set_user(scope=f"companion:{_OTHER_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.paths, companion_name=_NAME, contact_pubkey=_SENDER_PUBKEY_HEX)
        assert exc.value.status == 404


# --- GET .../transmissions/{packet_hash}/repeats --------------------------------


class TestTransmissionRepeats:
    def test_happy_path(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 60, transmitted=True)
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 30,
            is_duplicate=True,
            transmitted=False,
            original_path=["71"],
        )
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 20,
            is_duplicate=True,
            transmitted=False,
            original_path=["99"],
        )

        result = _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        data = result["data"]
        assert data["packet_hash"] == ph16
        assert data["heard_repeat_count"] == 2
        assert data["unique_repeater_count"] == 2
        assert data["repeats"][0]["terminal_repeater"]["raw_hash"] == "71"
        assert data["truncated"] is False

    def test_same_terminal_twice_unique_count_one(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 60, transmitted=True)
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 30,
            is_duplicate=True,
            original_path=["71"],
        )
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 20,
            is_duplicate=True,
            original_path=["71"],
        )

        result = _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        data = result["data"]
        assert data["heard_repeat_count"] == 2
        assert data["unique_repeater_count"] == 1

    def test_reception_before_tx_excluded(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        # A duplicate reception BEFORE our own transmission must not count
        # as a heard repeat (design doc §10.3 local-echo exclusion).
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 100,
            is_duplicate=True,
            original_path=["71"],
        )
        _store_packet(handler, packet_hash=ph16, timestamp=now - 60, transmitted=True)

        result = _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        assert result["data"]["heard_repeat_count"] == 0

    def test_transmitted_duplicate_row_excluded(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 60, transmitted=True)
        # transmitted=1 rows never count as a heard repeat even if flagged
        # is_duplicate somehow.
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 30,
            is_duplicate=True,
            transmitted=True,
            original_path=["71"],
        )

        result = _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        assert result["data"]["heard_repeat_count"] == 0

    def test_accepts_full_hash_truncates(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        full_hash = ph16 + "00" * 24
        _store_packet(handler, packet_hash=ph16, timestamp=now - 10, transmitted=True)

        result = _call(endpoints.repeats, companion_name=_NAME, packet_hash=full_hash)
        assert result["data"]["packet_hash"] == ph16

    @pytest.mark.parametrize("query_full_hash", [False, True])
    def test_frame_full_hash_is_owned_by_its_canonical_prefix(
        self,
        endpoints,
        handler,
        query_full_hash,
    ):
        now = time.time()
        ph16 = "89ABCDEF01234567"
        full_hash = ph16.lower() + ("ab" * 24)
        handler.companion_store_outbound_message(
            _HASH,
            {
                "timestamp": int(now - 20),
                "text": "frame send",
                "is_channel": True,
                "channel_idx": None,
                "packet_hash": full_hash,
            },
            "frame",
            "transmitted",
        )
        _store_packet(
            handler,
            packet_hash=ph16,
            timestamp=now - 20,
            transmitted=True,
        )

        result = _call(
            endpoints.repeats,
            companion_name=_NAME,
            packet_hash=full_hash if query_full_hash else ph16,
        )

        assert result["data"]["packet_hash"] == ph16

    def test_unknown_hash_404(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.repeats, companion_name=_NAME, packet_hash="ffffffffffffffff")
        assert exc.value.status == 404

    def test_transmission_storage_failure_is_503(self, endpoints, handler, monkeypatch):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 10, transmitted=True)

        def unavailable(*_args, **_kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(handler, "packets_transmissions_strict", unavailable)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        assert exc.value.status == 503

    def test_repeat_limit_propagates_truncated(self, endpoints, handler, monkeypatch):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 10, transmitted=True)
        monkeypatch.setattr(
            handler,
            "packets_heard_repeats_strict",
            lambda *_args, **_kwargs: (
                [
                    {
                        "timestamp": now,
                        "rssi": -80,
                        "snr": 5.0,
                        "original_path": ["71"],
                    }
                ],
                True,
            ),
        )
        data = _call(
            endpoints.repeats,
            companion_name=_NAME,
            packet_hash=ph16,
        )["data"]
        assert data["heard_repeat_count"] == 1
        assert data["truncated"] is True

    def test_invalid_hash_400(self, endpoints):
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.repeats, companion_name=_NAME, packet_hash="not-hex-zzzz")
        assert exc.value.status == 400

    def test_scope_404_folding(self, endpoints, handler):
        now = time.time()
        ph16 = "1122334455667788"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 10, transmitted=True)
        _set_user(scope=f"companion:{_OTHER_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(endpoints.repeats, companion_name=_NAME, packet_hash=ph16)
        assert exc.value.status == 404


class TestStrictStorageBounds:
    def test_rf_queries_fetch_one_extra_row_to_report_truncation(self, handler):
        now = time.time()
        ph16 = "A1B2C3D4E5F60718"
        _store_packet(handler, packet_hash=ph16, timestamp=now - 1000, transmitted=True)
        for offset in range(501):
            _store_packet(
                handler,
                packet_hash=ph16,
                timestamp=now - 900 + offset,
                is_duplicate=True,
                transmitted=False,
            )

        receptions, receptions_truncated = handler.packets_receptions_strict(
            ph16,
            now - 2000,
            now,
            limit=500,
        )
        repeats, repeats_truncated = handler.packets_heard_repeats_strict(
            ph16,
            now - 1000,
            now,
            limit=500,
        )

        assert len(receptions) == 500
        assert receptions_truncated is True
        assert len(repeats) == 500
        assert repeats_truncated is True


# --- _cp_dispatch routing for the three new URL shapes --------------------------


class TestDispatchRouting:
    def test_receptions_route(self, endpoints):
        vpath = [_NAME, "messages", "42", "receptions"]
        handler_fn = endpoints._cp_dispatch(vpath)
        assert handler_fn == endpoints.receptions
        assert cherrypy.request.params["companion_name"] == _NAME
        assert cherrypy.request.params["message_id"] == "42"

    def test_paths_route(self, endpoints):
        vpath = [_NAME, "contacts", _SENDER_PUBKEY_HEX, "paths"]
        handler_fn = endpoints._cp_dispatch(vpath)
        assert handler_fn == endpoints.paths
        assert cherrypy.request.params["contact_pubkey"] == _SENDER_PUBKEY_HEX

    def test_repeats_route(self, endpoints):
        vpath = [_NAME, "transmissions", "1122334455667788", "repeats"]
        handler_fn = endpoints._cp_dispatch(vpath)
        assert handler_fn == endpoints.repeats
        assert cherrypy.request.params["packet_hash"] == "1122334455667788"

    def test_unknown_transmission_action_falls_through(self, endpoints):
        vpath = [_NAME, "transmissions", "1122334455667788", "not_a_real_action"]
        assert endpoints._cp_dispatch(vpath) is None
