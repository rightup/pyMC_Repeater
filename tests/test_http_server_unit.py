import io
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import cherrypy
import pytest
import yaml
from cherrypy.lib import static as cherrypy_static

from repeater.config_manager import ConfigManager
from repeater.web import http_server as hs


def test_bundled_companion_ui_defaults_frame_listener_to_loopback():
    assets = Path(__file__).resolve().parents[1] / "repeater" / "web" / "html" / "assets"
    companion_chunks = list(assets.glob("Companions-*.js"))
    assert companion_chunks, "bundled Companions UI chunk is missing"

    bundle = "\n".join(path.read_text(encoding="utf-8") for path in companion_chunks)
    assert "bind_address:`0.0.0.0`" not in bundle
    assert "bind_address:`127.0.0.1`" in bundle
    assert "placeholder:`127.0.0.1`" in bundle


def test_safe_cors_exposes_observable_response_headers(monkeypatch):
    request = SimpleNamespace(
        method="GET",
        headers={"Origin": "https://chat.example"},
    )
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)

    hs._safe_cors(("https://chat.example",))

    assert response.headers["Access-Control-Expose-Headers"] == (
        "ETag, Idempotency-Replayed, Retry-After"
    )


def test_cors_origins_reject_ambiguous_or_credentialed_authorities(caplog):
    with caplog.at_level(logging.WARNING):
        origins = hs._cors_origins(
            {
                "web": {
                    "cors_origins": [
                        "https://chat.example/",
                        "https://@chat.example",
                        "https://user:pass@chat.example",
                        "https://chat.example:invalid",
                        "https://chat.example:65536",
                        "https://[broken",
                    ]
                }
            }
        )

    assert origins == ("https://chat.example",)
    assert caplog.text.count("Ignoring invalid CORS origin") == 5


def test_api_no_store_defaults_legacy_and_secret_responses(monkeypatch):
    for path in (
        "/api/config_export?include_secrets=true",
        "/api/identity_export",
        "/api/companion/get_contacts",
    ):
        response = SimpleNamespace(headers={})
        monkeypatch.setattr(
            cherrypy,
            "request",
            SimpleNamespace(path_info=path),
            raising=False,
        )
        monkeypatch.setattr(cherrypy, "response", response, raising=False)

        hs._default_api_no_store()

        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "cache_control",
    (
        "private, no-store, no-cache, no-transform",
        "no-store, no-cache, no-transform",
    ),
)
def test_api_no_store_preserves_explicit_v1_cache_policy(monkeypatch, cache_control):
    response = SimpleNamespace(headers={"Cache-Control": cache_control})
    monkeypatch.setattr(cherrypy, "response", response, raising=False)

    hs._default_api_no_store()

    assert response.headers["Cache-Control"] == cache_control


def test_http_server_mounts_no_store_policy_on_entire_api_tree(monkeypatch, tmp_path):
    def _fake_init_auth(self):
        self.jwt_handler = object()
        self.token_manager = object()

    app = SimpleNamespace(
        api=SimpleNamespace(config_manager=object()),
        apply_web_config=lambda: False,
    )
    mounted = {}

    monkeypatch.setattr(hs.HTTPStatsServer, "_init_auth_handlers", _fake_init_auth)
    monkeypatch.setattr(hs, "StatsApp", lambda *args, **kwargs: app)
    monkeypatch.setattr(hs, "AuthEndpoints", lambda *args, **kwargs: object())
    monkeypatch.setattr(hs, "DocEndpoint", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(hs, "WEBSOCKET_AVAILABLE", False)
    monkeypatch.setattr(hs, "_install_cheroot_bad_fd_unraisable_filter", lambda: None)
    monkeypatch.setattr(hs, "register_require_auth_tool", lambda: None)
    monkeypatch.setattr(hs, "_register_api_no_store_tool", lambda: None)
    monkeypatch.setattr(cherrypy.config, "update", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cherrypy.tree,
        "mount",
        lambda _app, path, config: mounted.__setitem__(path, config),
    )
    monkeypatch.setattr(cherrypy.engine, "start", lambda: None)
    monkeypatch.setattr(
        cherrypy.log.access_log,
        "propagate",
        cherrypy.log.access_log.propagate,
    )
    monkeypatch.setattr(cherrypy.log.error_log, "setLevel", lambda _level: None)

    server = hs.HTTPStatsServer(
        config={
            "web": {"cors_enabled": False},
            "http": {},
        },
        config_path=str(tmp_path / "config.yaml"),
    )
    server.start()

    assert mounted["/"]["/api"]["tools.api_no_store.on"] is True
    assert mounted["/"]["/api"]["error_page.default"] == server._json_error_handler


@pytest.mark.parametrize("expiry_minutes", (1, 10_080))
def test_jwt_expiry_accepts_exact_bounds(expiry_minutes):
    assert hs._jwt_expiry_minutes(expiry_minutes) == expiry_minutes


@pytest.mark.parametrize("expiry_minutes", (True, False, "60", 1.0, 0, 10_081))
def test_jwt_expiry_rejects_coercion_and_out_of_range(expiry_minutes):
    with pytest.raises(ValueError, match="jwt_expiry_minutes"):
        hs._jwt_expiry_minutes(expiry_minutes)


def _initialize_auth_only(monkeypatch, config, config_path):
    server = object.__new__(hs.HTTPStatsServer)
    server.config = config
    server.config_path = str(config_path) if config_path is not None else None
    server.daemon_instance = None
    monkeypatch.setattr(hs, "SQLiteHandler", lambda _path: object())
    server._init_auth_handlers()
    return server


def test_auth_reuses_daemon_storage_handler(monkeypatch, tmp_path):
    shared_handler = object()
    daemon = SimpleNamespace(
        repeater_handler=SimpleNamespace(storage=SimpleNamespace(sqlite_handler=shared_handler))
    )
    server = object.__new__(hs.HTTPStatsServer)
    server.config = {
        "repeater": {
            "security": {
                "jwt_secret": "s" * 32,
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }
    server.config_path = str(tmp_path / "config.yaml")
    server.daemon_instance = daemon

    def unexpected_handler(_path):
        raise AssertionError("shared daemon storage must be reused")

    monkeypatch.setattr(hs, "SQLiteHandler", unexpected_handler)
    server._init_auth_handlers()

    assert server.sqlite_handler is shared_handler
    assert server.token_manager.db is shared_handler


def test_auth_constructs_storage_handler_without_daemon(monkeypatch, tmp_path):
    constructed_handler = object()
    constructed_paths = []
    server = object.__new__(hs.HTTPStatsServer)
    server.config = {
        "repeater": {
            "security": {
                "jwt_secret": "s" * 32,
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }
    server.config_path = str(tmp_path / "config.yaml")
    server.daemon_instance = None

    def construct(path):
        constructed_paths.append(path)
        return constructed_handler

    monkeypatch.setattr(hs, "SQLiteHandler", construct)
    server._init_auth_handlers()

    assert server.sqlite_handler is constructed_handler
    assert server.token_manager.db is constructed_handler
    assert constructed_paths == [tmp_path / "storage"]


def test_generated_jwt_secret_survives_later_save_and_restart(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config = {
        "repeater": {
            "security": {
                "jwt_secret": None,
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(hs.secrets, "token_hex", lambda _size: "a" * 64)

    first = _initialize_auth_only(monkeypatch, config, config_path)
    token = first.jwt_handler.create_jwt("operator", "test-client")
    device_token_hash = first.token_manager.hash_token("device-credential")

    assert config["repeater"]["security"]["jwt_secret"] == "a" * 64
    assert (
        yaml.safe_load(config_path.read_text(encoding="utf-8"))["repeater"]["security"][
            "jwt_secret"
        ]
        == "a" * 64
    )

    # ConfigManager is the normal later settings-save path. The live dictionary
    # must already contain the generated secret or this save would erase it.
    assert ConfigManager(str(config_path), config).save_to_file() is True
    restarted_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    restarted = _initialize_auth_only(monkeypatch, restarted_config, config_path)

    assert restarted.jwt_handler.verify_jwt(token) is not None
    assert restarted.token_manager.hash_token("device-credential") == device_token_hash


def test_generated_jwt_secret_reuses_cross_process_winner(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    config_path = tmp_path / "config.yaml"
    config = {
        "repeater": {
            "security": {
                "jwt_secret": None,
                "jwt_expiry_minutes": 60,
            }
        }
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    lock_path = tmp_path / ".config.yaml.jwt-secret.lock"
    lock_stream = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
    command = (
        "from repeater.web.http_server import _persist_generated_jwt_secret;"
        "import sys;"
        "print(_persist_generated_jwt_secret(sys.argv[1], 'a' * 64))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", command, str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    winner = "b" * 64
    try:
        # The child cannot read a stale null and replace the config while the
        # first starter owns the transaction lock.
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.25)
        config["repeater"]["security"]["jwt_secret"] = winner
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()

    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert stdout.strip() == winner
    assert (
        yaml.safe_load(config_path.read_text(encoding="utf-8"))["repeater"]["security"][
            "jwt_secret"
        ]
        == winner
    )


@pytest.mark.parametrize(
    "jwt_secret",
    (123, [], {}, " \t", "a" * 31, "é" * 15),
)
def test_invalid_configured_jwt_secret_fails_startup(
    monkeypatch,
    tmp_path,
    jwt_secret,
):
    config = {
        "repeater": {
            "security": {
                "jwt_secret": jwt_secret,
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }

    with pytest.raises(ValueError, match="jwt_secret"):
        _initialize_auth_only(monkeypatch, config, tmp_path / "config.yaml")


def test_legacy_empty_jwt_secret_sentinel_generates_and_persists(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config = {
        "repeater": {
            "security": {
                "jwt_secret": "",
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(hs.secrets, "token_hex", lambda _size: "d" * 64)

    server = _initialize_auth_only(monkeypatch, config, config_path)

    assert server.jwt_handler.secret == "d" * 64
    assert config["repeater"]["security"]["jwt_secret"] == "d" * 64
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["repeater"]["security"]["jwt_secret"] == "d" * 64


@pytest.mark.parametrize("jwt_secret", ("a" * 32, "é" * 16, "😀" * 8))
def test_jwt_secret_strength_floor_counts_utf8_bytes(jwt_secret):
    assert hs._jwt_signing_secret(jwt_secret) == jwt_secret


def test_weak_persisted_jwt_secret_fails_closed_instead_of_winning(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    persisted_config = {
        "repeater": {
            "security": {
                "jwt_secret": "persisted-but-weak",
                "jwt_expiry_minutes": 60,
            }
        }
    }
    config_path.write_text(
        yaml.safe_dump(persisted_config, sort_keys=False),
        encoding="utf-8",
    )
    live_config = {
        "repeater": {
            "security": {
                "jwt_secret": None,
                "jwt_expiry_minutes": 60,
            }
        },
        "storage": {"storage_dir": str(tmp_path / "storage")},
    }
    monkeypatch.setattr(hs.secrets, "token_hex", lambda _size: "a" * 64)

    with pytest.raises(RuntimeError, match="at least 32 UTF-8 bytes"):
        _initialize_auth_only(monkeypatch, live_config, config_path)

    assert live_config["repeater"]["security"]["jwt_secret"] is None
    assert (
        yaml.safe_load(config_path.read_text(encoding="utf-8"))["repeater"]["security"][
            "jwt_secret"
        ]
        == "persisted-but-weak"
    )


def test_generated_jwt_secret_persistence_failure_is_fatal(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    original = "repeater:\n  security:\n    jwt_secret: null\n"
    config_path.write_text(original, encoding="utf-8")

    def deny_replace(*_args):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(hs.os, "replace", deny_replace)

    with pytest.raises(RuntimeError, match="could not be persisted durably"):
        hs._persist_generated_jwt_secret(config_path, "b" * 64)

    assert config_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".config.yaml.*.tmp")) == []


@pytest.mark.parametrize(
    "invalid_yaml",
    (
        "- not\n- an\n- object\n",
        "repeater: []\n",
        "repeater:\n  security: []\n",
        "repeater:\n  security:\n    jwt_secret: 123\n",
    ),
)
def test_generated_jwt_secret_rejects_invalid_config_file(tmp_path, invalid_yaml):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(invalid_yaml, encoding="utf-8")

    with pytest.raises(RuntimeError):
        hs._persist_generated_jwt_secret(config_path, "c" * 64)


def test_log_buffer_emit_collects_messages():
    buf = hs.LogBuffer(max_lines=2)
    rec1 = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    rec2 = logging.LogRecord("x", logging.ERROR, __file__, 2, "boom", (), None)
    rec3 = logging.LogRecord("x", logging.WARNING, __file__, 3, "warn", (), None)

    buf.emit(rec1)
    buf.emit(rec2)
    buf.emit(rec3)

    assert len(buf.logs) == 2
    assert buf.logs[-1]["level"] == "WARNING"
    assert "warn" in buf.logs[-1]["message"]


def test_log_buffer_emit_redacts_sensitive_values():
    buf = hs.LogBuffer(max_lines=5)
    rec = logging.LogRecord(
        "auth",
        logging.DEBUG,
        __file__,
        10,
        "auth password=secret123 token=abc123 Authorization: Bearer deadbeef",
        (),
        None,
    )

    buf.emit(rec)

    assert len(buf.logs) == 1
    entry = buf.logs[0]
    assert "secret123" not in entry["message"]
    assert "abc123" not in entry["message"]
    assert "deadbeef" not in entry["message"]
    assert "[REDACTED]" in entry["message"]
    assert "raw_message" not in entry


def test_log_buffer_redacts_quoted_structured_and_url_credentials():
    text = (
        'json={"password":"two words","identity_key":"private-material"} '
        "dict={'push_token': 'device wake token', 'secret': 'channel psk'} "
        "url=/events?token=query-token&cursor=3 "
        "Authorization: Bearer header-token"
    )

    sanitized = hs.LogBuffer._sanitize_log_text(text)

    for secret in (
        "two words",
        "private-material",
        "device wake token",
        "channel psk",
        "query-token",
        "header-token",
    ):
        assert secret not in sanitized
    assert sanitized.count("[REDACTED]") == 6
    assert '"password":"[REDACTED]"' in sanitized
    assert "'push_token': '[REDACTED]'" in sanitized
    assert "cursor=3" in sanitized


def test_log_buffer_emit_includes_exception_text_without_crashing():
    buf = hs.LogBuffer(max_lines=5)
    try:
        raise RuntimeError("boom password=secret123")
    except RuntimeError:
        rec = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            20,
            "failure while sending advert",
            (),
            sys.exc_info(),
        )

    buf.emit(rec)

    assert len(buf.logs) == 1
    assert "exception" in buf.logs[0]
    assert "RuntimeError" in buf.logs[0]["exception"]
    assert "secret123" not in buf.logs[0]["exception"]


def test_doc_endpoint_routes_and_openapi_json_paths(monkeypatch):
    api = SimpleNamespace(docs=lambda: "docs-html")
    doc = hs.DocEndpoint(api)

    assert doc.index() == "docs-html"
    assert doc.docs() == "docs-html"

    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )

    # success path
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO("openapi: 3.0.0\n"))
    out = doc.openapi_json()
    assert cherrypy.response.headers["Content-Type"] == "application/json"
    assert b"openapi" in out

    # not found
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", _missing)
    out = doc.openapi_json()
    assert cherrypy.response.status == 404
    assert b"not found" in out

    # generic error
    def _err(*_args, **_kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr("builtins.open", _err)
    out = doc.openapi_json()
    assert cherrypy.response.status == 500
    assert b"Error loading OpenAPI spec" in out


def test_stats_app_index_and_default_routing(monkeypatch, tmp_path):
    index_html = tmp_path / "index.html"
    index_html.write_text("<html>ok</html>", encoding="utf-8")

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    response = SimpleNamespace(headers={})
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    assert app.index() == "<html>ok</html>"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="OPTIONS"), raising=False)
    assert app.default("anything") == ""

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    with pytest.raises(cherrypy.NotFound):
        app.default("api")

    assert app.default("ws", "packets") == ""
    assert app.default("route") == "<html>ok</html>"


def _static_test_app(monkeypatch, tmp_path, accept_encoding="", response_headers=None):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(
            method="GET",
            headers={"Accept-Encoding": accept_encoding} if accept_encoding else {},
        ),
        raising=False,
    )
    response = SimpleNamespace(headers=dict(response_headers or {}))
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})
    return app, response


def _capture_static_serve(monkeypatch):
    served = []

    def serve_file(path, content_type=None):
        served.append((Path(path), content_type))
        return b"streamed"

    monkeypatch.setattr(cherrypy_static, "serve_file", serve_file)
    return served


def test_stats_app_negotiates_precompressed_hashed_assets(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    Path(f"{logical_asset}.gz").write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="gzip;q=0.5, br;q=1",
        response_headers={"Vary": "Origin"},
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(Path(f"{logical_asset}.br"), "text/javascript")]
    assert response.headers["Content-Encoding"] == "br"
    assert response.headers["Vary"] == "Origin, Accept-Encoding"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_stats_app_serves_gzip_when_brotli_is_not_accepted(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.css"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    gzip_asset = Path(f"{logical_asset}.gz")
    gzip_asset.write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="br;q=0, gzip",
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(gzip_asset, "text/css")]
    assert response.headers["Content-Encoding"] == "gzip"


def test_stats_app_preserves_byte_range_serving(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"0123456789")

    app, _ = _static_test_app(monkeypatch, tmp_path)

    original_request = cherrypy.serving.request
    original_response = cherrypy.serving.response
    request = cherrypy._cprequest.Request(
        original_request.local,
        original_request.remote,
        server_protocol="HTTP/1.1",
    )
    request.headers["Range"] = "bytes=2-5"
    response = cherrypy._cprequest.Response()
    cherrypy.serving.load(request, response)
    try:
        body = app._serve_static_file(str(assets), (logical_asset.name,))
        assert b"".join(body) == b"2345"
        assert response.status == "206 Partial Content"
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.headers["Accept-Ranges"] == "bytes"
    finally:
        cherrypy.serving.load(original_request, original_response)


def test_stats_app_uses_identity_when_encodings_are_rejected(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    Path(f"{logical_asset}.gz").write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="br;q=0, gzip;q=0",
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(logical_asset, "text/javascript")]
    assert "Content-Encoding" not in response.headers
    assert response.headers["Vary"] == "Accept-Encoding"


def test_stats_app_does_not_negotiate_an_explicit_sidecar_path(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    requested_asset = assets / "archive.gz"
    requested_asset.write_bytes(b"direct")
    Path(f"{requested_asset}.br").write_bytes(b"unrelated")

    app, response = _static_test_app(monkeypatch, tmp_path, accept_encoding="br")
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", requested_asset.name) == b"streamed"
    assert served == [(requested_asset, "application/octet-stream")]
    assert "Content-Encoding" not in response.headers


def test_stats_app_ignores_sidecar_symlinks_outside_static_root(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    outside_sidecar = tmp_path / "outside.br"
    outside_sidecar.write_bytes(b"outside")
    Path(f"{logical_asset}.br").symlink_to(outside_sidecar)

    app, response = _static_test_app(monkeypatch, tmp_path, accept_encoding="br")
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(logical_asset, "text/javascript")]
    assert "Content-Encoding" not in response.headers


def test_stats_app_rejects_static_traversal_into_prefix_sibling(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    sibling = tmp_path / "assets-secret"
    sibling.mkdir()
    (sibling / "leak.js").write_bytes(b"secret")

    app, _ = _static_test_app(monkeypatch, tmp_path)

    with pytest.raises(cherrypy.NotFound):
        app._serve_static_file(str(assets), ("..", "assets-secret", "leak.js"))


def test_stats_app_exposes_compiled_ui_favicon(monkeypatch, tmp_path):
    favicon = b"compiled-ui-favicon"
    favicon_path = tmp_path / "favicon.ico"
    favicon_path.write_bytes(favicon)

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    response = SimpleNamespace(headers={})
    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(headers={}), raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    served = []

    def serve_file(path, content_type=None):
        served.append((Path(path), content_type))
        return Path(path).read_bytes()

    monkeypatch.setattr(cherrypy_static, "serve_file", serve_file)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    assert app.favicon_ico() == favicon
    assert served == [(favicon_path, "image/x-icon")]
    assert response.headers["Vary"] == "Accept-Encoding"


def test_stats_app_index_error_paths(monkeypatch, tmp_path):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    with pytest.raises(cherrypy.HTTPError):
        app.index()

    # Force generic open() exception branch
    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("builtins.open", _explode)
    (tmp_path / "index.html").write_text("ignored", encoding="utf-8")
    with pytest.raises(cherrypy.HTTPError):
        app.index()


def test_http_server_utility_methods(monkeypatch, tmp_path):
    def _fake_init_auth(self):
        self.jwt_handler = object()
        self.token_manager = object()

    monkeypatch.setattr(hs.HTTPStatsServer, "_init_auth_handlers", _fake_init_auth)
    monkeypatch.setattr(
        hs,
        "StatsApp",
        lambda *args, **kwargs: SimpleNamespace(api=SimpleNamespace(config_manager=object())),
    )
    monkeypatch.setattr(hs, "AuthEndpoints", lambda *args, **kwargs: object())
    monkeypatch.setattr(hs, "DocEndpoint", lambda *_args, **_kwargs: object())

    server = hs.HTTPStatsServer(
        config={"web": {"cors_enabled": False}}, config_path=str(Path(tmp_path) / "cfg.yml")
    )

    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}), raising=False)
    out = server._json_error_handler(401, "no", "", "")
    assert '"success": false' in out

    resp = SimpleNamespace(headers={})
    monkeypatch.setattr(cherrypy, "response", resp, raising=False)
    out_v1 = hs._json_error_page_v1("404 Not Found", "not found", "", "")
    assert resp.headers["Content-Type"] == "application/json"
    import json as _json

    parsed = _json.loads(out_v1)
    assert parsed == {"success": False, "error": "not found", "status": 404}

    install_called = {"v": False}
    monkeypatch.setattr(
        hs,
        "_register_safe_cors_tool",
        lambda: install_called.__setitem__("v", True),
    )
    server._setup_server_cors()
    assert install_called["v"] is True

    exited = {"v": False}
    monkeypatch.setattr(
        cherrypy,
        "engine",
        SimpleNamespace(exit=lambda: exited.__setitem__("v", True)),
        raising=False,
    )
    server.stop()
    assert exited["v"] is True


def test_cors_response_headers_allow_bearer_preflight_without_credentials():
    headers = dict(hs._cors_response_headers())

    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert "Authorization" in headers["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Credentials" not in headers
