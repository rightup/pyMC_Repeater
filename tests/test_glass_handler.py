import asyncio
import importlib.util
import json
import time
from pathlib import Path
import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "repeater" / "data_acquisition" / "glass_handler.py"
_SPEC = importlib.util.spec_from_file_location("repeater_glass_handler", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
GlassHandler = _MODULE.GlassHandler


class _DummyIdentity:
    @staticmethod
    def get_public_key():
        return bytes.fromhex("ab" * 32)


class _DummyConfigManager:
    def __init__(self):
        self.calls = []

    def update_and_save(self, updates, live_update=True, live_update_sections=None):
        self.calls.append(
            {
                "updates": updates,
                "live_update": live_update,
                "live_update_sections": live_update_sections,
            }
        )
        return {"success": True, "saved": True, "live_updated": True}

    @staticmethod
    def save_to_file():
        return True

    @staticmethod
    def live_update_daemon(_sections):
        return True


class _DummyDaemon:
    def __init__(self):
        self.local_identity = _DummyIdentity()
        self.repeater_handler = type("RH", (), {"start_time": time.time() - 60})()

    @staticmethod
    def get_stats():
        return {
            "rx_count": 11,
            "forwarded_count": 7,
            "dropped_count": 2,
            "flood_dup_count": 3,
            "direct_dup_count": 1,
            "sent_flood_count": 5,
            "sent_direct_count": 2,
            "utilization_percent": 4.2,
            "noise_floor_dbm": -111.5,
            "uptime_seconds": 60,
        }

    @staticmethod
    async def send_advert():
        return True


class _DummyMqttClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, message, qos=0, retain=False):
        self.published.append(
            {
                "topic": topic,
                "message": message,
                "qos": qos,
                "retain": retain,
            }
        )


class _DummyPahoClient:
    def __init__(self):
        self.username = None
        self.password = None
        self.tls_set_kwargs = None
        self.tls_insecure = None
        self.connected = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.on_connect = None
        self.on_disconnect = None

    def username_pw_set(self, username, password):
        self.username = username
        self.password = password

    def tls_set(self, **kwargs):
        self.tls_set_kwargs = kwargs

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def connect_async(self, host, port, keepalive):
        self.connected = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True


class _DummyPahoModule:
    def __init__(self, client):
        self._client = client

    def Client(self):
        return self._client


class _DummyHttpResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


class _DummySslContext:
    def __init__(self):
        self.cert_chain = None

    def load_cert_chain(self, certfile, keyfile):
        self.cert_chain = (certfile, keyfile)


def _make_config():
    return {
        "repeater": {
            "node_name": "mesh-repeater-01",
            "mode": "forward",
            "location": "51.5074,-0.1278",
            "identity_key": "PRIVATE-KEY",
        },
        "radio": {
            "frequency": 869618000,
            "spreading_factor": 8,
            "bandwidth": 62500,
            "tx_power": 14,
        },
        "glass": {
            "enabled": False,
            "base_url": "http://localhost:8080",
            "inform_interval_seconds": 30,
            "request_timeout_seconds": 10,
            "verify_tls": True,
            "api_token": "",
            "cert_store_dir": "/tmp/pymc-glass-test",
            "mqtt_password": "super-secret",
        },
    }


def test_compute_config_hash_has_expected_format():
    config = _make_config()
    config["repeater"]["identity_key"] = b"\x01" * 32
    digest = GlassHandler._compute_config_hash(config)
    assert digest.startswith("sha256:")
    assert len(digest) == 71


def test_build_inform_payload_contains_expected_fields():
    config = _make_config()
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)

    asyncio.run(handler._queue_command_result("cmd-1", "success", "ok"))
    payload = asyncio.run(handler._build_inform_payload())

    assert payload["type"] == "inform"
    assert payload["version"] == 1
    assert payload["node_name"] == "mesh-repeater-01"
    assert payload["pubkey"].startswith("0x")
    assert payload["config_hash"].startswith("sha256:")
    assert payload["location"] == "51.907400,-0.157800"
    assert payload["radio"]["frequency"] == 869618000
    assert payload["counters"]["duplicates"] == 4
    assert payload["settings"]["repeater"]["location"] == "51.9074,-0.1570"
    assert payload["settings"]["repeater"]["identity_key"] == "<redacted>"
    assert payload["settings"]["glass"]["mqtt_password"] == "<redacted>"
    assert payload["command_results"][0]["command_id"] == "cmd-1"


def test_execute_set_mode_command_updates_config():
    config = _make_config()
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)

    ok, message = asyncio.run(handler._execute_command_action("set_mode", {"mode": "monitor"}))
    assert ok is True
    assert "Config patched" in message
    assert manager.calls
    assert manager.calls[-1]["updates"]["repeater"]["mode"] == "monitor"


def test_handle_command_response_queues_result():
    config = _make_config()
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)

    asyncio.run(
        handler._handle_command_response(
            {
                "type": "command",
                "command_id": "cmd-42",
                "action": "run_diagnostic",
                "params": {},
            }
        )
    )

    assert handler._pending_command_results
    queued = handler._pending_command_results[-1]
    assert queued["command_id"] == "cmd-42"
    assert queued["status"] == "success"


def test_publish_telemetry_packet_envelope_to_expected_topic():
    config = _make_config()
    config["glass"]["enabled"] = True
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)
    handler.mqtt_enabled = True
    handler._mqtt_ready = True
    handler._mqtt_client = _DummyMqttClient()

    handler.publish_telemetry(
        "packet",
        {
            "timestamp": 1760000000,
            "packet_hash": "ABCDEF123456",
            "rssi": -80.5,
        },
    )

    assert len(handler._mqtt_client.published) == 1
    published = handler._mqtt_client.published[0]
    assert published["topic"] == "glass/mesh-repeater-01/packet"
    payload = json.loads(published["message"])
    assert payload["version"] == 1
    assert payload["type"] == "packet"
    assert payload["node_name"] == "mesh-repeater-01"
    assert payload["topic"] == "glass/mesh-repeater-01/packet"
    assert payload["payload"]["packet_hash"] == "ABCDEF123456"
    assert published["qos"] == 0
    assert published["retain"] is False


def test_publish_telemetry_event_uses_event_topic_suffix():
    config = _make_config()
    config["glass"]["enabled"] = True
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)
    handler.mqtt_enabled = True
    handler._mqtt_ready = True
    handler._mqtt_client = _DummyMqttClient()

    handler.publish_telemetry(
        "noise_floor",
        {
            "timestamp": "2026-04-15T12:30:45Z",
            "noise_floor_dbm": -112.3,
        },
    )

    assert len(handler._mqtt_client.published) == 1
    published = handler._mqtt_client.published[0]
    assert published["topic"] == "glass/mesh-repeater-01/event/noise_floor"
    payload = json.loads(published["message"])
    assert payload["type"] == "event"
    assert payload["event_name"] == "noise_floor"
    assert payload["topic"] == "glass/mesh-repeater-01/event/noise_floor"


def test_apply_config_update_glass_managed_updates_runtime_and_file(tmp_path):
    config = _make_config()
    config["glass"]["enabled"] = True
    config["glass"]["cert_store_dir"] = str(tmp_path)
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)
    handler._sync_mqtt_publisher = lambda: None

    ok, message = handler._apply_config_update(
        {
            "glass_managed": {
                "mqtt_enabled": True,
                "mqtt_broker_host": "emqx",
                "mqtt_broker_port": 1883,
                "mqtt_base_topic": "glass/fleet",
                "mqtt_tls_enabled": True,
            }
        },
        merge_mode="patch",
    )

    assert ok is True
    assert "Managed settings updated" in message
    managed_path = tmp_path / "managed.json"
    assert managed_path.exists()
    managed = json.loads(managed_path.read_text(encoding="utf-8"))
    assert managed["mqtt_enabled"] is True
    assert managed["mqtt_broker_host"] == "emqx"
    assert managed["mqtt_broker_port"] == 1883
    assert managed["mqtt_base_topic"] == "glass/fleet"
    assert managed["mqtt_tls_enabled"] is True
    assert handler.mqtt_enabled is True
    assert handler.mqtt_broker_host == "emqx"
    assert handler.mqtt_broker_port == 1883
    assert handler.mqtt_base_topic == "glass/fleet"
    assert handler.mqtt_tls_enabled is True


def test_sync_mqtt_publisher_restarts_when_signature_changes(monkeypatch):
    config = _make_config()
    config["glass"]["enabled"] = True
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)
    handler.enabled = True
    handler.mqtt_enabled = True
    handler._mqtt_client = object()
    handler._mqtt_runtime_signature = ("old-host", 1883, "glass", None, None)

    calls = []

    def _fake_close():
        calls.append("close")
        handler._mqtt_client = None
        handler._mqtt_runtime_signature = None

    def _fake_init():
        calls.append("init")

    monkeypatch.setattr(_MODULE, "mqtt", object())
    monkeypatch.setattr(handler, "_close_mqtt_publisher", _fake_close)
    monkeypatch.setattr(handler, "_init_mqtt_publisher", _fake_init)

    handler.mqtt_broker_host = "new-host"
    handler._sync_mqtt_publisher()

    assert calls == ["close", "init"]


def test_init_mqtt_publisher_uses_mtls_cert_material(tmp_path, monkeypatch):
    cert_path = tmp_path / "glass-client.crt"
    key_path = tmp_path / "glass-client.key"
    ca_path = tmp_path / "glass-ca.crt"
    cert_path.write_text("CERT", encoding="utf-8")
    key_path.write_text("KEY", encoding="utf-8")
    ca_path.write_text("CA", encoding="utf-8")

    config = _make_config()
    config["glass"]["enabled"] = True
    config["glass"]["verify_tls"] = True
    config["glass"]["client_cert_path"] = str(cert_path)
    config["glass"]["client_key_path"] = str(key_path)
    config["glass"]["ca_cert_path"] = str(ca_path)
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)
    handler.enabled = True
    handler.mqtt_enabled = True
    handler.mqtt_tls_enabled = True
    handler.mqtt_broker_host = "emqx"
    handler.mqtt_broker_port = 8883
    handler.mqtt_base_topic = "glass"

    fake_client = _DummyPahoClient()
    monkeypatch.setattr(_MODULE, "mqtt", _DummyPahoModule(fake_client))

    handler._init_mqtt_publisher()

    assert fake_client.tls_set_kwargs is not None
    assert fake_client.tls_set_kwargs["ca_certs"] == str(ca_path)
    assert fake_client.tls_set_kwargs["certfile"] == str(cert_path)
    assert fake_client.tls_set_kwargs["keyfile"] == str(key_path)
    assert fake_client.tls_set_kwargs["cert_reqs"] == _MODULE.ssl.CERT_REQUIRED
    assert fake_client.connected == ("emqx", 8883, 60)
    assert fake_client.loop_started is True
    assert handler._mqtt_client is fake_client
    assert handler._mqtt_runtime_signature == handler._current_mqtt_signature()


def test_post_inform_sync_uses_configured_ca_and_client_cert_chain(tmp_path, monkeypatch):
    cert_path = tmp_path / "glass-client.crt"
    key_path = tmp_path / "glass-client.key"
    ca_path = tmp_path / "glass-ca.crt"
    cert_path.write_text("CERT", encoding="utf-8")
    key_path.write_text("KEY", encoding="utf-8")
    ca_path.write_text("CA", encoding="utf-8")

    config = _make_config()
    config["glass"]["enabled"] = True
    config["glass"]["base_url"] = "https://glass.example"
    config["glass"]["verify_tls"] = True
    config["glass"]["client_cert_path"] = str(cert_path)
    config["glass"]["client_key_path"] = str(key_path)
    config["glass"]["ca_cert_path"] = str(ca_path)
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)

    calls = {}
    fake_context = _DummySslContext()

    def _fake_create_default_context(cafile=None):
        calls["cafile"] = cafile
        return fake_context

    def _fake_urlopen(req, timeout=None, context=None):
        calls["timeout"] = timeout
        calls["context"] = context
        assert req.full_url == "https://glass.example/inform"
        return _DummyHttpResponse({"type": "noop", "interval": 30})

    monkeypatch.setattr(_MODULE.ssl, "create_default_context", _fake_create_default_context)
    monkeypatch.setattr(_MODULE.request, "urlopen", _fake_urlopen)

    response = handler._post_inform_sync({"type": "inform"})

    assert response["type"] == "noop"
    assert calls["cafile"] == str(ca_path)
    assert calls["context"] is fake_context
    assert fake_context.cert_chain == (str(cert_path), str(key_path))


def test_build_ssl_context_raises_when_client_key_missing(tmp_path):
    cert_path = tmp_path / "glass-client.crt"
    cert_path.write_text("CERT", encoding="utf-8")

    config = _make_config()
    config["glass"]["enabled"] = True
    config["glass"]["base_url"] = "https://glass.example"
    config["glass"]["verify_tls"] = False
    config["glass"]["client_cert_path"] = str(cert_path)
    daemon = _DummyDaemon()
    manager = _DummyConfigManager()
    handler = GlassHandler(config=config, daemon_instance=daemon, config_manager=manager)

    with pytest.raises(RuntimeError, match="client_key_path"):
        handler._build_ssl_context("https://glass.example/inform")
