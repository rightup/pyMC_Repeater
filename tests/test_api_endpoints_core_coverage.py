import asyncio
import copy
import io
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import cherrypy
import pytest
import yaml

from repeater.web.api_endpoints import APIEndpoints


def _make_api(config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api._bootstrap_mutation_lock = threading.Lock()
    api.daemon_instance = None
    api.send_advert_func = None
    api.event_loop = None
    api.stats_getter = None
    # Most unit tests exercise the in-memory authority directly. Tests for
    # persisted rechecks set an explicit temporary path.
    api._config_path = None
    api.config_manager = MagicMock()
    return api


def _attach_storage(api, storage):
    api.daemon_instance = SimpleNamespace(repeater_handler=SimpleNamespace(storage=storage))


def _set_json_body(request, value):
    raw = json.dumps(value).encode("utf-8")
    _set_raw_json_body(request, raw)


def _set_raw_json_body(request, raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    request.body = io.BytesIO(raw)
    request.headers["Content-Length"] = str(len(raw))
    request.headers["Content-Type"] = "application/json"


class _FakeDiscoveryHelper:
    def __init__(self):
        self.cleanup_called = False
        self.started_sessions = []
        self._session = {
            "session_id": "sess-1",
            "tag": 123,
            "status": "created",
            "timeout": 5.0,
            "filter_mask": 0x04,
            "since": 0,
            "prefix_only": False,
            "created_at": 1.0,
            "started_at": None,
            "completed_at": None,
            "count": 0,
            "error": None,
        }
        self._events = [
            {"id": 1, "event": "started", "data": {"session_id": "sess-1"}},
            {
                "id": 2,
                "event": "discovery_result",
                "data": {"session_id": "sess-1", "result": {"pub_key": "aa" * 32}},
            },
            {"id": 3, "event": "completed", "data": {"session_id": "sess-1", "count": 1}},
        ]

    def cleanup_sessions(self):
        self.cleanup_called = True

    def create_session(self, **kwargs):
        self._session.update(
            {
                "timeout": kwargs["timeout"],
                "filter_mask": kwargs["filter_mask"],
                "since": kwargs["since"],
                "prefix_only": kwargs["prefix_only"],
            }
        )
        return dict(self._session)

    def start_session_task(self, session_id):
        self.started_sessions.append(session_id)

    def get_session_snapshot(self, session_id):
        return dict(self._session) if session_id == self._session["session_id"] else None

    def get_events_since(self, session_id, last_event_id=0):
        if session_id != self._session["session_id"]:
            return None
        events = [event for event in self._events if event["id"] > last_event_id]
        return {"events": events, "completed": True, "status": "completed", "latest_event_id": 3}


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={}, headers={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request, response


def test_set_cors_headers_enabled(cherrypy_ctx):
    request, response = cherrypy_ctx
    request.headers["Origin"] = "https://chat.example"
    api = _make_api(
        {
            "web": {
                "cors_enabled": True,
                "cors_origins": ["https://chat.example"],
            }
        }
    )

    api._set_cors_headers()

    assert response.headers["Access-Control-Allow-Origin"] == "https://chat.example"
    assert response.headers["Vary"] == "Origin"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
    assert response.headers["Access-Control-Expose-Headers"] == (
        "ETag, Idempotency-Replayed, Retry-After"
    )


def test_set_cors_headers_disabled(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": False}})

    api._set_cors_headers()

    assert response.headers == {}


def test_default_returns_empty_for_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "OPTIONS"
    api = _make_api()

    assert api.default() == ""


def test_default_raises_404_for_non_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()

    with pytest.raises(cherrypy.HTTPError) as exc:
        api.default()
    assert exc.value.status == 404


def test_get_storage_success_and_failure_paths():
    api = _make_api()
    with pytest.raises(Exception, match="Daemon not available"):
        api._get_storage()

    api.daemon_instance = SimpleNamespace()
    with pytest.raises(Exception, match="Repeater handler not initialized"):
        api._get_storage()

    api.daemon_instance.repeater_handler = SimpleNamespace(storage=None)
    with pytest.raises(Exception, match="Storage not initialized"):
        api._get_storage()

    storage = object()
    api.daemon_instance.repeater_handler.storage = storage
    assert api._get_storage() is storage


def test_get_params_casts_int_float_and_none(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.params = {"count": "7", "ratio": "2.5", "name": "node", "maybe": None}
    api = _make_api()

    parsed = api._get_params({"count": 0, "ratio": 0.0, "name": "", "maybe": 1})

    assert parsed == {"count": 7, "ratio": 2.5, "name": "node", "maybe": None}


def test_require_post_enforces_method(cherrypy_ctx):
    request, response = cherrypy_ctx
    api = _make_api()

    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError) as exc:
        api._require_post()
    assert exc.value.status == 405
    assert response.status == 405
    assert response.headers["Allow"] == "POST"

    request.method = "POST"
    api._require_post()


def test_fmt_hash_respects_path_hash_mode():
    pubkey = bytes.fromhex("19272233AA")

    api = _make_api({"mesh": {"path_hash_mode": 0}})
    assert api._fmt_hash(pubkey) == "0x19"

    api.config["mesh"]["path_hash_mode"] = 1
    assert api._fmt_hash(pubkey) == "0x1927"

    api.config["mesh"]["path_hash_mode"] = 2
    assert api._fmt_hash(pubkey) == "0x192722"


def test_process_counter_and_gauge_data():
    api = _make_api()

    counter = api._process_counter_data([None, 10, 13, 9], [1000, 2000, 3000, 4000])
    gauge = api._process_gauge_data([1, None, 3], [1000, 2000, 3000])

    assert counter == [[1000, 0], [2000, 0], [3000, 3], [4000, 0]]
    assert gauge == [[1000, 1], [2000, 0], [3000, 3]]


def test_success_and_error_helpers():
    api = _make_api()

    ok = api._success([1, 2], source="unit")
    err = api._error("boom")

    assert ok == {"success": True, "data": [1, 2], "source": "unit"}
    assert err == {"success": False, "error": "boom"}


def test_discover_neighbors_start_schedules_session(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {"timeout": 7, "filter_mask": 0x04, "since": 0, "prefix_only": False}

    helper = _FakeDiscoveryHelper()
    loop = MagicMock()
    api = _make_api()
    api.event_loop = loop
    api.daemon_instance = SimpleNamespace(discovery_helper=helper)

    def run_now(coro, _loop):
        future = MagicMock()
        try:
            future.result.return_value = asyncio.run(coro)
        except BaseException as exc:
            future.result.side_effect = exc
        return future

    with patch(
        "repeater.web.api_endpoints.asyncio.run_coroutine_threadsafe",
        side_effect=run_now,
    ) as schedule:
        result = api.discover_neighbors_start()

    assert result["success"] is True
    assert result["data"]["session_id"] == "sess-1"
    assert helper.cleanup_called is True
    assert helper.started_sessions == ["sess-1"]
    schedule.assert_called_once()
    loop.call_soon_threadsafe.assert_not_called()


def test_concurrent_discovery_allocations_are_serial_on_the_daemon_loop(
    cherrypy_ctx,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "timeout": 7,
        "filter_mask": 0x04,
        "since": 0,
        "prefix_only": False,
    }
    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    loop_thread_id = []

    def run_loop():
        asyncio.set_event_loop(loop)
        loop_thread_id.append(threading.get_ident())
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop)
    loop_thread.start()
    assert loop_ready.wait(timeout=2.0)

    actions = []

    class _RecordingHelper:
        def __init__(self):
            self.next_session = 1

        def cleanup_sessions(self):
            actions.append(("cleanup", threading.get_ident()))

        def create_session(self, **_kwargs):
            session_id = f"sess-{self.next_session}"
            self.next_session += 1
            actions.append((f"create:{session_id}", threading.get_ident()))
            return {"session_id": session_id}

        def start_session_task(self, session_id):
            actions.append((f"start:{session_id}", threading.get_ident()))

    api = _make_api()
    api.event_loop = loop
    api.daemon_instance = SimpleNamespace(discovery_helper=_RecordingHelper())
    callers_ready = threading.Barrier(3)
    results = []

    def call_start():
        callers_ready.wait()
        results.append(api.discover_neighbors_start())

    callers = [threading.Thread(target=call_start) for _ in range(2)]
    for caller in callers:
        caller.start()
    callers_ready.wait()
    for caller in callers:
        caller.join(timeout=3.0)

    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=3.0)
    loop.close()

    assert len(results) == 2
    assert all(result["success"] for result in results)
    assert [action for action, _thread in actions] in (
        [
            "cleanup",
            "create:sess-1",
            "start:sess-1",
            "cleanup",
            "create:sess-2",
            "start:sess-2",
        ],
        [
            "cleanup",
            "create:sess-2",
            "start:sess-2",
            "cleanup",
            "create:sess-1",
            "start:sess-1",
        ],
    )
    assert {thread_id for _action, thread_id in actions} == {loop_thread_id[0]}


def test_trace_tag_retries_pending_frame_and_companion_owned_values():
    api = _make_api()
    trace_helper = SimpleNamespace(pending_pings={0x11: {}})
    frame_has_owner = MagicMock(side_effect=lambda _kind, tag: tag == 0x22)
    companion_has_owner = MagicMock(side_effect=lambda tag: tag == 0x33)
    api.daemon_instance = SimpleNamespace(
        _frame_has_response_owner=frame_has_owner,
        _companion_has_trace_owner=companion_has_owner,
    )

    with patch(
        "repeater.web.api_endpoints.secrets.randbits",
        side_effect=[0x11, 0x22, 0x33, 0x44],
    ):
        tag = api._allocate_trace_tag(trace_helper)

    assert tag == 0x44
    frame_has_owner.assert_any_call("trace", 0x22)
    companion_has_owner.assert_any_call(0x33)


def test_trace_tag_exhaustion_fails_before_radio_injection(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {"target_id": "0x42", "timeout": 1}
    router = SimpleNamespace(inject_packet=AsyncMock())
    api = _make_api()
    api.event_loop = object()
    api.daemon_instance = SimpleNamespace(
        router=router,
        trace_helper=SimpleNamespace(pending_pings={}),
        _frame_has_response_owner=lambda _kind, _tag: True,
    )

    def run_now(coro, _loop):
        future = MagicMock()
        try:
            future.result.return_value = asyncio.run(coro)
        except BaseException as exc:
            future.result.side_effect = exc
        return future

    with (
        patch(
            "repeater.web.api_endpoints.secrets.randbits",
            return_value=0x44,
        ),
        patch(
            "repeater.web.api_endpoints.asyncio.run_coroutine_threadsafe",
            side_effect=run_now,
        ),
    ):
        result = api.ping_neighbor()

    assert result["success"] is False
    assert "No unique trace response tag" in result["error"]
    router.inject_packet.assert_not_awaited()


def test_discover_neighbors_stream_yields_session_events(cherrypy_ctx):
    request, response = cherrypy_ctx
    request.method = "GET"

    helper = _FakeDiscoveryHelper()
    api = _make_api()
    api.daemon_instance = SimpleNamespace(discovery_helper=helper)

    stream = api.discover_neighbors_stream(session_id="sess-1")
    payload = "".join(list(stream))

    assert response.headers["Content-Type"] == "text/event-stream"
    assert "event: connected" in payload
    assert "event: started" in payload
    assert "event: discovery_result" in payload
    assert "event: completed" in payload


def test_add_discovered_neighbor_records_zero_hop_advert(cherrypy_ctx, monkeypatch):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers = {"Authorization": "Bearer test-token"}
    request.path_info = "/api/add_discovered_neighbor"
    request.json = {
        "pub_key": "aa" * 32,
        "node_name": "Field Repeater",
        "node_type": 2,
        "rssi": -71,
        "response_snr": 4.5,
    }

    jwt_handler = MagicMock()
    jwt_handler.verify_jwt.return_value = {"sub": "tester", "client_id": "ui"}
    token_manager = MagicMock()
    monkeypatch.setattr(
        cherrypy,
        "config",
        {"jwt_handler": jwt_handler, "token_manager": token_manager},
        raising=False,
    )

    storage = MagicMock()
    api = _make_api()
    api._enrich_discovery_result = lambda result: {**result, "known_neighbor": True}
    _attach_storage(api, storage)

    result = api.add_discovered_neighbor()

    assert result["success"] is True
    storage.record_advert.assert_called_once()
    advert_record = storage.record_advert.call_args.args[0]
    assert advert_record["route_type"] == 2
    assert advert_record["zero_hop"] is True
    assert advert_record["contact_type"] == "Repeater"


def test_add_discovered_neighbor_normalizes_unknown_name(cherrypy_ctx, monkeypatch):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers = {"Authorization": "Bearer test-token"}
    request.path_info = "/api/add_discovered_neighbor"
    request.json = {
        "pub_key": "bb" * 32,
        "node_name": "Unknown Node",
        "node_type": 2,
    }

    jwt_handler = MagicMock()
    jwt_handler.verify_jwt.return_value = {"sub": "tester", "client_id": "ui"}
    token_manager = MagicMock()
    monkeypatch.setattr(
        cherrypy,
        "config",
        {"jwt_handler": jwt_handler, "token_manager": token_manager},
        raising=False,
    )

    storage = MagicMock()
    api = _make_api()
    api._enrich_discovery_result = lambda result: result
    _attach_storage(api, storage)

    result = api.add_discovered_neighbor()

    assert result["success"] is True
    advert_record = storage.record_advert.call_args.args[0]
    assert advert_record["node_name"] is None


def test_add_discovered_neighbor_skips_local_node(cherrypy_ctx, monkeypatch):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers = {"Authorization": "Bearer test-token"}
    request.path_info = "/api/add_discovered_neighbor"
    request.json = {
        "pub_key": "aa" * 32,
        "node_name": "Self",
        "node_type": 2,
    }

    jwt_handler = MagicMock()
    jwt_handler.verify_jwt.return_value = {"sub": "tester", "client_id": "ui"}
    token_manager = MagicMock()
    monkeypatch.setattr(
        cherrypy,
        "config",
        {"jwt_handler": jwt_handler, "token_manager": token_manager},
        raising=False,
    )

    storage = MagicMock()
    api = _make_api()
    api._enrich_discovery_result = APIEndpoints._enrich_discovery_result.__get__(api, APIEndpoints)
    _attach_storage(api, storage)
    api.daemon_instance.local_identity = SimpleNamespace(
        get_public_key=lambda: bytes.fromhex("aa" * 32)
    )

    result = api.add_discovered_neighbor()

    assert result["success"] is True
    assert "Skipped local node" in result["message"]
    assert result["data"]["is_self"] is True
    storage.record_advert.assert_not_called()


def test_enrich_discovery_result_treats_placeholder_name_as_unknown():
    api = _make_api()

    storage = MagicMock()
    storage.get_neighbors.return_value = {}
    storage.get_node_name_by_pubkey.return_value = "Unknown"
    _attach_storage(api, storage)

    result = api._enrich_discovery_result(
        {
            "pub_key": "cc" * 32,
            "node_name": "Unknown Node",
            "node_type": 2,
        }
    )

    assert result["known_neighbor"] is False
    assert result["node_name"] is None


def test_get_time_range_uses_current_time(monkeypatch):
    api = _make_api()
    monkeypatch.setattr("repeater.web.api_endpoints.time.time", lambda: 10_000)

    start, end = api._get_time_range(2)

    assert end == 10_000
    assert start == 2_800


def test_setup_status_from_config_variants():
    api = _make_api()

    needs_setup, reasons = api._setup_status_from_config(
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "admin123"},
            },
            "radio_type": "none",
        }
    )
    assert needs_setup is True
    assert reasons == {
        "default_name": True,
        "default_password": True,
        "radio_not_configured": True,
    }

    needs_setup2, reasons2 = api._setup_status_from_config(
        {
            "repeater": {
                "node_name": "mesh-node-77",
                "security": {"admin_password": "verysecret"},
            },
            "radio_type": "sx1262",
        }
    )
    assert needs_setup2 is False
    assert reasons2["radio_not_configured"] is False


def test_site_info_success_and_error_fallback():
    api = _make_api({"web": {"site_name": "Field Node"}})
    assert api.site_info() == {"success": True, "site_name": "Field Node"}

    class _BadConfig(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("bad")

    api_bad = _make_api(_BadConfig())
    assert api_bad.site_info() == {"success": True, "site_name": ""}


def test_hardware_options_loads_installed_file(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    hardware_file = tmp_path / "radio-settings.json"
    hardware_file.write_text(
        '{"hardware":{"pymc_usb":{"name":"USB","description":"desc","radio_type":"pymc_usb"}}}',
        encoding="utf-8",
    )

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        result = api.hardware_options()

    assert len(result["hardware"]) == 1
    assert result["hardware"][0]["key"] == "pymc_usb"
    assert result["hardware"][0]["name"] == "USB"


def test_radio_presets_returns_error_when_file_missing(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        with patch("os.path.exists", return_value=False):
            result = api.radio_presets()

    assert result["error"] == "Radio presets file not found"


def test_radio_presets_loads_entries_from_installed_file(tmp_path):
    config = {"repeater": {"storage_dir": str(tmp_path)}}
    api = _make_api(config)
    api._config_path = str(tmp_path / "config.yaml")

    presets_file = tmp_path / "radio-presets.json"
    presets_file.write_text(
        '{"config":{"suggested_radio_settings":{"entries":[{"label":"Fast","frequency":869.5}]}}}',
        encoding="utf-8",
    )

    with patch("repeater.web.api_endpoints.resolve_storage_dir", return_value=Path(tmp_path)):
        result = api.radio_presets()

    assert result["source"] == "local"
    assert len(result["presets"]) == 1
    assert result["presets"][0]["label"] == "Fast"


def test_needs_setup_reads_config_file_when_available(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
repeater:
  node_name: mesh-node-11
  security:
    admin_password: longsecret
radio_type: sx1262
""".strip(),
        encoding="utf-8",
    )

    api = _make_api(
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "admin123"},
            },
            "radio_type": "none",
        }
    )
    api._config_path = str(config_path)

    result = api.needs_setup()

    assert result["needs_setup"] is False
    assert result["reasons"]["radio_not_configured"] is False


def test_serial_ports_uses_pyserial_metadata(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    p1 = SimpleNamespace(device="/dev/ttyACM0", description="USB CDC", hwid="VID:PID")
    p2 = SimpleNamespace(device="/dev/ttyUSB0", description="CH340", hwid="n/a")

    with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
        result = api.serial_ports()

    assert result["success"] is True
    devices = result["data"]
    assert devices[0]["device"] == "/dev/ttyACM0"
    assert "VID:PID" in devices[0]["description"]
    assert devices[1]["device"] == "/dev/ttyUSB0"


def test_serial_ports_dedupes_duplicate_devices(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    p1 = SimpleNamespace(device="/dev/ttyACM0", description="first", hwid="A")
    p2 = SimpleNamespace(device="/dev/ttyACM0", description="second", hwid="B")

    with patch("serial.tools.list_ports.comports", return_value=[p1, p2]):
        result = api.serial_ports()

    assert result["success"] is True
    assert len(result["data"]) == 1
    assert "first" in result["data"][0]["description"]


def test_config_export_redacts_secrets_and_identity_keys(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"

    api = _make_api(
        {
            "repeater": {
                "security": {
                    "admin_password": "pw1",
                    "guest_password": "pw2",
                    "jwt_secret": "jwt",
                },
                "identity_key": bytes.fromhex("AABB"),
            },
            "identities": {
                "companions": [{"name": "c1", "identity_key": bytes.fromhex("0102")}],
                "room_servers": [{"name": "r1", "identity_key": bytes.fromhex("0304")}],
            },
            "misc": {"blob": b"\x0a\x0b"},
        }
    )

    result = api.config_export()

    assert result["success"] is True
    exported = result["data"]["config"]
    sec = exported["repeater"]["security"]
    assert sec["admin_password"] == "*** REDACTED ***"
    assert sec["guest_password"] == "*** REDACTED ***"
    assert sec["jwt_secret"] == "*** REDACTED ***"
    assert "identity_key" not in exported["repeater"]
    assert exported["identities"]["companions"][0]["identity_key"] == "*** REDACTED ***"
    assert exported["misc"]["blob"] == "0a0b"
    assert result["data"]["meta"]["includes_secrets"] is False


def test_config_export_full_backup_includes_hex_keys(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"

    api = _make_api(
        {
            "repeater": {"identity_key": bytes.fromhex("AABB")},
            "identities": {
                "companions": [{"name": "c1", "identity_key": bytes.fromhex("0102")}],
                "room_servers": [{"name": "r1", "identity_key": bytes.fromhex("0304")}],
            },
        }
    )

    result = api.config_export(include_secrets="true")

    assert result["success"] is True
    exported = result["data"]["config"]
    assert exported["repeater"]["identity_key"] == "aabb"
    assert exported["identities"]["companions"][0]["identity_key"] == "0102"
    assert exported["identities"]["room_servers"][0]["identity_key"] == "0304"
    assert result["data"]["meta"]["includes_secrets"] is True


def test_config_import_rejects_missing_config_object(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    _set_json_body(request, {})
    api = _make_api()

    result = api.config_import()

    assert result["success"] is False
    assert "Missing or invalid 'config' object" in result["error"]


def test_config_import_rejects_cross_origin_simple_content_type_before_mutation(
    cherrypy_ctx,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    _set_json_body(request, {"config": {"web": {"site_name": "unsafe"}}})
    request.headers["Content-Type"] = "text/plain"
    api = _make_api()

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        api.config_import()

    assert int(str(exc_info.value.status).split()[0]) == 415
    assert api.config == {}
    api.config_manager.update_and_save.assert_not_called()


def test_public_config_import_is_single_use_under_concurrency(
    cherrypy_ctx,
    monkeypatch,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api(
        {
            "repeater": {
                "security": {"admin_password": "admin123"},
            }
        }
    )
    api.config_manager.update_and_save.return_value = {"success": True}
    api.config_manager.save_to_file.return_value = True
    api._require_admin_on_public_route = MagicMock(
        side_effect=cherrypy.HTTPError(403, "Public bootstrap is closed")
    )

    first_body_started = threading.Event()
    release_first_body = threading.Event()
    second_body_read = threading.Event()

    def read_body():
        thread_name = threading.current_thread().name
        if thread_name == "first-import":
            first_body_started.set()
            assert release_first_body.wait(timeout=2)
        else:
            second_body_read.set()
        return {
            "config": {
                "repeater": {"security": {"admin_password": f"{thread_name}-password"}},
                "web": {"site_name": thread_name},
            }
        }

    monkeypatch.setattr(
        "repeater.web.api_endpoints.read_json_object",
        lambda **_kwargs: read_body(),
    )

    results = {}
    errors = {}

    def run_import(label):
        try:
            results[label] = api.config_import()
        except Exception as exc:
            errors[label] = exc

    first = threading.Thread(
        target=run_import,
        args=("first",),
        name="first-import",
    )
    second = threading.Thread(
        target=run_import,
        args=("second",),
        name="second-import",
    )

    first.start()
    assert first_body_started.wait(timeout=2)
    second.start()
    assert second_body_read.wait(timeout=2)
    second.join(timeout=2)
    assert not second.is_alive()
    release_first_body.set()
    first.join(timeout=2)
    assert not first.is_alive()

    assert results["second"]["success"] is True
    assert isinstance(errors["first"], cherrypy.HTTPError)
    assert errors["first"].status == 403
    assert api.config["repeater"]["setup_complete"] is True
    assert api.config_manager.update_and_save.call_count == 1
    assert api.config_manager.save_to_file.call_count == 1


def test_public_config_import_requires_strong_admin_password(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    original = {
        "repeater": {
            "security": {"admin_password": "admin123"},
        }
    }
    api = _make_api(copy.deepcopy(original))
    _set_json_body(request, {"config": {"web": {"site_name": "restored"}}})

    result = api.config_import()

    assert result["success"] is False
    assert "non-default administrator password" in result["error"]
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


def test_config_import_rejects_malformed_section_before_live_mutation(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    original = {
        "repeater": {
            "security": {"admin_password": "admin123"},
        }
    }
    api = _make_api(copy.deepcopy(original))
    _set_json_body(request, {"config": {"repeater": "not-an-object"}})

    result = api.config_import()

    assert result == {
        "success": False,
        "error": "Configuration section 'repeater' must be an object",
    }
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()


def test_config_import_rejects_null_security_before_live_mutation(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    original = {
        "repeater": {
            "security": {"admin_password": "admin123"},
        }
    }
    api = _make_api(copy.deepcopy(original))
    _set_json_body(request, {"config": {"repeater": {"security": None}}})

    result = api.config_import()

    assert result == {
        "success": False,
        "error": "Configuration field 'repeater.security' must be an object",
    }
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize(
    "admin_password",
    [None, 123, "short", "admin123", " admin123 ", "x" * 1025],
)
def test_authenticated_config_import_rejects_unusable_new_admin_password(
    cherrypy_ctx,
    admin_password,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    original = {
        "repeater": {
            "setup_complete": True,
            "security": {
                "admin_password": "operator-password",
                "jwt_secret": "s" * 32,
            },
        }
    }
    api = _make_api(copy.deepcopy(original))
    api._require_admin_on_public_route = MagicMock()
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {
                        "admin_password": admin_password,
                    }
                }
            }
        },
    )

    result = api.config_import()

    assert result["success"] is False
    assert "repeater.security.admin_password" in result["error"]
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize(
    "persistence_result",
    [
        {"success": False, "saved": False},
        {"success": False},
    ],
)
def test_config_import_save_failure_restores_shared_live_config(
    cherrypy_ctx,
    persistence_result,
):
    request, response = cherrypy_ctx
    request.method = "POST"
    original = {
        "repeater": {
            "security": {"admin_password": "admin123"},
        },
        "web": {"site_name": "before"},
    }
    api = _make_api(copy.deepcopy(original))
    api.config_manager.update_and_save.return_value = persistence_result
    api.config_manager.save_to_file.return_value = False
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {"admin_password": "strong-password"},
                },
                "web": {"site_name": "after"},
            }
        },
    )

    result = api.config_import()

    assert response.status == 503
    assert result["success"] is False
    assert "could not be persisted" in result["error"]
    assert api.config == original


def test_public_config_import_fails_closed_when_persisted_recheck_disappears(
    cherrypy_ctx,
    monkeypatch,
    tmp_path,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    config_path = tmp_path / "config.yaml"
    original = {
        "repeater": {
            "security": {"admin_password": "admin123"},
        }
    }
    config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    api = _make_api(copy.deepcopy(original))
    api._config_path = str(config_path)
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {"admin_password": "strong-password"},
                }
            }
        },
    )

    real_open = open
    reads = 0

    def fail_second_read(path, *args, **kwargs):
        nonlocal reads
        if str(path) == str(config_path):
            reads += 1
            if reads == 2:
                raise OSError("persisted config disappeared")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_second_read)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        api.config_import()

    assert exc_info.value.status == 503
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    assert api._bootstrap_mutation_lock.acquire(blocking=False)
    api._bootstrap_mutation_lock.release()


@pytest.mark.parametrize(
    "config",
    [
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "strong-password"},
            },
            "radio_type": "none",
        },
        {
            "repeater": {
                "node_name": "configured",
                "security": {"admin_password": "strong-password"},
            },
        },
    ],
)
def test_config_import_strong_password_closes_public_bootstrap(cherrypy_ctx, config):
    request, _ = cherrypy_ctx
    request.method = "POST"
    _set_json_body(request, {"config": {"web": {"site_name": "attacker"}}})
    api = _make_api(config)

    with pytest.raises(cherrypy.HTTPError, match="Public bootstrap is closed"):
        api.config_import()

    assert api.config == config
    api.config_manager.update_and_save.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ('{"config":{"web":{"site_name":"first","site_name":"second"}}}', 400),
        ("{" + '"config":' + "[" * 1100 + "0" + "]" * 1100 + "}", 400),
        (
            json.dumps({"config": {"web": {"padding": "x" * (1024 * 1024)}}}),
            413,
        ),
    ],
)
def test_config_import_rejects_unsafe_json_before_mutation(cherrypy_ctx, raw, status):
    request, _ = cherrypy_ctx
    request.method = "POST"
    _set_raw_json_body(request, raw)
    api = _make_api()

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        api.config_import()

    assert int(str(exc_info.value.status).split()[0]) == status
    assert api.config == {}
    api.config_manager.update_and_save.assert_not_called()


def test_config_import_updates_sections_and_preserves_redacted(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"

    api = _make_api(
        {
            "repeater": {
                "security": {
                    "admin_password": "keep-admin",
                    "guest_password": "keep-guest",
                    "jwt_secret": "k" * 32,
                }
            },
            "identities": {
                "companions": [
                    {"name": "c1", "identity_key": bytes.fromhex("C0FFEE")},
                ]
            },
        }
    )

    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {
                        "admin_password": "*** REDACTED ***",
                        "guest_password": "new-guest",
                        "jwt_secret": "*** REDACTED ***",
                    },
                    "identity_key": "AABBCC",
                    "identity_file": "/tmp/remove-me",
                },
                "identities": {
                    "companions": [
                        {"name": "c1", "identity_key": "*** REDACTED ***"},
                    ]
                },
                "radio": {"frequency": 915000000},
                "radio_type": "pymc_usb",
                "unknown": {"x": 1},
            },
        },
    )
    api._require_admin_on_public_route = MagicMock()

    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is True
    assert set(result["sections_updated"]) == {"repeater", "identities", "radio", "radio_type"}

    sec = api.config["repeater"]["security"]
    assert sec["admin_password"] == "keep-admin"
    assert sec["guest_password"] == "new-guest"
    assert sec["jwt_secret"] == "k" * 32
    assert api.config["repeater"]["identity_key"] == bytes.fromhex("AABBCC")
    assert api.config["repeater"]["setup_complete"] is True
    assert "identity_file" not in api.config["repeater"]
    assert api.config["identities"]["companions"][0]["identity_key"] == bytes.fromhex("C0FFEE")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("jwt_secret", "too-short", "jwt_secret"),
        ("jwt_secret", 123, "jwt_secret"),
        ("jwt_secret", " \t", "jwt_secret"),
        ("jwt_expiry_minutes", True, "jwt_expiry_minutes"),
        ("jwt_expiry_minutes", "60", "jwt_expiry_minutes"),
        ("jwt_expiry_minutes", 1.0, "jwt_expiry_minutes"),
        ("jwt_expiry_minutes", 0, "jwt_expiry_minutes"),
        ("jwt_expiry_minutes", 10_081, "jwt_expiry_minutes"),
    ],
)
def test_config_import_rejects_auth_values_that_cannot_restart(
    cherrypy_ctx,
    field,
    value,
    message,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    original = {
        "repeater": {
            "security": {
                "admin_password": "admin123",
            }
        }
    }
    api = _make_api(copy.deepcopy(original))
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {
                        "admin_password": "strong-password",
                        field: value,
                    }
                }
            }
        },
    )

    result = api.config_import()

    assert result["success"] is False
    assert message in result["error"]
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


@pytest.mark.parametrize("jwt_secret", [None, "", "é" * 16])
def test_config_import_accepts_legacy_secret_sentinels_and_strong_utf8_secret(
    cherrypy_ctx,
    jwt_secret,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api()
    api.config_manager.update_and_save.return_value = {"saved": True}
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {
                        "admin_password": "strong-password",
                        "jwt_secret": jwt_secret,
                        "jwt_expiry_minutes": 10_080,
                    }
                }
            }
        },
    )

    result = api.config_import()

    assert result["success"] is True
    assert api.config["repeater"]["security"]["jwt_secret"] == jwt_secret
    assert api.config["repeater"]["security"]["jwt_expiry_minutes"] == 10_080
    assert result["restart_required"] is True


def test_config_import_security_patch_preserves_omitted_credentials(
    cherrypy_ctx,
):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    original = {
        "repeater": {
            "setup_complete": True,
            "security": {
                "admin_password": "operator-password",
                "guest_password": "guest-password",
                "jwt_secret": "s" * 32,
                "jwt_expiry_minutes": 60,
            },
        }
    }
    api = _make_api(copy.deepcopy(original))
    api._require_admin_on_public_route = MagicMock()
    api.config_manager.update_and_save.return_value = {"saved": True}
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {
                        "jwt_expiry_minutes": 120,
                    }
                }
            }
        },
    )

    result = api.config_import()

    assert result["success"] is True
    assert api.config["repeater"]["security"] == {
        "admin_password": "operator-password",
        "guest_password": "guest-password",
        "jwt_secret": "s" * 32,
        "jwt_expiry_minutes": 120,
    }
    assert result["restart_required"] is True


def test_openapi_success_sets_content_type(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    with patch("builtins.open", mock_open(read_data="openapi: 3.0.0")):
        content = api.openapi()

    assert response.headers["Content-Type"] == "application/x-yaml"
    assert content == b"openapi: 3.0.0"


def test_openapi_not_found_returns_404(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    with patch("builtins.open", side_effect=FileNotFoundError):
        content = api.openapi()

    assert response.status == 404
    assert content == b"OpenAPI spec not found"


def test_docs_returns_html_bytes_and_content_type(cherrypy_ctx):
    _, response = cherrypy_ctx
    api = _make_api()

    content = api.docs()

    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert isinstance(content, bytes)
    assert b"OpenAPI YAML" in content
    assert b"/v1/companions" in content
    assert b"<script" not in content
    assert b"https://" not in content


def test_packet_and_route_stats_endpoints(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_packet_stats=MagicMock(return_value={"total": 10}),
        get_packet_type_stats=MagicMock(return_value={"types": {1: 3}}),
        get_route_stats=MagicMock(return_value={"routes": {2: 5}}),
    )
    _attach_storage(api, storage)

    assert api.packet_stats("24") == {"success": True, "data": {"total": 10}}
    assert api.packet_type_stats("12") == {"success": True, "data": {"types": {1: 3}}}
    assert api.route_stats("6") == {"success": True, "data": {"routes": {2: 5}}}

    storage.get_packet_stats.assert_called_once_with(hours=24)
    storage.get_packet_type_stats.assert_called_once_with(hours=12)
    storage.get_route_stats.assert_called_once_with(hours=6)


def test_neighbor_links_endpoint_returns_snapshot(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    tracker = SimpleNamespace(
        snapshot=MagicMock(
            return_value=[
                {"peer_hash": "AB", "sample_count": 2},
                {"peer_hash": "CD", "sample_count": 1},
            ]
        )
    )
    repeater_handler = SimpleNamespace(neighbour_link_tracker=tracker)
    api.daemon_instance = SimpleNamespace(repeater_handler=repeater_handler)

    result = api.neighbor_links(active_within_seconds="120", limit="1")

    assert result["success"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["active_within_seconds"] == 120.0
    assert result["data"]["limit"] == 1
    assert result["data"]["links"][0]["peer_hash"] == "AB"
    tracker.snapshot.assert_called_once_with(active_within_seconds=120.0)


def test_neighbor_link_history_endpoint_filters_by_hash_and_size(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    rows = [{"packet_hash": "A1", "path_hop_count": 3}]
    storage = SimpleNamespace(get_neighbor_link_history=MagicMock(return_value=rows))
    _attach_storage(api, storage)

    result = api.neighbor_link_history(peer_hash="ab", path_hash_size="2", hours="12", limit="50")

    assert result["success"] is True
    assert result["data"]["peer_hash"] == "AB"
    assert result["data"]["path_hash_size"] == 2
    assert result["data"]["hours"] == 12
    assert result["data"]["limit"] == 50
    assert result["data"]["rows"] == rows
    storage.get_neighbor_link_history.assert_called_once_with(
        peer_hash="ab",
        path_hash_size=2,
        hours=12,
        limit=50,
    )


def test_recent_packets_and_bulk_packets(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    packets = [{"h": "aa"}, {"h": "bb"}]
    storage = SimpleNamespace(
        get_recent_packets=MagicMock(return_value=packets),
        get_filtered_packets=MagicMock(return_value=packets),
    )
    _attach_storage(api, storage)

    recent = api.recent_packets("2")
    bulk = api.bulk_packets(limit="20000", offset="-4", start_timestamp="1.5", end_timestamp="3.5")

    assert recent == {"success": True, "data": packets, "count": 2}
    assert bulk["success"] is True
    assert bulk["count"] == 2
    assert bulk["offset"] == 0
    assert bulk["limit"] == 10000
    assert bulk["compressed"] is True

    storage.get_recent_packets.assert_called_once_with(limit=2)
    storage.get_filtered_packets.assert_called_once_with(
        packet_type=None,
        route=None,
        start_timestamp=1.5,
        end_timestamp=3.5,
        limit=10000,
        offset=0,
    )


def test_filtered_packets_options_and_success(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})
    packets = [{"h": "a1"}]
    storage = SimpleNamespace(get_filtered_packets=MagicMock(return_value=packets))
    _attach_storage(api, storage)

    request.method = "OPTIONS"
    assert api.filtered_packets() == ""

    request.method = "GET"
    result = api.filtered_packets(
        start_timestamp="10",
        end_timestamp="20",
        limit="5",
        type="3",
        route="2",
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["filters"] == {
        "type": 3,
        "route": 2,
        "start_timestamp": 10.0,
        "end_timestamp": 20.0,
        "limit": 5,
    }
    storage.get_filtered_packets.assert_called_once_with(
        packet_type=3,
        route=2,
        start_timestamp=10.0,
        end_timestamp=20.0,
        limit=5,
    )


def test_filtered_packets_invalid_parameter_format(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    _attach_storage(api, SimpleNamespace(get_filtered_packets=MagicMock()))

    result = api.filtered_packets(type="not-an-int")

    assert result["success"] is False
    assert "Invalid parameter format" in result["error"]


def test_airtime_data_limit_and_error(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(get_airtime_data=MagicMock(return_value=[{"a": 1}]))
    _attach_storage(api, storage)

    ok = api.airtime_data(start_timestamp="1", end_timestamp="2", limit="999999")
    assert ok["success"] is True
    assert ok["count"] == 1
    storage.get_airtime_data.assert_called_once_with(
        start_timestamp=1.0,
        end_timestamp=2.0,
        limit=50000,
    )

    storage.get_airtime_data.side_effect = RuntimeError("db down")
    err = api.airtime_data()
    assert err["success"] is False
    assert "db down" in err["error"]


def test_db_stats_options_success_and_error(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.db_stats() == ""

    request.method = "GET"
    rrd = tmp_path / "metrics.rrd"
    rrd.write_bytes(b"123456")
    sqlite_handler = SimpleNamespace(
        get_table_stats=MagicMock(return_value={"packets": {"rows": 10}}),
        storage_dir=tmp_path,
    )
    _attach_storage(
        api,
        SimpleNamespace(
            sqlite_handler=sqlite_handler,
            rrd_enabled=True,
            rrd_handler=object(),
        ),
    )

    result = api.db_stats()
    assert result["success"] is True
    assert result["data"]["packets"]["rows"] == 10
    assert result["data"]["rrd_enabled"] is True
    assert result["data"]["rrd_available"] is True
    assert result["data"]["metrics_data_source"] == "rrd"
    assert result["data"]["rrd_size_bytes"] == 6

    sqlite_handler.storage_dir = tmp_path / "missing"
    sqlite_handler.storage_dir.mkdir()
    _attach_storage(
        api,
        SimpleNamespace(
            sqlite_handler=sqlite_handler,
            rrd_enabled=False,
            rrd_handler=None,
        ),
    )

    missing_rrd = api.db_stats()
    assert missing_rrd["success"] is True
    assert missing_rrd["data"]["rrd_enabled"] is False
    assert missing_rrd["data"]["rrd_available"] is False
    assert missing_rrd["data"]["metrics_data_source"] == "sqlite"
    assert missing_rrd["data"]["rrd_size_bytes"] == 0

    sqlite_handler.get_table_stats.side_effect = RuntimeError("stats failed")
    err = api.db_stats()
    assert err["success"] is False
    assert "stats failed" in err["error"]


def test_rrd_data_endpoint_succeeds_with_sqlite_metrics(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_metrics_data=MagicMock(
            return_value={
                "start_time": 0,
                "end_time": 60,
                "step": 60,
                "timestamps": [0, 60],
                "data_sources": ["rx_count"],
                "packet_types": {"type_0": [0, 0]},
                "metrics": {"rx_count": [1, 2]},
                "data_source": "sqlite",
                "counter_mode": "bucket_count",
            }
        )
    )
    _attach_storage(api, storage)

    out = api.rrd_data()

    assert out["success"] is True
    assert out["data"]["data_source"] == "sqlite"


def test_db_purge_validation_and_results(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api({"web": {"cors_enabled": True}})

    sqlite_handler = SimpleNamespace(
        purge_table=MagicMock(side_effect=[5, ValueError("bad table")])
    )
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    request.json = {}
    missing = api.db_purge()
    assert missing["success"] is False
    assert "Missing 'tables'" in missing["error"]

    request.json = {"tables": "nope"}
    bad_type = api.db_purge()
    assert bad_type["success"] is False
    assert "must be a list" in bad_type["error"]

    request.json = {"tables": ["packets", "invalid"]}
    result = api.db_purge()
    assert result["success"] is True
    assert result["data"]["packets"]["deleted"] == 5
    assert "bad table" in result["data"]["invalid"]["error"]


def test_db_purge_all_and_options(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": False}})

    request.method = "OPTIONS"
    assert api.db_purge() == ""

    request.method = "POST"
    request.json = {"tables": "all"}
    sqlite_handler = SimpleNamespace(
        purge_table=MagicMock(return_value=1),
        companion_journal_epoch_strict=MagicMock(return_value="new-epoch"),
    )
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_purge()
    assert result["success"] is True
    assert sqlite_handler.purge_table.call_count == 10
    sqlite_handler.companion_journal_epoch_strict.assert_called_once_with()
    assert result["journal_epoch"] == "new-epoch"


def test_db_purge_does_not_rotate_epoch_for_non_companion_tables(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {"tables": ["packets", "adverts"]}
    api = _make_api({"web": {"cors_enabled": False}})
    sqlite_handler = SimpleNamespace(
        purge_table=MagicMock(return_value=1),
        companion_journal_epoch_strict=MagicMock(),
    )
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_purge()

    assert result["success"] is True
    sqlite_handler.companion_journal_epoch_strict.assert_not_called()
    assert "journal_epoch" not in result


def test_db_vacuum_options_success_and_error(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.db_vacuum() == ""

    request.method = "POST"
    stat_values = [SimpleNamespace(st_size=1000), SimpleNamespace(st_size=700)]
    sqlite_path = SimpleNamespace(stat=MagicMock(side_effect=stat_values))
    sqlite_handler = SimpleNamespace(sqlite_path=sqlite_path, vacuum=MagicMock())
    _attach_storage(api, SimpleNamespace(sqlite_handler=sqlite_handler))

    result = api.db_vacuum()
    assert result["success"] is True
    assert result["data"] == {"size_before": 1000, "size_after": 700, "freed_bytes": 300}

    sqlite_path.stat = MagicMock(
        side_effect=[SimpleNamespace(st_size=700), SimpleNamespace(st_size=700)]
    )
    sqlite_handler.vacuum.side_effect = RuntimeError("vacuum failed")
    err = api.db_vacuum()
    assert err["success"] is False
    assert "vacuum failed" in err["error"]


def test_config_export_options_preflight(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "OPTIONS"
    api = _make_api({"web": {"cors_enabled": True}})

    assert api.config_export() == ""


def test_config_import_options_and_no_valid_sections(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.config_import() == ""

    request.method = "POST"
    _set_json_body(request, {"config": {"unknown_section": {"x": 1}}})
    result = api.config_import()
    assert result["success"] is False
    assert "No valid configuration sections" in result["error"]


def test_config_import_invalid_identity_key_hex_is_skipped(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    api = _make_api({"repeater": {"security": {}}})
    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True
    _set_json_body(
        request,
        {
            "config": {
                "repeater": {
                    "security": {"admin_password": "strong-password"},
                    "identity_key": "NOTHEX",
                }
            },
        },
    )

    result = api.config_import()

    assert result["success"] is True
    assert "identity_key" not in api.config["repeater"]


def test_validate_config_options_and_method_guard(cherrypy_ctx):
    request, response = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.validate_config() == ""

    request.method = "POST"
    with pytest.raises(cherrypy.HTTPError) as exc:
        api.validate_config()
    assert exc.value.status == 405
    assert response.status == 405
    assert response.headers["Allow"] == "GET"


def test_validate_config_reports_missing_file(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api({"web": {"cors_enabled": True}})
    api._config_path = str(tmp_path / "missing.yaml")

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    assert result["data"]["summary"]["error_count"] >= 1
    assert result["data"]["errors"][0]["path"] == "config"


def test_validate_config_reports_yaml_parse_error(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "bad.yaml")
    (tmp_path / "bad.yaml").write_text("repeater: [unterminated", encoding="utf-8")

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    assert any("YAML syntax error" in e["message"] for e in result["data"]["errors"])


def test_validate_config_valid_kiss_configuration(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
  node_name: mesh-node-01
  security:
    admin_password: supersecret
radio_type: kiss
radio:
  frequency: 869618000
  bandwidth: 62500
  spreading_factor: 8
  coding_rate: 5
  tx_power: 22
  preamble_length: 16
kiss:
  port: /dev/ttyUSB0
  baud_rate: 115200
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["summary"]["error_count"] == 0


def test_validate_config_disabled_radio_warns_but_valid(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
  node_name: mesh-node-02
  security:
    admin_password: supersecret
radio_type: none
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is True
    assert result["data"]["summary"]["warning_count"] >= 1
    assert any(w["path"] == "radio_type" for w in result["data"]["warnings"])


def test_update_web_config_options_no_updates_success_failure(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "OPTIONS"
    assert api.update_web_config() == ""

    request.method = "POST"
    request.json = {}
    no_updates = api.update_web_config()
    assert no_updates["success"] is False
    assert "No configuration updates" in no_updates["error"]

    request.json = {"web": {"cors_enabled": True}}
    api.config_manager.update_and_save.return_value = {"success": True, "saved": True}
    ok = api.update_web_config()
    assert ok["success"] is True
    assert ok["data"]["persisted"] is True
    api.config_manager.update_and_save.assert_called_with(
        updates={"web": {"cors_enabled": True}},
        live_update=False,
    )

    api.config_manager.update_and_save.return_value = {"success": False, "error": "bad"}
    fail = api.update_web_config()
    assert fail["success"] is False
    assert fail["error"] == "bad"


def test_update_web_config_requires_post_and_handles_exception(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"web": {"cors_enabled": True}})

    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError) as exc:
        api.update_web_config()
    assert exc.value.status == 405

    request.method = "POST"
    request.json = {"web": {"site_name": "mesh"}}
    api.config_manager.update_and_save.side_effect = RuntimeError("write failed")
    err = api.update_web_config()
    assert err["success"] is False
    assert "write failed" in err["error"]


def test_validate_config_top_level_must_be_mapping(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text("- list\n- not\n- mapping\n", encoding="utf-8")

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    assert any(
        e["message"].startswith("Top-level YAML value must be a mapping")
        for e in result["data"]["errors"]
    )


def test_validate_config_invalid_radio_type_and_missing_sections(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: ""
radio_type: weird_radio
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "repeater.node_name" in paths
    assert "repeater.security" in paths
    assert "radio_type" in paths


def test_validate_config_pymc_tcp_placeholder_and_bad_port(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: mesh-node-03
    security:
        admin_password: supersecret
radio_type: pymc_tcp
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
pymc_tcp:
    host: REPLACE_WITH_MODEM_HOST
    port: 70000
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "pymc_tcp.host" in paths
    assert "pymc_tcp.port" in paths


def test_validate_config_sx1262_ch341_missing_sections(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: mesh-node-04
    security:
        admin_password: supersecret
radio_type: sx1262_ch341
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "sx1262" in paths
    assert "ch341" in paths


def test_validate_config_rejects_bool_numeric_fields(cherrypy_ctx, tmp_path):
    """Booleans silently cast to int in Python, so this guards explicit type checks."""
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: mesh-node-bool
    security:
        admin_password: supersecret
radio_type: kiss
radio:
    frequency: 869618000
    bandwidth: true
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
kiss:
    port: /dev/ttyUSB0
    baud_rate: true
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    errors = {e["path"]: e["message"] for e in result["data"]["errors"]}
    assert "radio.bandwidth" in errors
    assert "kiss.baud_rate" in errors


def test_validate_config_radio_numeric_ranges_and_modes(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: mesh-node-ranges
    security:
        admin_password: supersecret
radio_type: sx1262
radio:
    frequency: 99
    bandwidth: 12345
    spreading_factor: 4
    coding_rate: 9
    tx_power: 31
    preamble_length: 0
sx1262:
    bus_id: 0
    cs_id: 0
    cs_pin: 8
    reset_pin: 25
    busy_pin: 24
    irq_pin: 16
    txen_pin: 18
    rxen_pin: 17
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "radio.frequency" in paths
    assert "radio.bandwidth" in paths
    assert "radio.spreading_factor" in paths
    assert "radio.coding_rate" in paths
    assert "radio.tx_power" in paths
    assert "radio.preamble_length" in paths


def test_validate_config_en_pins_type_and_entry_validation(cherrypy_ctx, tmp_path):
    request, _ = cherrypy_ctx
    request.method = "GET"
    api = _make_api()
    api._config_path = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(
        """
repeater:
    node_name: mesh-node-enpins
    security:
        admin_password: supersecret
radio_type: sx1262
radio:
    frequency: 869618000
    bandwidth: 62500
    spreading_factor: 8
    coding_rate: 5
    tx_power: 22
    preamble_length: 16
sx1262:
    bus_id: 0
    cs_id: 0
    cs_pin: 8
    reset_pin: 25
    busy_pin: 24
    irq_pin: 16
    txen_pin: 18
    rxen_pin: 17
    en_pins: [21, bad]
""".strip(),
        encoding="utf-8",
    )

    result = api.validate_config()

    assert result["success"] is True
    assert result["data"]["valid"] is False
    paths = {e["path"] for e in result["data"]["errors"]}
    assert "sx1262.en_pins[1]" in paths


def test_config_import_cors_change_requires_restart(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    api = _make_api({"web": {"site_name": "old"}})
    api._require_admin_on_public_route = MagicMock()
    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True
    _set_json_body(
        request,
        {"config": {"web": {"site_name": "new", "cors_enabled": True}}},
    )

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is True
    assert result["sections_updated"] == ["web"]
    assert api.config["web"]["site_name"] == "new"
    assert api.config["web"]["cors_enabled"] is True
    assert api.config["repeater"]["setup_complete"] is True

    request.headers.pop("Authorization")
    api._require_admin_on_public_route = MagicMock(
        side_effect=cherrypy.HTTPError(403, "Public bootstrap is closed")
    )
    _set_json_body(request, {"config": {"web": {"site_name": "second"}}})
    with pytest.raises(cherrypy.HTTPError, match="Public bootstrap is closed"):
        api.config_import()


def test_config_import_site_name_only_remains_live(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    api = _make_api({"web": {"site_name": "old"}})
    api._require_admin_on_public_route = MagicMock()
    api.config_manager.update_and_save.return_value = {"saved": True}
    _set_json_body(
        request,
        {"config": {"web": {"site_name": "new"}}},
    )

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is False
    assert api.config["web"]["site_name"] == "new"


def test_config_import_http_listener_change_defers_to_restart(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    api = _make_api(
        {
            "http": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8000,
            }
        }
    )
    api._require_admin_on_public_route = MagicMock()
    api.config_manager.update_and_save.return_value = {"saved": True}
    _set_json_body(
        request,
        {
            "config": {
                "http": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 9000,
                }
            }
        },
    )

    result = api.config_import()

    assert result["success"] is True
    assert result["restart_required"] is True
    assert api.config["http"]["port"] == 9000
    api.config_manager.update_and_save.assert_called_once_with(
        updates={},
        live_update=False,
        live_update_sections=[],
    )


def test_config_import_identity_redaction_preserves_by_name_for_room_servers(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    api = _make_api(
        {
            "identities": {
                "room_servers": [
                    {"name": "main-room", "identity_key": bytes.fromhex("ABCD")},
                ]
            }
        }
    )
    api._require_admin_on_public_route = MagicMock()
    api.config_manager.update_and_save.return_value = {"ok": True}
    api.config_manager.save_to_file.return_value = True
    _set_json_body(
        request,
        {
            "config": {
                "identities": {
                    "room_servers": [
                        {"name": "main-room", "identity_key": "*** REDACTED ***"},
                        {"name": "new-room", "identity_key": "*** REDACTED ***"},
                    ]
                }
            },
        },
    )

    result = api.config_import()

    assert result["success"] is True
    rooms = api.config["identities"]["room_servers"]
    by_name = {r["name"]: r["identity_key"] for r in rooms}
    assert by_name["main-room"] == bytes.fromhex("ABCD")
    # Unknown existing room keeps empty value when imported as redacted.
    assert by_name["new-room"] == ""


def test_config_import_rejects_listener_collision_without_partial_apply(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.headers["Authorization"] = "Bearer operator"
    original = {
        "http": {"enabled": True, "port": 5000},
        "web": {"site_name": "unchanged"},
    }
    api = _make_api(original)
    api._require_admin_on_public_route = MagicMock()
    _set_json_body(
        request,
        {
            "config": {
                "web": {"site_name": "must-not-apply"},
                "identities": {
                    "companions": [
                        {"name": "chat", "settings": {"tcp_port": 5000}},
                    ]
                },
            },
        },
    )

    result = api.config_import()

    assert result["success"] is False
    assert "conflicts with Repeater HTTP API" in result["error"]
    assert api.config == original
    api.config_manager.update_and_save.assert_not_called()
    api.config_manager.save_to_file.assert_not_called()


def test_stats_includes_versions_and_buildroot_image_info(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api({"radio_type": "sx1262", "web": {"site_name": "Field"}})
    api.stats_getter = lambda: {"uptime": 10}

    with patch(
        "repeater.web.api_endpoints.get_buildroot_image_info",
        return_value={"image_name": "pyMC", "image_version": "1.2.3"},
    ):
        out = api.stats()

    assert out["uptime"] == 10
    assert out["radio_type"] == "sx1262"
    assert out["site_name"] == "Field"
    assert out["version"]
    assert out["image_name"] == "pyMC"
    assert out["image_version"] == "1.2.3"


def test_gps_snapshot_when_service_present_and_default_when_absent(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api({"gps": {"enabled": True}})

    api.daemon_instance = SimpleNamespace(
        gps_service=SimpleNamespace(get_snapshot=lambda: {"running": True})
    )
    out = api.gps()
    assert out == {"success": True, "data": {"running": True}}

    api.daemon_instance = SimpleNamespace(gps_service=None)
    out2 = api.gps()
    assert out2["success"] is True
    assert out2["data"]["status"]["state"] == "disabled"


def test_check_pymc_console_and_mqtt_status_and_broker_presets(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.check_pymc_console() == ""

    request.method = "GET"
    with patch("os.path.isdir", return_value=True):
        out = api.check_pymc_console()
    assert out["success"] is True
    assert out["data"]["exists"] is True

    # mqtt status when no handler reachable
    api.daemon_instance = None
    status = api.mqtt_status()
    assert status["success"] is True
    assert status["data"]["handler_active"] is False

    # mqtt status with active connections
    conn = SimpleNamespace(
        enabled=True,
        broker={"name": "main", "host": "mqtt.local"},
        is_connected=lambda: True,
        has_pending_reconnect=lambda: False,
        format="json",
    )
    _attach_storage(api, SimpleNamespace(mqtt_handler=SimpleNamespace(connections=[conn])))
    status2 = api.mqtt_status()
    assert status2["success"] is True
    assert status2["data"]["brokers"][0]["name"] == "main"

    with (
        patch("repeater.presets.list_presets", return_value=["waev"]),
        patch(
            "repeater.presets.get_preset",
            return_value={
                "display_name": "Waev",
                "website": "https://waev.app",
                "brokers": [{"host": "h"}],
            },
        ),
    ):
        presets = api.broker_presets()
    assert presets["success"] is True
    assert presets["data"][0]["id"] == "waev"


def test_send_advert_paths(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.send_advert() == ""

    request.method = "POST"
    no_func = api.send_advert()
    assert no_func["success"] is False
    assert "not configured" in no_func["error"]

    api.send_advert_func = MagicMock()
    no_loop = api.send_advert()
    assert no_loop["success"] is False
    assert "Event loop not available" in no_loop["error"]

    api.event_loop = object()
    api.send_advert_func = MagicMock()
    fake_future = SimpleNamespace(result=lambda timeout: True)
    with patch("asyncio.run_coroutine_threadsafe", return_value=fake_future):
        ok = api.send_advert()
    assert ok == {"success": True, "data": "Advert sent successfully"}

    fake_future_fail = SimpleNamespace(result=lambda timeout: False)
    with patch("asyncio.run_coroutine_threadsafe", return_value=fake_future_fail):
        bad = api.send_advert()
    assert bad["success"] is False

    future_timeout = MagicMock()
    future_timeout.result.side_effect = FutureTimeoutError()
    with patch("asyncio.run_coroutine_threadsafe", return_value=future_timeout):
        timeout = api.send_advert()

    assert timeout["success"] is False
    assert "Timed out waiting for advert transmission" in timeout["error"]
    future_timeout.cancel.assert_called_once()


def test_room_server_advert_applies_default_region_scope():
    from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD

    api = _make_api({"mesh": {"default_region": "alpha"}, "repeater": {}})
    dispatcher = SimpleNamespace(send_packet=AsyncMock())
    api.daemon_instance = SimpleNamespace(dispatcher=dispatcher, repeater_handler=None)

    packet = SimpleNamespace(
        header=ROUTE_TYPE_FLOOD,
        transport_codes=[0, 0],
        get_payload_type=lambda: 3,
        get_payload=lambda: b"room_server_advert_payload",
    )

    with (
        patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet),
        patch("openhop_core.protocol.transport_keys.get_auto_key_for", return_value=b"\x01" * 16),
        patch("openhop_core.protocol.transport_keys.calc_transport_code", return_value=0xCAFE),
    ):
        result = asyncio.run(
            api._send_room_server_advert_async(
                identity=SimpleNamespace(),
                node_name="RoomAlpha",
                latitude=1.0,
                longitude=2.0,
                disable_fwd=False,
            )
        )

    assert result is True
    assert packet.transport_codes == [0xCAFE, 0]
    assert (packet.header & 0x03) == ROUTE_TYPE_TRANSPORT_FLOOD
    dispatcher.send_packet.assert_awaited_once_with(packet, wait_for_ack=False)


def test_room_server_advert_returns_false_when_send_rejected():
    from openhop_core.protocol.constants import ROUTE_TYPE_FLOOD

    api = _make_api({"mesh": {"default_region": "alpha"}, "repeater": {}})
    dispatcher = SimpleNamespace(send_packet=AsyncMock(return_value=False))
    api.daemon_instance = SimpleNamespace(dispatcher=dispatcher, repeater_handler=None)

    packet = SimpleNamespace(
        header=ROUTE_TYPE_FLOOD,
        transport_codes=[0, 0],
        get_payload_type=lambda: 3,
        get_payload=lambda: b"room_server_advert_payload",
    )

    with (
        patch("openhop_core.protocol.PacketBuilder.create_advert", return_value=packet),
        patch("openhop_core.protocol.transport_keys.get_auto_key_for", return_value=b"\x01" * 16),
        patch("openhop_core.protocol.transport_keys.calc_transport_code", return_value=0xCAFE),
    ):
        result = asyncio.run(
            api._send_room_server_advert_async(
                identity=SimpleNamespace(),
                node_name="RoomAlpha",
                latitude=1.0,
                longitude=2.0,
                disable_fwd=False,
            )
        )

    assert result is False
    dispatcher.send_packet.assert_awaited_once_with(packet, wait_for_ack=False)


def test_set_mode_and_set_duty_cycle_paths(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"repeater": {}, "duty_cycle": {}})

    request.method = "OPTIONS"
    assert api.set_mode() == ""
    assert api.set_duty_cycle() == ""

    request.method = "POST"
    request.json = {"mode": "invalid"}
    invalid = api.set_mode()
    assert invalid["success"] is False

    request.json = {"mode": "monitor"}
    ok = api.set_mode()
    assert ok == {"success": True, "mode": "monitor"}
    assert api.config["repeater"]["mode"] == "monitor"

    request.json = {"enabled": False}
    duty = api.set_duty_cycle()
    assert duty == {"success": True, "enabled": False}
    assert api.config["duty_cycle"]["enforcement_enabled"] is False


def test_update_duty_cycle_config_branches(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"duty_cycle": {}})

    request.method = "OPTIONS"
    assert api.update_duty_cycle_config() == ""

    request.method = "POST"
    request.json = {"max_airtime_percent": 0.01}
    bad = api.update_duty_cycle_config()
    assert bad["success"] is False
    assert "0.1-100.0" in bad["error"]

    request.json = {}
    none = api.update_duty_cycle_config()
    assert none["success"] is False
    assert "No valid settings" in none["error"]

    request.json = {"max_airtime_percent": 10, "enforcement_enabled": True}
    api.config_manager.update_and_save.return_value = {"saved": False, "error": "disk"}
    save_fail = api.update_duty_cycle_config()
    assert save_fail["success"] is False
    assert "disk" in save_fail["error"]

    api.config_manager.update_and_save.return_value = {"saved": True, "live_updated": True}
    ok = api.update_duty_cycle_config()
    assert ok["success"] is True
    assert ok["data"]["persisted"] is True
    assert api.config["duty_cycle"]["max_airtime_per_minute"] == 6000


def test_update_advert_rate_limit_config_branches(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"repeater": {}})

    request.method = "OPTIONS"
    assert api.update_advert_rate_limit_config() == ""

    request.method = "POST"
    request.json = {}
    none = api.update_advert_rate_limit_config()
    assert none["success"] is False
    assert "No valid settings" in none["error"]

    request.json = {
        "rate_limit_enabled": True,
        "bucket_capacity": 0,
        "refill_tokens": 0,
        "refill_interval_seconds": 1,
        "min_interval_seconds": -10,
        "penalty_enabled": True,
        "violation_threshold": 0,
        "violation_decay_seconds": 1,
        "base_penalty_seconds": 1,
        "penalty_multiplier": 0.1,
        "max_penalty_seconds": 1,
        "adaptive_enabled": True,
        "ewma_alpha": 99,
        "hysteresis_seconds": -1,
        "quiet_max": 0.05,
        "normal_max": 0.2,
        "busy_max": 0.5,
    }
    api.config_manager.update_and_save.return_value = {"saved": True, "live_updated": False}
    ok = api.update_advert_rate_limit_config()
    assert ok["success"] is True
    rate = api.config["repeater"]["advert_rate_limit"]
    pen = api.config["repeater"]["advert_penalty_box"]
    ad = api.config["repeater"]["advert_adaptive"]
    assert rate["bucket_capacity"] == 1
    assert rate["refill_tokens"] == 1
    assert rate["refill_interval_seconds"] == 60
    assert rate["min_interval_seconds"] == 0
    assert pen["violation_threshold"] == 1
    assert pen["base_penalty_seconds"] == 60
    assert pen["penalty_multiplier"] == 1.0
    assert pen["max_penalty_seconds"] == 60
    assert ad["ewma_alpha"] == 1.0
    assert ad["hysteresis_seconds"] == 0
    assert ad["thresholds"]["quiet_max"] == 0.05


def test_logs_hardware_stats_and_hardware_processes(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    with patch("repeater.web.http_server._log_buffer", SimpleNamespace(logs=[])):
        logs = api.logs()
    assert "logs" in logs
    assert logs["logs"][0]["message"] == "No logs available"

    storage = SimpleNamespace(
        get_hardware_stats=MagicMock(return_value={"cpu": 10}),
        get_hardware_processes=MagicMock(return_value=[{"pid": 1}]),
    )
    _attach_storage(api, storage)

    hs = api.hardware_stats()
    hp = api.hardware_processes()
    assert hs == {"success": True, "data": {"cpu": 10}}
    assert hp == {"success": True, "data": [{"pid": 1}]}

    storage.get_hardware_stats.return_value = None
    assert api.hardware_stats()["success"] is False

    storage.get_hardware_processes.return_value = None
    assert api.hardware_processes()["success"] is False


def test_noise_floor_and_crc_endpoints(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_noise_floor_history=MagicMock(return_value=[{"v": -110}]),
        get_noise_floor_stats=MagicMock(return_value={"avg": -105}),
        get_noise_floor_rrd=MagicMock(return_value=[[1, -100]]),
        get_crc_error_count=MagicMock(return_value=3),
        get_crc_error_history=MagicMock(return_value=[{"id": 1}]),
    )
    _attach_storage(api, storage)

    h = api.noise_floor_history(hours="24", limit="5")
    s = api.noise_floor_stats(hours="12")
    c = api.noise_floor_chart_data(hours="2")
    cc = api.crc_error_count(hours="6")
    ch = api.crc_error_history(hours="6", limit="10")

    assert h["success"] is True and h["data"]["count"] == 1
    assert s["success"] is True and s["data"]["stats"]["avg"] == -105
    assert c["success"] is True and c["data"]["chart_data"] == [[1, -100]]
    assert cc["success"] is True and cc["data"]["crc_error_count"] == 3
    assert ch["success"] is True and ch["data"]["count"] == 1

    err = api.crc_error_count(hours="bad")
    assert err["success"] is False


def test_metrics_graph_data_includes_policy_events(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_metrics_data=MagicMock(
            return_value={
                "start_time": 100,
                "end_time": 220,
                "step": 60,
                "timestamps": [100, 160, 220],
                "metrics": {
                    "rx_count": [10, 12, 15],
                    "tx_count": [8, 9, 11],
                },
            }
        ),
        get_policy_event_counts=MagicMock(
            return_value=[
                {"timestamp": 60, "count": 1},
                {"timestamp": 120, "count": 3},
                {"timestamp": 180, "count": 2},
            ]
        ),
    )
    _attach_storage(api, storage)

    out = api.metrics_graph_data(hours="24", resolution="average", metrics="rx_count,policy_events")
    assert out["success"] is True

    series_by_type = {item["type"]: item for item in out["data"]["series"]}
    assert "rx_count" in series_by_type
    assert "policy_events" in series_by_type
    assert series_by_type["policy_events"]["name"] == "Policy Events"
    assert series_by_type["policy_events"]["data"] == [[100000, 1], [160000, 3], [220000, 2]]
    assert out["data"]["data_source"] == "rrd"


def test_metrics_graph_data_uses_sqlite_bucket_counts_without_counter_delta(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_metrics_data=MagicMock(
            return_value={
                "start_time": 0,
                "end_time": 60,
                "step": 60,
                "timestamps": [0, 60],
                "metrics": {
                    "rx_count": [2, 4],
                    "avg_rssi": [None, -80.0],
                },
                "packet_types": {},
                "data_source": "sqlite",
                "counter_mode": "bucket_count",
            }
        ),
        get_policy_event_counts=MagicMock(return_value=[]),
    )
    _attach_storage(api, storage)

    out = api.metrics_graph_data(hours="24", resolution="average", metrics="rx_count,avg_rssi")

    assert out["success"] is True
    series_by_type = {item["type"]: item for item in out["data"]["series"]}
    assert series_by_type["rx_count"]["data"] == [[0, 2], [60000, 4]]
    assert series_by_type["avg_rssi"]["data"] == [[0, 0], [60000, -80.0]]
    assert out["data"]["data_source"] == "sqlite"


def test_metrics_graph_data_empty_sqlite_payload_is_successful(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    storage = SimpleNamespace(
        get_metrics_data=MagicMock(
            return_value={
                "start_time": 0,
                "end_time": 0,
                "step": 60,
                "timestamps": [0],
                "metrics": {
                    "rx_count": [0],
                    "tx_count": [0],
                    "drop_count": [0],
                    "avg_rssi": [None],
                    "avg_snr": [None],
                    "avg_length": [None],
                    "avg_score": [None],
                    "neighbor_count": [None],
                },
                "packet_types": {},
                "data_source": "sqlite",
                "counter_mode": "bucket_count",
            }
        ),
        get_policy_event_counts=MagicMock(return_value=[]),
    )
    _attach_storage(api, storage)

    out = api.metrics_graph_data(hours="24", resolution="average", metrics="rx_count")

    assert out["success"] is True
    assert out["data"]["series"][0]["data"] == [[0, 0]]
    assert out["data"]["data_source"] == "sqlite"


def test_lbt_diagnostics_aligns_with_rrd_and_returns_correlations(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    storage = SimpleNamespace(
        get_lbt_diagnostics=MagicMock(
            return_value={
                "start_time": 60,
                "end_time": 240,
                "bucket_seconds": 60,
                "summary": {
                    "total_transmissions": 10,
                    "total_attempts": 14,
                    "first_attempt_success": 7,
                    "retry_packets": 3,
                    "retry_rate_pct": 30.0,
                    "first_attempt_success_rate_pct": 70.0,
                    "avg_attempts": 1.4,
                    "median_attempts": 1.0,
                    "p95_attempts": 3.0,
                    "max_attempts": 4,
                    "attempts_1": 7,
                    "attempts_2": 2,
                    "attempts_3": 1,
                    "attempts_4_plus": 0,
                    "attempts_3_plus": 1,
                    "attempts_3_plus_pct": 10.0,
                    "attempts_4_plus_pct": 0.0,
                    "failed_transmissions": 0,
                    "busy_channel_events": 3,
                    "severe_contention_count": 0,
                    "severe_contention_pct": 0.0,
                    "severe_attempt_threshold": 4,
                    "has_lbt_data": True,
                    "worst_bucket": {
                        "timestamp": 120,
                        "retry_rate_pct": 50.0,
                        "attempts_3_plus_pct": 20.0,
                        "max_attempts": 3,
                        "transmissions": 5,
                    },
                },
                "buckets": [
                    {
                        "timestamp": 60,
                        "transmissions": 3,
                        "total_attempts": 3,
                        "first_attempt_success": 3,
                        "retry_packets": 0,
                        "retry_rate_pct": 0.0,
                        "first_attempt_success_rate_pct": 100.0,
                        "avg_attempts": 1.0,
                        "median_attempts": 1.0,
                        "p95_attempts": 1.0,
                        "max_attempts": 1,
                        "attempts_1": 3,
                        "attempts_2": 0,
                        "attempts_3": 0,
                        "attempts_4_plus": 0,
                        "attempts_3_plus": 0,
                        "attempts_3_plus_pct": 0.0,
                        "attempts_4_plus_pct": 0.0,
                        "failed_transmissions": 0,
                        "busy_channel_events": 0,
                        "severe_contention_count": 0,
                        "severe_contention_pct": 0.0,
                    },
                    {
                        "timestamp": 120,
                        "transmissions": 5,
                        "total_attempts": 8,
                        "first_attempt_success": 2,
                        "retry_packets": 3,
                        "retry_rate_pct": 60.0,
                        "first_attempt_success_rate_pct": 40.0,
                        "avg_attempts": 1.6,
                        "median_attempts": 2.0,
                        "p95_attempts": 3.0,
                        "max_attempts": 3,
                        "attempts_1": 2,
                        "attempts_2": 2,
                        "attempts_3": 1,
                        "attempts_4_plus": 0,
                        "attempts_3_plus": 1,
                        "attempts_3_plus_pct": 20.0,
                        "attempts_4_plus_pct": 0.0,
                        "failed_transmissions": 0,
                        "busy_channel_events": 3,
                        "severe_contention_count": 0,
                        "severe_contention_pct": 0.0,
                    },
                ],
                "packet_types": [
                    {
                        "packet_type": 1,
                        "packet_type_label": "Response (RESPONSE)",
                        "transmissions": 8,
                        "retry_packets": 3,
                        "retry_rate_pct": 37.5,
                    }
                ],
                "packet_type_buckets": [
                    {
                        "timestamp": 120,
                        "packet_type": 1,
                        "packet_type_label": "Response (RESPONSE)",
                        "transmissions": 5,
                        "total_attempts": 8,
                        "first_attempt_success": 2,
                        "retry_packets": 3,
                        "retry_rate_pct": 60.0,
                        "first_attempt_success_rate_pct": 40.0,
                        "avg_attempts": 1.6,
                        "attempts_1": 2,
                        "attempts_2": 2,
                        "attempts_3": 1,
                        "attempts_4_plus": 0,
                        "attempts_3_plus": 1,
                        "attempts_3_plus_pct": 20.0,
                        "max_attempts": 3,
                        "failed_transmissions": 0,
                        "severe_contention_count": 0,
                    }
                ],
            }
        ),
        get_rrd_data=MagicMock(
            return_value={
                "timestamps": [100, 160, 220],
                "metrics": {
                    "rx_count": [100, 110, 125],
                    "tx_count": [50, 55, 70],
                    "drop_count": [10, 11, 14],
                    "avg_rssi": [-80.0, -90.0, -95.0],
                    "avg_snr": [8.0, 2.0, -1.0],
                },
            }
        ),
    )
    _attach_storage(api, storage)

    out = api.lbt_diagnostics(hours="24", bucket_seconds="60", severe_attempt_threshold="4")
    assert out["success"] is True
    assert out["data"]["bucket_seconds"] == 60
    assert out["data"]["severe_attempt_threshold"] == 4

    buckets = out["data"]["buckets"]
    assert len(buckets) == 2
    assert "rf" in buckets[0]
    assert buckets[0]["rf"]["traffic_volume"] >= 0
    assert buckets[0]["rf"]["avg_snr"] is not None

    correlations = out["data"]["correlations"]
    assert "retry_rate_vs_avg_snr" in correlations
    assert "retry_rate_vs_traffic_volume" in correlations
    assert "sample_count" in correlations["retry_rate_vs_avg_snr"]

    assert out["data"]["packet_types"][0]["packet_type"] == 1
    assert out["data"]["packet_type_buckets"][0]["packet_type_label"] == "Response (RESPONSE)"
    assert "rf" in out["data"]["packet_type_buckets"][0]


def test_lbt_diagnostics_rejects_unbounded_large_time_range(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()
    _attach_storage(api, SimpleNamespace())

    out = api.lbt_diagnostics(start_timestamp="0", end_timestamp=str(200 * 3600))
    assert out["success"] is False
    assert "Max range" in out["error"]


def test_advert_contact_and_rate_limit_stats_endpoints(cherrypy_ctx):
    del cherrypy_ctx
    api = _make_api()

    miss = api.adverts_by_contact_type()
    assert miss["success"] is False

    storage = SimpleNamespace(
        sqlite_handler=SimpleNamespace(
            get_adverts_by_contact_type=MagicMock(return_value=[{"id": 1}]),
            get_adverts_count_by_contact_type=MagicMock(return_value=7),
        )
    )
    _attach_storage(api, storage)

    out = api.adverts_by_contact_type(contact_type="room_server", limit="2", offset="0", hours="24")
    count = api.adverts_count_by_contact_type(contact_type="room_server", hours="24")
    assert out["success"] is True and out["count"] == 1
    assert count["success"] is True and count["data"]["count"] == 7

    bad_fmt = api.adverts_count_by_contact_type(contact_type="room_server", hours="bad")
    assert bad_fmt["success"] is False

    no_daemon = api.advert_rate_limit_stats()
    assert no_daemon["success"] is False

    api.daemon_instance = SimpleNamespace(advert_helper=None)
    no_helper = api.advert_rate_limit_stats()
    assert no_helper["success"] is False

    api.daemon_instance = SimpleNamespace(advert_helper=SimpleNamespace())
    no_method = api.advert_rate_limit_stats()
    assert no_method["success"] is False

    api.daemon_instance = SimpleNamespace(
        advert_helper=SimpleNamespace(get_rate_limit_stats=lambda: {"tier": "normal"})
    )
    ok = api.advert_rate_limit_stats()
    assert ok == {"success": True, "data": {"tier": "normal"}}


def test_transport_keys_and_transport_key_and_unscoped_policy(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api({"mesh": {}})
    storage = SimpleNamespace(
        get_transport_keys=MagicMock(return_value=[{"id": 1}]),
        create_transport_key=MagicMock(return_value=10),
        get_transport_key_by_id=MagicMock(return_value={"id": 1}),
        update_transport_key=MagicMock(return_value=True),
        delete_transport_key=MagicMock(return_value=True),
    )
    _attach_storage(api, storage)

    request.method = "GET"
    keys = api.transport_keys()
    assert keys["success"] is True and keys["count"] == 1

    request.method = "POST"
    request.json = {"name": "", "flood_policy": "allow"}
    assert api.transport_keys()["success"] is False

    request.json = {"name": "k1", "flood_policy": "invalid"}
    assert api.transport_keys()["success"] is False

    request.json = {"name": "k1", "flood_policy": "allow", "last_used": "bad"}
    created = api.transport_keys()
    assert created["success"] is True

    request.method = "GET"
    assert api.transport_key("x")["success"] is False
    assert api.transport_key("1")["success"] is True

    request.method = "PUT"
    request.json = {"flood_policy": "maybe"}
    assert api.transport_key("1")["success"] is False

    request.json = {"name": "new", "flood_policy": "deny", "last_used": "not-ts"}
    updated = api.transport_key("1")
    assert updated["success"] is True

    request.method = "DELETE"
    deleted = api.transport_key("1")
    assert deleted["success"] is True

    request.method = "GET"
    assert api.unscoped_flood_policy()["success"] is False

    got_default = api.default_region()
    assert got_default["success"] is True
    assert got_default["data"]["default_region"] is None

    request.method = "POST"
    request.json = {}
    assert api.unscoped_flood_policy()["success"] is False

    request.json = {}
    assert api.default_region()["success"] is False

    request.json = {"unscoped_flood_allow": "yes"}
    assert api.unscoped_flood_policy()["success"] is False

    request.json = {"default_region": "alpha"}
    set_default = api.default_region()
    assert set_default["success"] is True
    assert api.config["mesh"]["default_region"] == "alpha"
    assert any(
        call.args and call.args[0] == "alpha" and call.args[1] == "allow"
        for call in storage.create_transport_key.call_args_list
    )

    request.method = "GET"
    got_default_after = api.default_region()
    assert got_default_after["success"] is True
    assert got_default_after["data"]["default_region"] == "alpha"

    request.method = "POST"
    request.json = {"default_region": None}
    clear_default = api.default_region()
    assert clear_default["success"] is True
    assert api.config["mesh"]["default_region"] is None

    api.config_manager.save_to_file.return_value = True
    request.json = {"unscoped_flood_allow": True}
    ok = api.unscoped_flood_policy()
    assert ok["success"] is True
    assert api.config["mesh"]["unscoped_flood_allow"] is True


def test_update_radio_config_owner_info_and_mesh_fields(cherrypy_ctx):
    request, _ = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "owner_info": "Alice|Ops",
        "path_hash_mode": 2,
        "loop_detect": "strict",
    }

    api = _make_api({"repeater": {}, "mesh": {}, "radio": {}, "delays": {}})
    api.config_manager.update_and_save.return_value = {
        "success": True,
        "saved": True,
        "live_updated": True,
    }

    out = api.update_radio_config()

    assert out["success"] is True
    assert api.config["repeater"]["owner_info"] == "Alice\nOps"
    assert api.config["mesh"]["path_hash_mode"] == 2
    assert api.config["mesh"]["loop_detect"] == "strict"
    assert "owner.info" in out["data"].get("applied", [])


class _FakeIdentityObj:
    def __init__(self, first=0x42):
        self._pk = bytes([first]) + (b"A" * 31)

    def get_public_key(self):
        return self._pk

    def get_address_bytes(self):
        return b"\x12\x34"


class _FakeClient:
    def __init__(self, key_hex: str, admin: bool):
        self.id = SimpleNamespace(get_public_key=lambda: bytes.fromhex(key_hex))
        self._admin = admin
        self.last_activity = 1.0
        self.last_login_success = 2.0
        self.last_timestamp = 3.0

    def is_admin(self):
        return self._admin


class _FakeACL:
    def __init__(self, clients, admin_password="a", guest_password="g"):
        self._clients = list(clients)
        self.max_clients = 10
        self.admin_password = admin_password
        self.guest_password = guest_password
        self.allow_read_only = True

    def get_num_clients(self):
        return len(self._clients)

    def get_all_clients(self):
        return list(self._clients)

    def remove_client(self, pubkey):
        before = len(self._clients)
        self._clients = [c for c in self._clients if c.id.get_public_key() != pubkey]
        return len(self._clients) < before


def test_identity_endpoints_paths(cherrypy_ctx):
    request, response = cherrypy_ctx
    api = _make_api({"identities": {"room_servers": [], "companions": []}})

    request.method = "OPTIONS"
    assert api.identities() == ""

    request.method = "GET"
    assert api.identities()["success"] is False

    id_mgr = SimpleNamespace(
        list_identities=lambda: [{"name": "room_server:main", "hash": "0x42", "address": "1234"}],
        get_identities_by_type=lambda t: (
            [("main", _FakeIdentityObj(0x42), {"settings": {"x": 1}})]
            if t == "room_server"
            else [("comp1", _FakeIdentityObj(0x51), {"settings": {"tcp_port": 5000}})]
        ),
        get_identity_by_name=lambda n: (
            (_FakeIdentityObj(0x42), {}, "room_server") if n == "main" else None
        ),
        named_identities={"comp1": 1, "main": 1},
    )
    api.daemon_instance = SimpleNamespace(identity_manager=id_mgr)
    api.config = {
        "identities": {
            "room_servers": [{"name": "main", "identity_key": "a" * 64, "settings": {"x": 1}}],
            "companions": [
                {"name": "comp1", "identity_key": "b" * 64, "settings": {"tcp_port": 5000}}
            ],
        }
    }
    api.config_manager.save_to_file.return_value = True

    ids = api.identities()
    assert ids["success"] is True
    assert ids["data"]["total_configured"] == 1
    assert ids["data"]["total_configured_companions"] == 1
    assert "identity_key" not in str(ids)

    assert api.identity()["success"] is False
    assert api.identity(name="missing")["success"] is False
    one = api.identity(name="main")
    assert one["success"] is True
    assert one["data"]["registered"] is False
    assert one["data"]["runtime"]["registered"] is True
    assert one["data"]["runtime"]["matches_configuration"] is False
    assert "identity_key" not in one["data"]
    assert "runtime" not in api.config["identities"]["room_servers"][0]

    # create identity validation + success path
    request.method = "POST"
    request.json = {}
    assert api.create_identity()["success"] is False
    request.json = {"name": "x", "type": "invalid"}
    assert api.create_identity()["success"] is False
    request.json = {
        "name": "x",
        "type": "room_server",
        "settings": {"admin_password": "p", "guest_password": "p"},
    }
    assert api.create_identity()["success"] is False
    request.json = {"name": "comp1", "type": "companion", "identity_key": "aa" * 32}
    assert api.create_identity()["success"] is False
    request.json = {
        "name": "bad/name",
        "type": "companion",
        "identity_key": "aa" * 32,
    }
    assert api.create_identity()["success"] is False
    assert response.status == 400
    response.status = 200
    request.json = {
        "name": "new-port",
        "type": "companion",
        "identity_key": "aa" * 32,
        "settings": {"tcp_port": "5000"},
    }
    assert api.create_identity()["success"] is False
    assert response.status == 400
    response.status = 200
    request.json = {
        "name": "new-timeout",
        "type": "companion",
        "identity_key": "aa" * 32,
        "settings": {"tcp_timeout": -1},
    }
    assert api.create_identity()["success"] is False
    assert response.status == 400
    response.status = 200

    request.json = {
        "name": "new-comp",
        "type": "companion",
        "identity_key": "cc" * 32,
        "settings": {
            "node_name": "N",
            "tcp_port": 5001,
            "max_contacts": 100_000,
            "offline_queue_size": 0,
            "trim_contacts_on_overflow": True,
            "rf_reception_events": True,
        },
    }
    api.event_loop = object()
    api.daemon_instance = SimpleNamespace(add_companion_from_config=MagicMock())
    with patch(
        "asyncio.run_coroutine_threadsafe",
        return_value=SimpleNamespace(result=lambda timeout: True),
    ):
        created = api.create_identity()
    assert created["success"] is True
    assert "identity_key" not in created["data"]
    assert created["data"]["activation_pending"] is False
    created_settings = next(
        companion["settings"]
        for companion in api.config["identities"]["companions"]
        if companion["name"] == "new-comp"
    )
    assert created_settings["max_contacts"] == 100_000
    assert created_settings["offline_queue_size"] == 0
    assert created_settings["frame_enabled"] is True
    assert created_settings["trim_contacts_on_overflow"] is True
    assert created_settings["rf_reception_events"] is True

    request.json = {
        "name": "normalized-key",
        "type": "companion",
        "identity_key": f"  0X{'AB' * 32}  ",
        "settings": {"tcp_port": 5002},
    }
    with patch(
        "asyncio.run_coroutine_threadsafe",
        return_value=SimpleNamespace(result=lambda timeout: True),
    ):
        normalized = api.create_identity()
    assert normalized["success"] is True
    assert (
        next(
            companion["identity_key"]
            for companion in api.config["identities"]["companions"]
            if companion["name"] == "normalized-key"
        )
        == "AB" * 32
    )

    response.status = 200
    request.json = {
        "name": "embedded-space-key",
        "type": "companion",
        "identity_key": ("AB" * 16) + " " + ("CD" * 16),
    }
    assert api.create_identity()["success"] is False
    assert response.status == 400

    response.status = 200
    request.json = {
        "name": "invalid-key-type",
        "type": "companion",
        "identity_key": False,
    }
    assert api.create_identity()["success"] is False
    assert response.status == 400

    # update identity method guard and room_server success
    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError):
        api.update_identity()
    request.method = "PUT"
    request.json = {
        "name": "new-comp",
        "type": "companion",
        "new_name": "bad:name",
    }
    assert api.update_identity()["success"] is False
    assert response.status == 400
    response.status = 200
    request.json = {
        "name": "new-comp",
        "type": "companion",
        "identity_key": "dd" * 32,
    }
    assert api.update_identity()["success"] is False
    assert response.status == 400
    assert (
        next(
            companion["identity_key"]
            for companion in api.config["identities"]["companions"]
            if companion["name"] == "new-comp"
        )
        == "cc" * 32
    )
    response.status = 200
    request.json = {
        "name": "new-comp",
        "type": "companion",
        "settings": {"tcp_port": 70_000},
    }
    assert api.update_identity()["success"] is False
    assert response.status == 400
    response.status = 200
    request.json = {
        "name": "new-comp",
        "type": "companion",
        "new_name": "renamed-comp",
        "settings": {"tcp_timeout": -1},
    }
    assert api.update_identity()["success"] is False
    assert response.status == 400
    assert any(
        companion["name"] == "new-comp" for companion in api.config["identities"]["companions"]
    )
    assert not any(
        companion["name"] == "renamed-comp" for companion in api.config["identities"]["companions"]
    )
    response.status = 200
    api.daemon_instance = None
    request.json = {"name": "main", "settings": {"node_name": "updated"}}
    upd = api.update_identity()
    assert upd["success"] is True

    # delete identity paths
    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError):
        api.delete_identity(name="main")
    request.method = "DELETE"
    assert api.delete_identity(name="", type="room_server")["success"] is False
    deleted = api.delete_identity(name="main", type="room_server")
    assert deleted["success"] is True

    # companion delete
    api.config["identities"]["companions"] = [{"name": "comp1", "identity_key": "11" * 32}]
    remove_companion = AsyncMock(return_value=True)
    api.daemon_instance = SimpleNamespace(
        identity_manager=id_mgr,
        remove_companion=remove_companion,
    )
    api.event_loop = object()

    def _completed(coro, _loop):
        coro.close()
        return SimpleNamespace(result=lambda timeout: True)

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_completed):
        d2 = api.delete_identity(name="comp1", type="companion")
    assert d2["success"] is True
    assert d2["data"]["deactivated"] is True
    remove_companion.assert_called_once_with(
        "comp1",
        identity_key="11" * 32,
    )
    assert response.status in (200, 405)


def test_delete_companion_timeout_leaves_live_deactivation_running(
    cherrypy_ctx,
):
    request, _response = cherrypy_ctx
    request.method = "DELETE"
    api = _make_api(
        {
            "identities": {
                "room_servers": [],
                "companions": [
                    {
                        "name": "slow-companion",
                        "identity_key": "12" * 32,
                    }
                ],
            }
        }
    )
    api.config_manager.save_to_file.return_value = True
    remove_companion = AsyncMock()
    api.daemon_instance = SimpleNamespace(remove_companion=remove_companion)
    api.event_loop = object()

    slow_future = MagicMock()
    slow_future.result.side_effect = FutureTimeoutError()

    def _scheduled(coro, _loop):
        coro.close()
        return slow_future

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_scheduled):
        result = api.delete_identity(
            name="slow-companion",
            type="companion",
        )

    assert result["success"] is True
    assert result["data"] == {
        "name": "slow-companion",
        "deactivated": False,
        "deactivation_pending": True,
        "restart_required": True,
    }
    assert "still finishing" in result["message"]
    slow_future.cancel.assert_not_called()
    slow_future.add_done_callback.assert_called_once()
    remove_companion.assert_called_once_with(
        "slow-companion",
        identity_key="12" * 32,
    )


def test_generated_identity_key_never_enters_logs_or_response(
    cherrypy_ctx,
    caplog,
):
    request, _response = cherrypy_ctx
    request.method = "POST"
    request.json = {"name": "generated-comp", "type": "companion"}
    api = _make_api({"identities": {"room_servers": [], "companions": []}})
    api.config_manager.save_to_file.return_value = True
    private_key = b"\xde" * 64

    with (
        patch(
            "repeater.keygen.generate_meshcore_keypair",
            return_value=(b"\x00" * 32, private_key),
        ),
        caplog.at_level("INFO"),
    ):
        result = api.create_identity()

    assert result["success"] is True
    assert "identity_key" not in result["data"]
    logged = caplog.text.lower()
    assert private_key.hex() not in logged
    assert private_key[:8].hex() not in logged


def test_create_companion_passes_identity_type_to_conflict_validation(cherrypy_ctx):
    request, _response = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "name": "typed-companion",
        "type": "companion",
        "identity_key": "ae" * 32,
        "settings": {"frame_enabled": False},
    }
    api = _make_api({"identities": {"room_servers": [], "companions": []}})
    api.config_manager.save_to_file.return_value = True
    registration_error = MagicMock(return_value=None)
    api.daemon_instance = SimpleNamespace(
        identity_manager=SimpleNamespace(registration_error=registration_error),
        add_companion_from_config=MagicMock(),
    )
    api.event_loop = object()

    with patch(
        "asyncio.run_coroutine_threadsafe",
        return_value=SimpleNamespace(result=lambda timeout: True),
    ):
        result = api.create_identity()

    assert result["success"] is True
    assert registration_error.call_args.args[0] == "typed-companion"
    assert registration_error.call_args.args[2] == "companion"


def test_create_companion_reports_durable_namespace_collision(cherrypy_ctx):
    from repeater.data_acquisition.sqlite_handler import (
        CompanionNamespaceCollisionError,
    )

    request, _response = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "name": "colliding-companion",
        "type": "companion",
        "identity_key": "ab" * 32,
        "settings": {"tcp_port": 5001},
    }
    api = _make_api({"identities": {"room_servers": [], "companions": []}})
    api.config_manager.save_to_file.return_value = True
    api.event_loop = object()
    api.daemon_instance = SimpleNamespace(
        add_companion_from_config=MagicMock(),
    )
    collision = CompanionNamespaceCollisionError(
        "Companion namespace 0xab is already bound; refusing activation"
    )
    future = SimpleNamespace(result=MagicMock(side_effect=collision))

    with patch("asyncio.run_coroutine_threadsafe", return_value=future):
        result = api.create_identity()

    assert result["success"] is True
    assert "created, but not activated" in result["message"]
    assert "already bound" in result["message"]
    assert "refusing activation" in result["message"]


def test_create_companion_reports_frame_listener_activation_failure(cherrypy_ctx):
    from repeater.identity_manager import IdentityConfigurationError

    request, _response = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "name": "frame-client",
        "type": "companion",
        "identity_key": "ac" * 32,
        "settings": {"tcp_port": 5001},
    }
    api = _make_api({"identities": {"room_servers": [], "companions": []}})
    api.config_manager.save_to_file.return_value = True
    api.event_loop = object()
    api.daemon_instance = SimpleNamespace(
        add_companion_from_config=MagicMock(),
    )
    activation_error = IdentityConfigurationError(
        "Companion 'frame-client' Frame listener failed to start "
        "on 127.0.0.1:5001: address already in use"
    )
    future = SimpleNamespace(result=MagicMock(side_effect=activation_error))

    with patch("asyncio.run_coroutine_threadsafe", return_value=future):
        result = api.create_identity()

    assert result["success"] is True
    assert "created, but not activated" in result["message"]
    assert "Frame listener failed to start" in result["message"]
    assert "address already in use" in result["message"]


def test_create_companion_timeout_leaves_live_activation_running(
    cherrypy_ctx,
):
    request, _response = cherrypy_ctx
    request.method = "POST"
    request.json = {
        "name": "slow-companion",
        "type": "companion",
        "identity_key": "ad" * 32,
        "settings": {"frame_enabled": False},
    }
    api = _make_api({"identities": {"room_servers": [], "companions": []}})
    api.config_manager.save_to_file.return_value = True
    add_companion = MagicMock()
    api.daemon_instance = SimpleNamespace(
        add_companion_from_config=add_companion,
    )
    api.event_loop = object()

    slow_future = MagicMock()
    slow_future.result.side_effect = FutureTimeoutError()

    def _scheduled(coro, _loop):
        coro.close()
        return slow_future

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_scheduled):
        result = api.create_identity()

    assert result["success"] is True
    assert result["data"]["name"] == "slow-companion"
    assert result["data"]["registered"] is False
    assert result["data"]["activation_pending"] is True
    assert "still finishing" in result["message"]
    slow_future.cancel.assert_not_called()
    slow_future.add_done_callback.assert_called_once()
    add_companion.assert_called_once()
    activation_call = add_companion.call_args
    assert activation_call.args[0] == api.config["identities"]["companions"][0]
    assert activation_call.kwargs == {
        "require_current_config": True,
    }


def test_acl_endpoints_paths(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.acl_info() == ""
    assert api.acl_clients() == ""
    assert api.acl_remove_client() == ""
    assert api.acl_stats() == ""

    request.method = "GET"
    assert api.acl_info()["success"] is False

    clients = [_FakeClient("aa" * 32, True), _FakeClient("bb" * 32, False)]
    acl = _FakeACL(clients)
    login_helper = SimpleNamespace(get_acl_dict=lambda: {0x42: acl, 0x51: _FakeACL([])})
    id_mgr = SimpleNamespace(
        get_identities_by_type=lambda t: (
            [("room1", _FakeIdentityObj(0x42), {})]
            if t == "room_server"
            else [("comp1", _FakeIdentityObj(0x51), {})]
        )
    )
    local = _FakeIdentityObj(0x42)
    frame_server = SimpleNamespace(
        companion_hash="0x51",
        _client_writer=SimpleNamespace(get_extra_info=lambda k: ("10.0.0.2", 1234)),
    )
    api.daemon_instance = SimpleNamespace(
        login_helper=login_helper,
        identity_manager=id_mgr,
        local_identity=local,
        companion_bridges={0x51: object()},
        companion_frame_servers=[frame_server],
    )

    info = api.acl_info()
    assert info["success"] is True
    assert info["data"]["total_identities"] >= 2

    all_clients = api.acl_clients()
    assert all_clients["success"] is True
    assert all_clients["data"]["count"] >= 1
    assert api.acl_clients(identity_hash="bad")["success"] is False
    assert api.acl_clients(identity_name="missing")["success"] is False

    request.method = "POST"
    request.json = {}
    assert api.acl_remove_client()["success"] is False
    request.json = {"public_key": "zz"}
    assert api.acl_remove_client()["success"] is False
    request.json = {"public_key": "aa" * 32, "identity_hash": "0x42"}
    removed = api.acl_remove_client()
    assert removed["success"] is True

    request.method = "GET"
    st = api.acl_stats()
    assert st["success"] is True
    assert st["data"]["total_identities"] >= 1


def test_room_endpoint_slice(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.room_messages(room_name="x") == ""
    assert api.room_stats(room_name="x") == ""
    assert api.room_clients(room_name="x") == ""
    assert api.room_message(room_name="x") == ""
    assert api.room_messages_clear(room_name="x") == ""

    # Basic room messages success path via helper patch
    request.method = "GET"
    db = SimpleNamespace(
        get_room_message_count=MagicMock(return_value=1),
        get_room_messages=MagicMock(
            return_value=[
                {
                    "id": 1,
                    "author_pubkey": "aa" * 32,
                    "post_timestamp": 1.0,
                    "sender_timestamp": 1,
                    "message_text": "m",
                    "txt_type": 0,
                }
            ]
        ),
        get_messages_since=MagicMock(return_value=[]),
        delete_room_message=MagicMock(return_value=True),
        clear_room_messages=MagicMock(return_value=1),
        get_all_room_clients=MagicMock(return_value=[]),
    )
    room = SimpleNamespace(
        db=db, max_posts=10, _running=True, next_push_time=0, last_cleanup_time=0
    )
    with patch.object(
        api,
        "_get_room_server_by_name_or_hash",
        return_value={
            "room_server": room,
            "name": "room",
            "hash": 0x42,
            "identity": None,
            "config": {},
        },
    ):
        _attach_storage(api, SimpleNamespace(get_node_name_by_pubkey=lambda _pk: "Node"))
        msgs = api.room_messages(room_name="room")
        assert msgs["success"] is True
        assert msgs["data"]["count"] == 1

        request.method = "DELETE"
        one = api.room_message(room_name="room", message_id="1")
        assert one["success"] is True
        cleared = api.room_messages_clear(room_name="room")
        assert cleared["success"] is True


def test_update_mqtt_config_validation_and_success(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.update_mqtt_config() == ""

    request.method = "POST"
    request.json = {}
    assert api.update_mqtt_config()["success"] is False

    request.json = {"brokers": "not-list"}
    assert api.update_mqtt_config()["success"] is False

    request.json = {"brokers": ["bad"]}
    assert "must be an object" in api.update_mqtt_config()["error"]

    request.json = {"brokers": [{"name": "n", "host": "h", "port": "x", "format": "json"}]}
    assert "invalid port" in api.update_mqtt_config()["error"]

    request.json = {
        "iata_code": "SFO",
        "status_interval": 20,
        "brokers": [
            {"preset": "waev"},
            {"name": "a", "host": "h", "port": 443, "format": "json", "enabled": True},
        ],
    }
    api.config_manager.update_and_save.return_value = {"success": True, "saved": True}
    out = api.update_mqtt_config()
    assert out["success"] is True
    assert out["data"]["restart_required"] is True

    api.config_manager.update_and_save.return_value = {"success": False, "error": "save failed"}
    out2 = api.update_mqtt_config()
    assert out2["success"] is False
    assert "save failed" in out2["error"]


def test_restart_service_options_method_and_result_paths(cherrypy_ctx):
    request, _ = cherrypy_ctx
    api = _make_api()

    request.method = "OPTIONS"
    assert api.restart_service() == ""

    request.method = "GET"
    with pytest.raises(cherrypy.HTTPError):
        api.restart_service()

    request.method = "POST"
    with patch("repeater.service_utils.restart_service", return_value=(True, "ok")):
        ok = api.restart_service()
    assert ok == {"success": True, "message": "ok"}

    with patch("repeater.service_utils.restart_service", return_value=(False, "nope")):
        err = api.restart_service()
    assert err["success"] is False
    assert "nope" in err["error"]
