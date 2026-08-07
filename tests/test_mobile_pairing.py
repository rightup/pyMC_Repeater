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

import asyncio
import hashlib
import io
import json
import time
from types import SimpleNamespace

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import CompanionStorageError, SQLiteHandler
from repeater.web.auth.api_tokens import APITokenManager
from repeater.web.mobile_endpoints import CompanionsV1, DevicesV1, MobileAPIEndpoints, PairV1

_HASH_BYTE = 0x01
_HASH = "0x01"
_OTHER_HASH_BYTE = 0x02
_OTHER_HASH = "0x02"
_NAME = "comp-test"
_OTHER_NAME = "comp-other"
_IDENTITY = (bytes([_HASH_BYTE]) + b"\x22" * 31).hex()
_OTHER_IDENTITY = (bytes([_OTHER_HASH_BYTE]) + b"\x22" * 31).hex()


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
        self.channels = SimpleNamespace(max_channels=5)

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
def companions(daemon):
    endpoint = CompanionsV1(daemon_instance=daemon, config={})
    endpoint._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
    return endpoint


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
        expected_identity = (bytes([_HASH_BYTE]) + b"\x22" * 31).hex()
        expected_fp = hashlib.sha256(bytes([_HASH_BYTE]) + b"\x22" * 31).hexdigest()
        assert data["companion_identity"] == expected_identity
        assert data["fingerprint"] == expected_fp

    def test_scope_less_user_dict_fails_closed(self, pair):
        _set_user(username="adam")  # no 'scope' key at all
        _post(pair, {"companion_name": _NAME})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 403

    def test_unknown_companion_404(self, pair):
        _set_user(scope="admin")
        _post(pair, {"companion_name": "nope"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 404

    @pytest.mark.parametrize(
        "companion_name",
        (
            "",
            " comp-test",
            "comp-test ",
            "two words",
            "../comp-test",
            "comp/test",
            "comp\ntest",
            "x" * 65,
        ),
    )
    def test_registration_name_contract_is_validated_before_lookup(
        self,
        pair,
        companion_name,
    ):
        _set_user(scope="admin")
        _post(pair, {"companion_name": companion_name})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)

        assert exc.value.status == 400

    def test_get_method_405(self, pair):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)
        assert exc.value.status == 405

    def test_active_pairing_codes_are_bounded(self, pair):
        pair._MAX_ACTIVE_CODES = 2
        _set_user(scope="admin")
        for _ in range(2):
            _post(pair, {"companion_name": _NAME})
            _call(pair.start)

        _post(pair, {"companion_name": _NAME})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)

        assert exc.value.status == 429
        assert int(cherrypy.response.headers["Retry-After"]) >= 1

    def test_pairing_code_collision_never_overwrites_an_active_code(
        self,
        pair,
        monkeypatch,
    ):
        existing_code = "11" * 16
        replacement_code = "22" * 16
        existing = {
            "companion_name": "other",
            "companion_hash": "0xff",
            "companion_identity": "ff" * 32,
            "issued_at": time.monotonic(),
        }
        pair._codes[existing_code] = existing
        generated = iter((existing_code, replacement_code))
        monkeypatch.setattr(
            "repeater.web.mobile_endpoints.secrets.token_hex",
            lambda _size: next(generated),
        )

        _set_user(scope="admin")
        _post(pair, {"companion_name": _NAME})
        result = _call(pair.start)

        assert result["data"]["code"] == replacement_code
        assert pair._codes[existing_code] is existing
        assert pair._codes[replacement_code]["companion_name"] == _NAME

    def test_pairing_code_generation_fails_closed_after_repeated_collision(
        self,
        pair,
        monkeypatch,
    ):
        existing_code = "33" * 16
        existing = {
            "companion_name": "other",
            "companion_hash": "0xff",
            "companion_identity": "ff" * 32,
            "issued_at": time.monotonic(),
        }
        pair._codes[existing_code] = existing
        monkeypatch.setattr(
            "repeater.web.mobile_endpoints.secrets.token_hex",
            lambda _size: existing_code,
        )

        _set_user(scope="admin")
        _post(pair, {"companion_name": _NAME})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(pair.start)

        assert exc.value.status == 503
        assert pair._codes == {existing_code: existing}


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

    def test_cross_origin_simple_content_type_is_rejected_without_rate_cost(
        self,
        pair,
    ):
        cherrypy.serving.request.method = "POST"
        cherrypy.serving.request.headers = {"Content-Type": "text/plain"}
        cherrypy.serving.request.body = io.BytesIO(
            json.dumps(
                {
                    "code": "00" * 16,
                    "device_id": "browser",
                    "name": "Browser",
                }
            ).encode()
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()

        assert exc.value.status == 415
        assert pair._attempts._buckets == {}

    def test_happy_path_token_works_device_row_and_scope(self, pair, handler, token_manager):
        code = self._start_code(pair)

        _post(pair, {"code": code, "device_id": "dev-1", "name": "Phone", "platform": "ios"})
        result = pair.index()
        assert result["success"] is True
        data = result["data"]
        assert data["device_id"] == "dev-1"
        assert data["companion_name"] == _NAME
        assert data["scope"] == f"companion:{_NAME}"
        expected_identity = (bytes([_HASH_BYTE]) + b"\x22" * 31).hex()
        assert data["companion_identity"] == expected_identity
        assert data["fingerprint"] == hashlib.sha256(bytes.fromhex(expected_identity)).hexdigest()

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

    def test_code_cannot_pair_after_companion_identity_changes(
        self,
        pair,
        handler,
    ):
        code = self._start_code(pair)
        pair.daemon_instance.identity_manager.get_identities_by_type = lambda identity_type: (
            [(_NAME, _FakeIdentity(0x7F), {})] if identity_type == "companion" else []
        )

        _post(
            pair,
            {"code": code, "device_id": "stale-device", "name": "Stale"},
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()

        assert exc.value.status == 404
        assert code not in pair._codes
        assert handler.companion_device_get("stale-device") is None
        assert handler.list_api_tokens() == []

    def test_uppercase_hex_code_is_the_same_credential(self, pair):
        code = self._start_code(pair)
        _post(
            pair,
            {
                "code": code.upper(),
                "device_id": "dev-uppercase",
                "name": "Uppercase",
            },
        )
        assert pair.index()["data"]["device_id"] == "dev-uppercase"

    def test_ttl_expiry_404(self, pair, monkeypatch):
        now = [100.0]
        monkeypatch.setattr(
            "repeater.web.mobile_endpoints.time.monotonic",
            lambda: now[0],
        )
        code = self._start_code(pair)
        now[0] += pair._TTL_SEC + 1

        _post(pair, {"code": code, "device_id": "dev-4", "name": "Phone4"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404
        assert code not in pair._codes  # swept as a side effect

    def test_unknown_code_404(self, pair):
        _post(pair, {"code": "00" * 16, "device_id": "dev-5", "name": "Phone5"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404

    def test_unknown_and_expired_share_error_message(self, pair, monkeypatch):
        now = [100.0]
        monkeypatch.setattr(
            "repeater.web.mobile_endpoints.time.monotonic",
            lambda: now[0],
        )
        code = self._start_code(pair)
        now[0] += pair._TTL_SEC + 1
        _post(pair, {"code": code, "device_id": "dev-x", "name": "X"})
        with pytest.raises(cherrypy.HTTPError) as expired_exc:
            pair.index()

        _post(pair, {"code": "00" * 16, "device_id": "dev-y", "name": "Y"})
        with pytest.raises(cherrypy.HTTPError) as unknown_exc:
            pair.index()

        assert expired_exc.value._message == unknown_exc.value._message

    def test_wall_clock_jump_does_not_expire_pairing_code(self, pair, monkeypatch):
        from repeater.web import mobile_endpoints

        monkeypatch.setattr(
            mobile_endpoints,
            "time",
            SimpleNamespace(
                monotonic=lambda: 100.0,
                time=lambda: 10_000_000_000.0,
            ),
        )
        code = self._start_code(pair)

        _post(
            pair,
            {
                "code": code,
                "device_id": "wall-clock-safe",
                "name": "Phone",
            },
        )

        assert pair.index()["data"]["device_id"] == "wall-clock-safe"

    def test_device_id_is_globally_unique_across_companions(self, pair, handler):
        existing_token_id = handler.create_api_token(
            "existing", "existing-hash", scope=f"companion:{_OTHER_NAME}"
        )
        assert (
            handler.companion_device_create(
                _OTHER_HASH,
                "dupe-device",
                "Existing",
                existing_token_id,
            )
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

    def test_duplicate_check_storage_failure_is_503_and_code_remains_usable(
        self, pair, handler, monkeypatch
    ):
        code = self._start_code(pair)

        def pairing_unavailable(*_args, **_kwargs):
            raise CompanionStorageError("write unavailable")

        def duplicate_check_unavailable(_device_id):
            raise CompanionStorageError("read unavailable")

        monkeypatch.setattr(handler, "companion_pair_device", pairing_unavailable)
        monkeypatch.setattr(
            handler,
            "companion_device_get_strict",
            duplicate_check_unavailable,
        )
        _post(pair, {"code": code, "device_id": "dev-uncertain", "name": "Phone"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 503
        assert code in pair._codes

    def test_missing_required_fields_400(self, pair):
        for body in ({}, {"code": "x"}, {"code": "x", "device_id": "y"}):
            _post(pair, body)
            with pytest.raises(cherrypy.HTTPError) as exc:
                pair.index()
            assert exc.value.status == 400

    @pytest.mark.parametrize(
        "device_id",
        [
            "   ",
            " dev-spaced",
            "dev-spaced ",
            "dev/child",
            "dev\\child",
            "dev\nchild",
            ".",
            "..",
            "x" * 129,
            "\ud800",
        ],
    )
    def test_device_id_contract_is_shared_with_path_routes(self, pair, device_id):
        _post(
            pair,
            {
                "code": "00" * 16,
                "device_id": device_id,
                "name": "Phone",
            },
        )
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
            _post(pair, {"code": "00" * 16, "device_id": f"d{i}", "name": "x"})
            with pytest.raises(cherrypy.HTTPError) as exc:
                pair.index()
            assert exc.value.status == 404  # counted, but code lookup still fails first

        _post(pair, {"code": "00" * 16, "device_id": "d-last", "name": "x"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 429

    def test_rate_limit_resets_after_window(self, pair, monkeypatch):
        base = 1000.0
        monkeypatch.setattr(
            "repeater.web.rate_limit.time.monotonic",
            lambda: base,
        )
        for i in range(pair._RATE_LIMIT_MAX):
            _post(pair, {"code": "00" * 16, "device_id": f"d{i}", "name": "x"})
            with pytest.raises(cherrypy.HTTPError):
                pair.index()

        monkeypatch.setattr(
            "repeater.web.rate_limit.time.monotonic",
            lambda: base + pair._RATE_LIMIT_WINDOW_SEC + 1,
        )
        _post(pair, {"code": "00" * 16, "device_id": "d-after-window", "name": "x"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            pair.index()
        assert exc.value.status == 404  # rate limit cleared, back to normal code-lookup failure


# --- Scope enforcement on CompanionsV1._resolve -----------------------------


class TestScopeEnforcement:
    def test_companion_scope_accesses_own_companion(self, companions):
        _set_user(scope=f"companion:{_NAME}")
        _bridge, chash = companions._resolve(_NAME)
        assert chash == _HASH

    def test_companion_scope_404_on_other_companion(self, companions):
        """Out-of-scope is indistinguishable from nonexistent — same status
        AND message shape — so a scoped token can't enumerate names."""
        _set_user(scope=f"companion:{_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_OTHER_NAME)
        assert exc.value.status == 404
        assert exc.value._message == f"Companion '{_OTHER_NAME}' not found"

    def test_wildcard_scope_passes_all_companions(self, companions):
        _set_user(
            scope="companion:*",
            auth_type="api_token",
            token_id=999_999,
        )
        companions._resolve(_NAME)
        companions._resolve(_OTHER_NAME)

    def test_admin_scope_passes_all_companions(self, companions):
        _set_user(scope="admin")
        companions._resolve(_NAME)
        companions._resolve(_OTHER_NAME)

    def test_scope_less_user_dict_fails_closed(self, companions):
        _set_user(username="adam", auth_type="jwt")  # no 'scope' key
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_missing_user_404(self, companions):
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_unrelated_scope_404(self, companions):
        _set_user(scope="something-else")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_unknown_companion_still_404_regardless_of_scope(self, companions):
        _set_user(scope=f"companion:{_NAME}")
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve("does-not-exist")
        assert exc.value.status == 404

    def test_device_binding_read_failure_is_503_not_name_scope_fallback(
        self, companions, handler, monkeypatch
    ):
        def unavailable(_token_id):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_device_get_by_token_strict",
            unavailable,
        )
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=7,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 503

    def test_exact_api_token_requires_a_paired_device(self, companions, handler):
        token_id = handler.create_api_token(
            "unpaired",
            "unpaired-hash",
            scope=f"companion:{_NAME}",
        )
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=token_id,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_exact_api_token_rejects_legacy_device_without_identity(self, companions, handler):
        token_id = handler.create_api_token(
            "legacy",
            "legacy-device-hash",
            scope=f"companion:{_NAME}",
        )
        handler.companion_device_create(
            _HASH,
            "legacy-device",
            "Legacy",
            token_id,
        )
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=token_id,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_exact_api_token_uses_full_identity_across_a_slug_rename(self, companions, handler):
        token_id = handler.create_api_token(
            "phone",
            "renamed-device-hash",
            scope="companion:old-slug",
        )
        handler.companion_device_create(
            _HASH,
            "renamed-device",
            "Phone",
            token_id,
            companion_identity=_IDENTITY.upper(),
        )
        _set_user(
            scope="companion:old-slug",
            auth_type="api_token",
            token_id=token_id,
        )

        _bridge, companion_hash = companions._resolve(_NAME)
        assert companion_hash == _HASH

    def test_exact_api_token_rejects_a_different_full_identity(self, companions, handler):
        token_id = handler.create_api_token(
            "wrong identity",
            "wrong-identity-hash",
            scope=f"companion:{_NAME}",
        )
        handler.companion_device_create(
            _HASH,
            "wrong-identity-device",
            "Phone",
            token_id,
            companion_identity=_OTHER_IDENTITY,
        )
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=token_id,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 404

    def test_exact_api_token_rejects_ambiguous_device_bindings(self, companions, handler):
        token_id = handler.create_api_token(
            "ambiguous",
            "ambiguous-device-hash",
            scope=f"companion:{_NAME}",
        )
        handler.companion_device_create(
            _HASH,
            "ambiguous-a",
            "Phone A",
            token_id,
            companion_identity=_IDENTITY,
        )
        with handler._connect() as conn:
            conn.execute(
                """
                INSERT INTO companion_devices
                    (companion_hash, companion_identity, device_id, name,
                     token_id, created_at)
                VALUES (?, ?, 'ambiguous-b', 'Phone B', ?, ?)
                """,
                (_HASH, _IDENTITY, token_id, time.time()),
            )
            conn.commit()
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=token_id,
        )

        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._resolve(_NAME)
        assert exc.value.status == 503

    def test_principal_read_failure_is_503_not_token_fallback(
        self, companions, handler, monkeypatch
    ):
        def unavailable(_token_id):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_device_get_by_token_strict",
            unavailable,
        )
        _set_user(
            scope=f"companion:{_NAME}",
            auth_type="api_token",
            token_id=7,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            companions._principal()
        assert exc.value.status == 503

    def test_listing_filtered_to_own_companion(self, companions):
        # GET /api/v1/companions: a companion:{name} token must not
        # enumerate the other identities' names/pubkeys (§11.1 — the scope
        # grants the API for ONE companion identity).
        _set_user(scope=f"companion:{_NAME}")
        items = companions.index.__wrapped__(companions)["data"]
        assert [i["name"] for i in items] == [_NAME]
        assert items[0]["capabilities"] == {"max_channels": 5}

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

    def test_scope_less_user_dict_fails_closed(self, devices):
        _set_user(username="adam")
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
        assert cherrypy.serving.response.headers["Cache-Control"] == "no-store"
        item = next(d for d in result["data"] if d["device_id"] == "dev-a")
        token_last_used = handler.list_api_tokens()[0]["last_used"]
        assert item["last_seen"] == token_last_used
        assert "last_synced_seq" not in item

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

    def test_storage_failure_is_503_not_an_empty_registry(self, devices, handler, monkeypatch):
        _set_user(scope="admin")

        def unavailable():
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(handler, "companion_device_list_strict", unavailable)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.index)
        assert exc.value.status == 503


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

    def test_device_token_can_revoke_its_own_device(self, devices, handler):
        token_id = handler.create_api_token("phone", "hash-q", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-q", "Phone Q", token_id)

        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        cherrypy.serving.request.method = "DELETE"
        result = _call(devices.delete, device_id="dev-q")
        assert result["data"] == {"revoked": True, "device_id": "dev-q"}
        assert handler.companion_device_get("dev-q") is None
        assert handler.verify_api_token("hash-q") is None

    def test_stale_self_revoke_cannot_delete_newly_repaired_device(
        self,
        devices,
        handler,
        monkeypatch,
    ):
        old_token_id = handler.create_api_token(
            "old",
            "hash-old",
            scope=f"companion:{_NAME}",
        )
        handler.companion_device_create(
            _HASH,
            "stable-device",
            "Old",
            old_token_id,
        )
        _set_user(scope=f"companion:{_NAME}", token_id=old_token_id)
        original_check = devices._check_device_or_admin
        replacement = {}

        def check_then_repair(storage, device_id):
            original_check(storage, device_id)
            assert storage.companion_revoke_device(device_id=device_id)["devices_deleted"] == 1
            new_token_id = storage.create_api_token(
                "new",
                "hash-new",
                scope=f"companion:{_NAME}",
            )
            storage.companion_device_create(
                _HASH,
                device_id,
                "New",
                new_token_id,
            )
            replacement["token_id"] = new_token_id

        monkeypatch.setattr(
            devices,
            "_check_device_or_admin",
            check_then_repair,
        )
        cherrypy.serving.request.method = "DELETE"

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="stable-device")

        assert exc.value.status == 404
        current = handler.companion_device_get("stable-device")
        assert current["token_id"] == replacement["token_id"]
        assert handler.verify_api_token("hash-new") is not None

    def test_device_token_cannot_revoke_another_device(self, devices, handler):
        own_token_id = handler.create_api_token("own", "hash-own", scope=f"companion:{_NAME}")
        other_token_id = handler.create_api_token("other", "hash-other", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-own", "Own", own_token_id)
        handler.companion_device_create(_HASH, "dev-other", "Other", other_token_id)

        _set_user(scope=f"companion:{_NAME}", token_id=own_token_id)
        cherrypy.serving.request.method = "DELETE"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="dev-other")
        assert exc.value.status == 404
        assert handler.companion_device_get("dev-other") is not None

    def test_wrong_method_405(self, devices):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="whatever")
        assert exc.value.status == 405

    @pytest.mark.parametrize("device_id", ["", "   ", "x" * 129])
    def test_device_id_path_is_bounded_before_storage(self, devices, device_id):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "DELETE"

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id=device_id)

        assert exc.value.status == 400

    def test_lookup_failure_is_503_not_not_found(self, devices, handler, monkeypatch):
        _set_user(scope="admin")
        cherrypy.serving.request.method = "DELETE"

        def unavailable(**_kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(handler, "companion_revoke_device", unavailable)
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.delete, device_id="dev-unknown")
        assert exc.value.status == 503


class TestDevicesDispatch:
    def test_dispatch_routes_single_segment_to_delete(self, devices):
        vpath = ["dev-x"]
        handler_fn = devices._cp_dispatch(vpath)
        assert handler_fn == devices.delete
        assert cherrypy.request.params["device_id"] == "dev-x"

    def test_dispatch_falls_through_for_other_lengths(self, devices):
        assert devices._cp_dispatch([]) is None
        assert devices._cp_dispatch(["a", "b"]) is None

    def test_dispatch_routes_push_subresource(self, devices):
        vpath = ["dev-x", "push"]
        handler_fn = devices._cp_dispatch(vpath)
        assert handler_fn == devices.push
        assert cherrypy.request.params["device_id"] == "dev-x"


class TestDevicesPush:
    def _make_device(self, handler, device_id="dev-p", token_hash="hash-p"):
        token_id = handler.create_api_token("phone", token_hash, scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, device_id, "Phone P", token_id)
        return token_id

    def test_device_registers_own_push(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(
            devices,
            {
                "push_token": "apns-xyz",
                "push_detail": "count",
            },
        )
        result = _call(devices.push, device_id="dev-p")
        assert result["success"] is True
        assert result["data"]["push_detail"] == "count"
        device = handler.companion_device_get("dev-p")
        assert device["push_token"] == "apns-xyz"
        assert device["push_relay_url"] is None

    def test_stale_self_service_post_cannot_mutate_repaired_device(
        self,
        devices,
        handler,
        monkeypatch,
    ):
        old_token_id = self._make_device(handler, "stable-device", "hash-old")
        _set_user(scope=f"companion:{_NAME}", token_id=old_token_id)
        original_check = devices._check_device_or_admin
        replacement = {}

        def check_then_repair(storage, device_id):
            original_check(storage, device_id)
            storage.companion_revoke_device(device_id=device_id)
            new_token_id = storage.create_api_token(
                "new",
                "hash-new",
                scope=f"companion:{_NAME}",
            )
            storage.companion_device_create(
                _HASH,
                device_id,
                "New",
                new_token_id,
            )
            replacement["token_id"] = new_token_id

        monkeypatch.setattr(
            devices,
            "_check_device_or_admin",
            check_then_repair,
        )
        _post(devices, {"push_token": "stale-write"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="stable-device")

        assert exc.value.status == 404
        current = handler.companion_device_get("stable-device")
        assert current["token_id"] == replacement["token_id"]
        assert current["push_token"] is None

    def test_admin_registers_any_device(self, devices, handler):
        self._make_device(handler)
        _set_user(scope="admin")
        _post(devices, {"push_token": "tok"})
        result = _call(devices.push, device_id="dev-p")
        assert result["success"] is True
        assert handler.companion_device_get("dev-p")["push_token"] == "tok"

    def test_missing_request_user_fails_closed(self, devices, handler):
        self._make_device(handler)
        _post(devices, {"push_token": "must-not-write"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 404
        assert handler.companion_device_get("dev-p")["push_token"] is None

    def test_scope_less_user_dict_cannot_manage_a_device(
        self,
        devices,
        handler,
    ):
        token_id = self._make_device(handler)
        _set_user(token_id=token_id)
        _post(devices, {"push_token": "must-not-write"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 404
        assert handler.companion_device_get("dev-p")["push_token"] is None

    def test_options_is_not_a_write_method(self, devices, handler):
        self._make_device(handler)
        _set_user(scope="admin")
        cherrypy.serving.request.method = "OPTIONS"

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 405
        assert cherrypy.serving.response.headers["Allow"] == "POST, DELETE"
        assert handler.companion_device_get("dev-p")["push_token"] is None

    @pytest.mark.parametrize("device_id", [None, "   ", "x" * 129])
    def test_device_id_path_is_bounded_before_storage(
        self,
        devices,
        handler,
        device_id,
    ):
        _set_user(scope="admin")
        _post(devices, {"push_token": "must-not-write"})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id=device_id)

        assert exc.value.status == 400
        assert handler.companion_device_list() == []

    def test_non_owning_device_token_404(self, devices, handler):
        # dev-p is paired to token_a; the caller authenticates as a DIFFERENT
        # device's token and must not be able to touch dev-p.
        self._make_device(handler, "dev-p", "hash-a")
        other_token = handler.create_api_token("other", "hash-b", scope=f"companion:{_NAME}")
        handler.companion_device_create(_HASH, "dev-other", "Other", other_token)
        _set_user(scope=f"companion:{_NAME}", token_id=other_token)
        _post(devices, {"push_token": "tok"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 404
        assert handler.companion_device_get("dev-p")["push_token"] is None

    def test_device_binding_read_failure_is_503_not_not_found(self, devices, handler, monkeypatch):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": "tok"})

        def unavailable(_token_id):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_device_get_by_token_strict",
            unavailable,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 503

    def test_missing_push_token_400(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_relay_url": "https://r.example"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 400

    @pytest.mark.parametrize("push_token", [" tok", "tok ", "tok\nnext"])
    def test_push_token_rejects_ambiguous_whitespace(
        self,
        devices,
        handler,
        push_token,
    ):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": push_token})

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")

        assert exc.value.status == 400
        assert handler.companion_device_get("dev-p")["push_token"] is None

    def test_invalid_push_detail_400(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": "tok", "push_detail": "everything"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 400

    def test_invalid_relay_url_400(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": "tok", "push_relay_url": "ftp://nope"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 400

    def test_delete_clears_push_token(self, devices, handler):
        token_id = self._make_device(handler)
        handler.companion_device_set_push(
            "dev-p", "tok", push_relay_url="https://r.example", push_detail="count"
        )
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        cherrypy.serving.request.method = "DELETE"
        result = _call(devices.push, device_id="dev-p")
        assert result["data"]["unregistered"] is True
        device = handler.companion_device_get("dev-p")
        assert device["push_token"] is None
        # Legacy device-selected destinations are removed on unregister.
        assert device["push_relay_url"] is None

    def test_stale_self_service_delete_cannot_clear_repaired_device(
        self,
        devices,
        handler,
        monkeypatch,
    ):
        old_token_id = self._make_device(handler, "stable-device", "hash-old")
        _set_user(scope=f"companion:{_NAME}", token_id=old_token_id)
        original_check = devices._check_device_or_admin
        replacement = {}

        def check_then_repair(storage, device_id):
            original_check(storage, device_id)
            storage.companion_revoke_device(device_id=device_id)
            new_token_id = storage.create_api_token(
                "new",
                "hash-new",
                scope=f"companion:{_NAME}",
            )
            storage.companion_device_create(
                _HASH,
                device_id,
                "New",
                new_token_id,
            )
            storage.companion_device_set_push(device_id, "new-push-token")
            replacement["token_id"] = new_token_id

        monkeypatch.setattr(
            devices,
            "_check_device_or_admin",
            check_then_repair,
        )
        cherrypy.serving.request.method = "DELETE"

        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="stable-device")

        assert exc.value.status == 404
        current = handler.companion_device_get("stable-device")
        assert current["token_id"] == replacement["token_id"]
        assert current["push_token"] == "new-push-token"

    def test_push_write_failure_is_503_not_not_found(self, devices, handler, monkeypatch):
        self._make_device(handler)
        _set_user(scope="admin")
        _post(devices, {"push_token": "tok"})

        def unavailable(*_args, **_kwargs):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_device_set_push_strict",
            unavailable,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 503

    def test_push_clear_failure_is_503_not_not_found(self, devices, handler, monkeypatch):
        self._make_device(handler)
        _set_user(scope="admin")
        cherrypy.serving.request.method = "DELETE"

        def unavailable(_device_id):
            raise CompanionStorageError("database unavailable")

        monkeypatch.setattr(
            handler,
            "companion_device_clear_push_strict",
            unavailable,
        )
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 503

    def test_wrong_method_405(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        cherrypy.serving.request.method = "GET"
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 405

    def test_registers_mention_fields(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(
            devices,
            {
                "push_token": "tok",
                "mention_push": True,
                "mention_keywords": ["adam", "@adam"],
            },
        )
        result = _call(devices.push, device_id="dev-p")
        assert result["data"]["mention_push"] is True
        device = handler.companion_device_get("dev-p")
        assert device["mention_push"] is True
        import json as _json

        assert _json.loads(device["mention_keywords"]) == ["adam", "@adam"]

    def test_invalid_mention_push_type_400(self, devices, handler):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": "tok", "mention_push": "yes"})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 400

    @pytest.mark.parametrize("keywords", [[1, 2], ["\ud800"]])
    def test_invalid_mention_keywords_400(self, devices, handler, keywords):
        token_id = self._make_device(handler)
        _set_user(scope=f"companion:{_NAME}", token_id=token_id)
        _post(devices, {"push_token": "tok", "mention_keywords": keywords})
        with pytest.raises(cherrypy.HTTPError) as exc:
            _call(devices.push, device_id="dev-p")
        assert exc.value.status == 400
