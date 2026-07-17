"""Tests for the Mobile Companion API v1 auth surface (phase 2): token
scopes, QR pairing, server_info, and the device registry. See
docs/architecture/mobile-companion-api.md §11.

Handlers decorated with @require_auth are invoked through ``__wrapped__``
(same pattern as tests/test_mobile_endpoints.py); PairV1.index and
MobileAPIEndpoints.server_info carry no @require_auth decorator (the
pairing code is itself the credential; server_info is intentionally public)
so those are called directly. Storage is a real SQLiteHandler on tmp_path;
the token manager is a real APITokenManager over the same handler, wired
into cherrypy.config the way http_server.py wires it in production.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.auth.api_tokens import APITokenManager
from repeater.web.mobile_endpoints import CompanionsV1, DevicesV1, MobileAPIEndpoints, PairV1

_HASH_BYTE = 0x01
_HASH = "0x01"
_OTHER_HASH_BYTE = 0x02
_OTHER_HASH = "0x02"
_NAME = "comp-test"
_OTHER_NAME = "comp-other"


# --- Fixtures ---------------------------------------------------------------


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
        self.prefs = SimpleNamespace(node_name=f"node-{hash_byte:02x}")

    def get_public_key(self):
        return bytes([self._hash_byte]) + b"\x22" * 31


_HASH_BYTES_BY_NAME = {_NAME: _HASH_BYTE, _OTHER_NAME: _OTHER_HASH_BYTE}


def _daemon(handler, names=(_NAME, _OTHER_NAME)):
    identities = [(name, _FakeIdentity(_HASH_BYTES_BY_NAME[name]), {}) for name in names]
    identity_manager = SimpleNamespace(
        get_identities_by_type=lambda t: identities if t == "companion" else []
    )
    bridges = {
        _HASH_BYTES_BY_NAME[name]: _FakeBridge(_HASH_BYTES_BY_NAME[name]) for name in names
    }
    return SimpleNamespace(
        identity_manager=identity_manager,
        companion_bridges=bridges,
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=handler)),
    )


@pytest.fixture
def daemon(handler):
    return _daemon(handler)


@pytest.fixture
def companions(daemon):
    return CompanionsV1(daemon_instance=daemon, config={})


@pytest.fixture
def pair(daemon):
    return PairV1(daemon_instance=daemon, config={})


@pytest.fixture
def devices(daemon):
    return DevicesV1(daemon_instance=daemon, config={})


@pytest.fixture
def token_manager(handler):
    return APITokenManager(handler, "test-secret")


@pytest.fixture(autouse=True)
def token_manager_config(token_manager):
    """Wire token_manager into cherrypy.config the way http_server.py does
    (middleware.py and PairV1.index both read it from there via
    cherrypy.config.get). Restore whatever was there before so other test
    files sharing the same process aren't affected by this one."""
    previous = cherrypy.config.get("token_manager")
    cherrypy.config["token_manager"] = token_manager
    yield
    if previous is None:
        cherrypy.config.pop("token_manager", None)
    else:
        cherrypy.config["token_manager"] = previous


@pytest.fixture(autouse=True)
def request_context():
    """Minimal CherryPy request/response state for direct handler calls.

    Unlike tests/test_mobile_endpoints.py, ``request.user`` starts unset
    (deleted, not just omitted) each test: this file is specifically about
    scope enforcement, so tests set it explicitly via ``_set_user``.
    """
    cherrypy.serving.request.method = "GET"
    cherrypy.serving.request.headers = {}
    cherrypy.serving.request.params = {}
    if hasattr(cherrypy.serving.request, "user"):
        del cherrypy.serving.request.user
    cherrypy.serving.response.headers = {}
    cherrypy.serving.response.status = None
    yield
    cherrypy.serving.response.status = None
    if hasattr(cherrypy.serving.request, "user"):
        del cherrypy.serving.request.user


def _call(bound_method, **kwargs):
    """Invoke an endpoint bypassing require_auth (via functools.wraps chain)."""
    return bound_method.__wrapped__(bound_method.__self__, **kwargs)


def _post(target, body):
    cherrypy.serving.request.method = "POST"
    cherrypy.serving.request.headers = {}
    target._get_json_body = lambda: body


def _set_user(scope=None, **extra):
    user = dict(extra)
    if scope is not None:
        user["scope"] = scope
    cherrypy.serving.request.user = user


# --- server_info --------------------------------------------------------


class TestServerInfo:
    def test_shape_no_companion_names_no_auth_required(self, daemon):
        root = MobileAPIEndpoints(
            daemon_instance=daemon, config={"web": {"site_name": "Ridge Site"}}
        )
        # No request.user is set at all (deleted by the fixture) -- this
        # must still succeed, unlike every companion-scoped endpoint.
        result = root.server_info()
        assert result["success"] is True
        data = result["data"]
        assert data["site_name"] == "Ridge Site"
        assert data["api_versions"] == ["v1"]
        assert set(data["auth_modes"]) == {"jwt", "api_token"}
        assert "version" in data["server"]
        assert isinstance(data["server"]["time"], float)
        assert _NAME not in str(data)
        assert _OTHER_NAME not in str(data)

    def test_wrong_method_405(self, daemon):
        root = MobileAPIEndpoints(daemon_instance=daemon)
        cherrypy.serving.request.method = "POST"
        with pytest.raises(cherrypy.HTTPError) as exc:
            root.server_info()
        assert exc.value.status == 405


# --- pair/start (admin-only) ---------------------------------------------


class TestPairStart:
    def test_missing_user_403(self, pair):
        _post(pair, {"companion_name": _NAME})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 403

    def test_companion_scoped_token_403(self, pair):
        _set_user(scope=f"companion:{_NAME}")
        _post(pair, {"companion_name": _NAME})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 403

    def test_admin_generates_code(self, pair):
        _set_user(scope="admin")
        _post(pair, {"companion_name": _NAME})
        result = _call(pair.start)
        assert result["success"] is True
        data = result["data"]
        assert len(data["code"]) == 32  # secrets.token_hex(16) -> 32 hex chars
        assert data["expires_in"] == 300
        assert data["companion_name"] == _NAME
        expected_fp = hashlib.sha256(bytes([_HASH_BYTE]) + b"\x22" * 31).hexdigest()
        assert data["fingerprint"] == expected_fp

    def test_legacy_scope_less_user_dict_passes_as_admin(self, pair):
        _set_user(username="adam")  # no 'scope' key at all
        _post(pair, {"companion_name": _NAME})
        result = _call(pair.start)
        assert result["success"] is True

    def test_unknown_companion_404(self, pair):
        _set_user(scope="admin")
        _post(pair, {"companion_name": "nope"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 404

    def test_get_method_405(self, pair):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 405


# --- pair (exchange code for token) ---------------------------------------


class TestPairExchange:
    def _start_code(self, pair, name=_NAME):
        _set_user(scope="admin")
        _post(pair, {"companion_name": name})
        result = _call(pair.start)
        # Clear the admin identity used to mint the code -- the exchange
        # endpoint itself is unauthenticated, and the next _post() call
        # will overwrite headers/method anyway, but be explicit.
        return result["data"]["code"]

    def test_happy_path_token_works_device_row_and_scope(self, pair, handler, token_manager):
        code = self._start_code(pair)

        _post(pair, {"code": code, "device_id": "dev-1", "name": "Phone", "platform": "ios"})
        result = pair.index()
        assert result["success"] is True
        data = result["data"]
        assert data["device_id"] == "dev-1"
        assert data["companion_name"] == _NAME
        assert data["scope"] == f"companion:{_NAME}"

        verified = token_manager.verify_token(data["token"])
        assert verified is not None
        assert verified["scope"] == f"companion:{_NAME}"

        device = handler.companion_device_get("dev-1")
        assert device is not None
        assert device["name"] == "Phone"
        assert device["platform"] == "ios"
        assert device["companion_hash"] == _HASH

    def test_code_single_use_second_pair_404(self, pair):
        code = self._start_code(pair)
        _post(pair, {"code": code, "device_id": "dev-2", "name": "Phone2"})
        pair.index()

        _post(pair, {"code": code, "device_id": "dev-3", "name": "Phone3"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404

    def test_ttl_expiry_404(self, pair, monkeypatch):
        code = self._start_code(pair)
        future = time.time() + pair._TTL_SEC + 1
        monkeypatch.setattr(time, "time", lambda: future)

        _post(pair, {"code": code, "device_id": "dev-4", "name": "Phone4"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404
        assert code not in pair._codes  # swept as a side effect

    def test_unknown_code_404(self, pair):
        _post(pair, {"code": "does-not-exist", "device_id": "dev-5", "name": "Phone5"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404

    def test_unknown_and_expired_share_error_message(self, pair, monkeypatch):
        code = self._start_code(pair)
        future = time.time() + pair._TTL_SEC + 1
        monkeypatch.setattr(time, "time", lambda: future)
        _post(pair, {"code": code, "device_id": "dev-x", "name": "X"})
        with pytest.raises(cherrypy.HTTPError) as expired_exc:
            pair.index()

        _post(pair, {"code": "totally-unknown", "device_id": "dev-y", "name": "Y"})
        with pytest.raises(cherrypy.HTTPError) as unknown_exc:
            pair.index()

        assert expired_exc.value._message == unknown_exc.value._message

    def test_duplicate_device_id_409_and_token_cleaned_up(self, pair, handler):
        existing_token_id = handler.create_api_token(
            "existing", "existing-hash", scope=f"companion:{_NAME}"
        )
        assert (
            handler.companion_device_create(_HASH, "dupe-device", "Existing", existing_token_id)
            is not None
        )

        code = self._start_code(pair)
        _post(pair, {"code": code, "device_id": "dupe-device", "name": "New"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 409

        tokens = handler.list_api_tokens()
        # Only the pre-existing token remains; the one minted for the
        # failed pairing attempt was cleaned up.
        assert [t["id"] for t in tokens] == [existing_token_id]

    def test_missing_required_fields_400(self, pair):
        for body in ({}, {"code": "x"}, {"code": "x", "device_id": "y"}):
            _post(pair, body)
            with pytest.raises(cherrypy.HTTPError) as exc:
                pair.index()
            assert exc.value.status == 400

    def test_get_method_405(self, pair):
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 405


# --- Rate limiting ----------------------------------------------------------


class TestPairRateLimit:
    def test_429_after_max_attempts_in_window(self, pair):
        for i in range(pair._RATE_LIMIT_MAX):
            _post(pair, {"code": "bogus", "device_id": f"d{i}", "name": "x"})
            with pytest.raises(cherrypy.HTTPError) as exc:
                pair.index()
            assert exc.value.status == 404  # counted, but code lookup still fails first

        _post(pair, {"code": "bogus", "device_id": "d-last", "name": "x"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 429

    def test_rate_limit_resets_after_window(self, pair, monkeypatch):
        base = time.time()
        monkeypatch.setattr(time, "time", lambda: base)
        for i in range(pair._RATE_LIMIT_MAX):
            _post(pair, {"code": "bogus", "device_id": f"d{i}", "name": "x"})
            with pytest.raises(cherrypy.HTTPError):
                pair.index()

        monkeypatch.setattr(time, "time", lambda: base + pair._RATE_LIMIT_WINDOW_SEC + 1)
        _post(pair, {"code": "bogus", "device_id": "d-after-window", "name": "x"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404  # rate limit cleared, back to normal code-lookup failure


# --- Scope enforcement on CompanionsV1._resolve -----------------------------


class TestScopeEnforcement:
    def test_companion_scope_accesses_own_companion(self, companions):
        _set_user(scope=f"companion:{_NAME}")
        _bridge, chash = companions._resolve(_NAME)
        assert chash == _HASH

    def test_companion_scope_403_on_other_companion(self, companions):
        _set_user(scope=f"companion:{_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_OTHER_NAME)
        assert exc.value.status == 403

    def test_wildcard_scope_passes_all_companions(self, companions):
        _set_user(scope="companion:*")
        companions._resolve(_NAME)
        companions._resolve(_OTHER_NAME)

    def test_admin_scope_passes_all_companions(self, companions):
        _set_user(scope="admin")
        companions._resolve(_NAME)
        companions._resolve(_OTHER_NAME)

    def test_legacy_scope_less_user_dict_passes_as_admin(self, companions):
        _set_user(username="adam", auth_type="jwt")  # no 'scope' key
        companions._resolve(_NAME)
        companions._resolve(_OTHER_NAME)

    def test_missing_user_403(self, companions):
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 403

    def test_unrelated_scope_403(self, companions):
        _set_user(scope="something-else")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 403

    def test_unknown_companion_still_404_regardless_of_scope(self, companions):
        _set_user(scope=f"companion:{_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve("does-not-exist")
        assert exc.value.status == 404

    def test_listing_filtered_to_own_companion(self, companions):
        # GET /api/v1/companions: a companion:{name} token must not
        # enumerate the other identities' names/pubkeys (§11.1 — the scope
        # grants the API for ONE companion identity).
        _set_user(scope=f"companion:{_NAME}")
        items = companions.index.__wrapped__(companions)["data"]
        assert [i["name"] for i in items] == [_NAME]

    def test_listing_unfiltered_for_admin_and_wildcard(self, companions):
        for scope in ("admin", "companion:*"):
            _set_user(scope=scope)
            items = companions.index.__wrapped__(companions)["data"]
            assert sorted(i["name"] for i in items) == sorted([_NAME, _OTHER_NAME])


# --- Device registry ---------------------------------------------------------


class TestDevicesList:
    def test_requires_admin_scope(self, devices, handler):
        token_id = handler.create_api_token("phone", "hash-y", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-b", "Phone B", token_id)

        _set_user(scope=f"companion:{_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.index)
        assert exc.value.status == 403

    def test_last_seen_derived_from_token_last_used(self, devices, handler):
        _set_user(scope="admin")
        token_id = handler.create_api_token("phone", "hash-x", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-a", "Phone A", token_id, "ios")
        assert handler.verify_api_token("hash-x") is not None  # bumps last_used

        result = _call(devices.index)
        assert result["success"] is True
        item = next(d for d in result["data"] if d["device_id"] == "dev-a")
        token_last_used = handler.list_api_tokens()[0]["last_used"]
        assert item["last_seen"] == token_last_used

    def test_own_last_seen_kept_when_newer_than_token(self, devices, handler):
        _set_user(scope="admin")
        token_id = handler.create_api_token("phone", "hash-w", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-fresh", "Fresh", token_id)
        far_future = time.time() + 10_000
        handler.companion_device_touch("dev-fresh", last_seen=far_future)

        result = _call(devices.index)
        item = next(d for d in result["data"] if d["device_id"] == "dev-fresh")
        assert item["last_seen"] == far_future

    def test_wrong_method_405(self, devices):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "POST"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.index)
        assert exc.value.status == 405


class TestDevicesRevoke:
    def test_revoke_deletes_token_and_device(self, devices, handler):
        _set_user(scope="admin")
        token_id = handler.create_api_token("phone", "hash-z", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-c", "Phone C", token_id)

        cherrypy.serving.request.method = "DELETE"
        result = _call(devices.delete, device_id="dev-c")
        assert result["success"] is True
        assert handler.companion_device_get("dev-c") is None
        assert handler.verify_api_token("hash-z") is None

    def test_revoke_unknown_device_404(self, devices):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "DELETE"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="does-not-exist")
        assert exc.value.status == 404

    def test_revoke_requires_admin_scope(self, devices, handler):
        token_id = handler.create_api_token("phone", "hash-q", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-q", "Phone Q", token_id)

        _set_user(scope=f"companion:{_NAME}")
        cherrypy.serving.request.method = "DELETE"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="dev-q")
        assert exc.value.status == 403
        # Nothing was actually revoked.
        assert handler.companion_device_get("dev-q") is not None

    def test_wrong_method_405(self, devices):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="whatever")
        assert exc.value.status == 405


class TestDevicesDispatch:
    def test_dispatch_routes_single_segment_to_delete(self, devices):
        vpath = ["dev-x"]
        handler_fn = devices._cp_dispatch(vpath)
        assert handler_fn == devices.delete
        assert cherrypy.request.params["device_id"] == "dev-x"

    def test_dispatch_falls_through_for_other_lengths(self, devices):
        assert devices._cp_dispatch([]) is None
        assert devices._cp_dispatch(["a", "b"]) is None
