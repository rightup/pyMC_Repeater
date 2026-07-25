"""Tests for setup_wizard pymc_usb / pymc_tcp branches.

These verify that when the first-run /setup wizard is finished with one of
the two pymc_* hardware tiles selected, api_endpoints.setup_wizard() writes
a config.yaml that matches what get_radio_for_board() expects (see
repeater/config.py and tests/test_radio_config.py).
"""

import copy
import io
import json
import sys
import threading
import types

import cherrypy
import pytest
import yaml

from repeater.config_manager import ConfigManager
from repeater.web.api_endpoints import APIEndpoints

# Minimal initial config.yaml the wizard writes into.
_BASE_CONFIG = {
    "repeater": {"node_name": "mesh-repeater-01", "security": {"admin_password": "admin123"}},
    "radio": {},
}

_BASE_REQUEST = {
    "node_name": "pymc-test",
    "admin_password": "supersecret",
    "radio_preset": {
        "frequency": 869.618,
        "spreading_factor": 8,
        "bandwidth": 62.5,
        "coding_rate": 8,
        "tx_power": 22,
    },
}


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    """Bootstrap a tempdir with config.yaml + radio-settings.json + mocked cherrypy."""
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(_BASE_CONFIG, f)

    radio_settings = {
        "hardware": {
            "pymc_usb": {
                "name": "pymc_usb modem (USB-CDC)",
                "radio_type": "pymc_usb",
                "tx_power": 22,
                "preamble_length": 16,
            },
            "pymc_tcp": {
                "name": "pymc_tcp modem (Wi-Fi / Ethernet)",
                "radio_type": "pymc_tcp",
                "tx_power": 22,
                "preamble_length": 16,
            },
        }
    }
    with open(tmp_path / "radio-settings.json", "w") as f:
        json.dump(radio_settings, f)

    # resolve_storage_dir() returns the directory of config_path when the
    # config has no explicit storage_dir set — that's exactly what we want
    # so the wizard finds our radio-settings.json next to config.yaml.
    config = {
        "storage_dir": str(tmp_path),
        "repeater": {
            "node_name": "mesh-repeater-01",
            "security": {"admin_password": "admin123"},
        },
    }
    endpoints = APIEndpoints(config=config, config_path=str(config_path))

    # Stub the post-wizard service restart — we don't want a real systemctl call.
    fake_service_utils = types.ModuleType("repeater.service_utils")
    fake_service_utils.restart_service = lambda: None
    monkeypatch.setitem(sys.modules, "repeater.service_utils", fake_service_utils)

    def _set_request(body):
        # cherrypy.request is a thread-local — populate the bits the handler reads.
        encoded = json.dumps(body).encode("utf-8")
        cherrypy.request.method = "POST"
        cherrypy.request.headers = {
            "Content-Length": str(len(encoded)),
            "Content-Type": "application/json",
        }
        cherrypy.request.body = io.BytesIO(encoded)

    return tmp_path, config_path, endpoints, _set_request


def _read_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def test_api_reuses_daemon_config_manager(tmp_path):
    config_path = tmp_path / "config.yaml"
    config = {
        "identities": {"companions": []},
        "mobile_api": {"sse_max_connections": 8},
    }
    manager = ConfigManager(str(config_path), config)
    daemon = types.SimpleNamespace(config_manager=manager)

    endpoints = APIEndpoints(
        config=config,
        config_path=str(config_path),
        daemon_instance=daemon,
    )

    assert endpoints.config_manager is manager
    assert endpoints.companion.config_manager is manager


# ─── pymc_usb ─────────────────────────────────────────────────────────


def test_wizard_pymc_usb_defaults(wizard_env):
    tmp_path, config_path, endpoints, set_request = wizard_env

    body = dict(_BASE_REQUEST, hardware_key="pymc_usb")
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is True
    assert result["config"]["radio_type"] == "pymc_usb"
    assert result["config"]["pymc_usb_port"] == "/dev/ttyACM0"
    assert result["config"]["pymc_usb_baudrate"] == 921600

    written = _read_yaml(config_path)
    assert written["repeater"]["setup_complete"] is True
    assert endpoints.config["repeater"]["setup_complete"] is True
    assert (
        endpoints.config["repeater"]["security"]["admin_password"]
        == "supersecret"
    )
    assert written["radio_type"] == "pymc_usb"
    assert written["pymc_usb"]["port"] == "/dev/ttyACM0"
    assert written["pymc_usb"]["baudrate"] == 921600
    assert written["pymc_usb"]["lbt_enabled"] is True
    assert written["pymc_usb"]["lbt_max_attempts"] == 5
    assert written["radio"]["tx_power"] == 22
    assert written["radio"]["preamble_length"] == 16
    # config.py rejects pymc_usb if 'sx1262' / 'ch341' keys leak in — none here.
    assert "sx1262" not in written


def test_wizard_pymc_usb_overrides_from_request(wizard_env):
    tmp_path, config_path, endpoints, set_request = wizard_env

    body = dict(
        _BASE_REQUEST,
        hardware_key="pymc_usb",
        pymc_usb_port="/dev/ttyUSB0",
        pymc_usb_baudrate=115200,
    )
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["pymc_usb"]["port"] == "/dev/ttyUSB0"
    assert written["pymc_usb"]["baudrate"] == 115200


# ─── pymc_tcp ─────────────────────────────────────────────────────────


def test_wizard_pymc_tcp_placeholder(wizard_env):
    """No host in request → wizard writes a sentinel placeholder. config.py
    will then refuse to start with a clear error pointing at pymc_tcp.host."""
    tmp_path, config_path, endpoints, set_request = wizard_env

    body = dict(_BASE_REQUEST, hardware_key="pymc_tcp")
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is True
    assert result["config"]["radio_type"] == "pymc_tcp"
    assert result["config"]["pymc_tcp_host"] == "REPLACE_WITH_MODEM_HOST"
    assert result["config"]["pymc_tcp_port"] == 5055

    written = _read_yaml(config_path)
    assert written["radio_type"] == "pymc_tcp"
    assert written["pymc_tcp"]["host"] == "REPLACE_WITH_MODEM_HOST"
    assert written["pymc_tcp"]["port"] == 5055
    assert written["pymc_tcp"]["token"] == ""
    assert written["pymc_tcp"]["connect_timeout"] == 5.0
    assert written["pymc_tcp"]["lbt_enabled"] is True
    # token deliberately stripped from response.
    assert "pymc_tcp_token" not in result["config"]


def test_wizard_pymc_tcp_full_fields(wizard_env):
    tmp_path, config_path, endpoints, set_request = wizard_env

    body = dict(
        _BASE_REQUEST,
        hardware_key="pymc_tcp",
        pymc_tcp_host="pymc-3e2834.local",
        pymc_tcp_port=6000,
        pymc_tcp_token="hunter2",
    )
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["pymc_tcp"]["host"] == "pymc-3e2834.local"
    assert written["pymc_tcp"]["port"] == 6000
    assert written["pymc_tcp"]["token"] == "hunter2"


# ─── KISS regression guard ────────────────────────────────────────────


def test_wizard_kiss_branch_unchanged(wizard_env, tmp_path):
    """Make sure adding the pymc_* branches didn't break the existing KISS path."""
    tmp_path, config_path, endpoints, set_request = wizard_env

    body = dict(_BASE_REQUEST, hardware_key="kiss")
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["radio_type"] == "kiss"
    assert written["kiss"]["port"] == "/dev/ttyUSB0"
    assert written["kiss"]["baud_rate"] == 115200


def test_wizard_rejected_after_setup_complete(wizard_env):
    """setup_wizard should be first-run only once config is already initialized."""
    tmp_path, config_path, endpoints, set_request = wizard_env

    configured = {
        "repeater": {"node_name": "already-set", "security": {"admin_password": "verysecret"}},
        "radio_type": "pymc_tcp",
        "radio": {
            "frequency": 869618000,
            "spreading_factor": 8,
            "bandwidth": 62500,
            "coding_rate": 8,
        },
    }
    with open(config_path, "w") as f:
        yaml.safe_dump(configured, f)

    body = dict(_BASE_REQUEST, hardware_key="pymc_tcp", pymc_tcp_host="modem.local")
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert "public setup is closed" in result["error"].lower()


def test_wizard_and_config_import_share_one_bootstrap_gate(
    wizard_env,
    monkeypatch,
):
    _tmp_path, _config_path, endpoints, _set_request = wizard_env
    endpoints._require_post = lambda: None
    monkeypatch.setattr(
        cherrypy,
        "response",
        types.SimpleNamespace(status=200, headers={}),
        raising=False,
    )

    wizard_body_started = threading.Event()
    release_wizard_body = threading.Event()

    def read_body(**_kwargs):
        if threading.current_thread().name == "setup-wizard":
            wizard_body_started.set()
            assert release_wizard_body.wait(timeout=2)
            return dict(_BASE_REQUEST, hardware_key="kiss")
        return {
            "config": {
                "repeater": {
                    "security": {"admin_password": "restored-password"}
                },
                "web": {"site_name": "restored"},
            }
        }

    monkeypatch.setattr(
        "repeater.web.api_endpoints.read_json_object",
        read_body,
    )

    results = {}

    wizard = threading.Thread(
        target=lambda: results.setdefault("wizard", endpoints.setup_wizard()),
        name="setup-wizard",
    )
    config_import = threading.Thread(
        target=lambda: results.setdefault("import", endpoints.config_import()),
        name="config-import",
    )

    wizard.start()
    assert wizard_body_started.wait(timeout=2)
    config_import.start()
    config_import.join(timeout=2)
    assert not config_import.is_alive()
    release_wizard_body.set()
    wizard.join(timeout=2)
    assert not wizard.is_alive()

    assert results["import"]["success"] is True
    assert results["wizard"]["success"] is False
    assert results["wizard"]["error"].startswith("Public setup is closed")
    assert _read_yaml(_config_path)["repeater"]["setup_complete"] is True


@pytest.mark.parametrize(
    "configured",
    [
        {
            "repeater": {
                "node_name": "mesh-repeater-01",
                "security": {"admin_password": "verysecret"},
            },
            "radio_type": "none",
        },
        {
            "repeater": {
                "node_name": "configured-name",
                "security": {"admin_password": "verysecret"},
            },
        },
    ],
)
def test_wizard_strong_password_closes_public_bootstrap(wizard_env, configured):
    _tmp_path, config_path, endpoints, set_request = wizard_env
    with open(config_path, "w") as file:
        yaml.safe_dump(configured, file)
    set_request(dict(_BASE_REQUEST, hardware_key="kiss"))

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert result["error"].startswith("Public setup is closed")


def test_wizard_rejects_short_admin_password(wizard_env):
    _tmp_path, _config_path, endpoints, set_request = wizard_env

    body = dict(_BASE_REQUEST, hardware_key="pymc_tcp", admin_password="short7")
    set_request(body)

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert "at least 8 characters" in result["error"]


def test_wizard_rejects_public_default_admin_password(wizard_env):
    _tmp_path, _config_path, endpoints, set_request = wizard_env
    set_request(dict(_BASE_REQUEST, hardware_key="kiss", admin_password="admin123"))

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert "default admin123" in result["error"]


@pytest.mark.parametrize(
    "admin_password",
    [
        123,
        "safe-pass\ud800",
        "a" * 1025,
        "🙂" * 257,
    ],
)
def test_wizard_rejects_unusable_admin_password(wizard_env, admin_password):
    _tmp_path, config_path, endpoints, set_request = wizard_env
    before_file = config_path.read_bytes()
    before_live = copy.deepcopy(endpoints.config)
    set_request(
        dict(
            _BASE_REQUEST,
            hardware_key="kiss",
            admin_password=admin_password,
        )
    )

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert "password" in result["error"].lower()
    assert config_path.read_bytes() == before_file
    assert endpoints.config == before_live


def test_wizard_accepts_admin_password_at_utf8_wire_limit(wizard_env):
    _tmp_path, config_path, _endpoints, set_request = wizard_env
    password = "🙂" * 256
    set_request(
        dict(
            _BASE_REQUEST,
            hardware_key="kiss",
            admin_password=password,
        )
    )

    result = _endpoints.setup_wizard()

    assert result["success"] is True
    assert (
        _read_yaml(config_path)["repeater"]["security"]["admin_password"]
        == password
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("jwt_secret", "too-short"),
        ("jwt_secret", 123),
        ("jwt_expiry_minutes", 0),
        ("jwt_expiry_minutes", True),
        ("jwt_expiry_minutes", 10_081),
    ],
)
def test_wizard_rejects_invalid_auth_startup_config_without_mutation(
    wizard_env,
    field,
    value,
):
    _tmp_path, config_path, endpoints, set_request = wizard_env
    configured = copy.deepcopy(_BASE_CONFIG)
    configured["repeater"]["security"][field] = value
    with open(config_path, "w") as file:
        yaml.safe_dump(configured, file)
    before_file = config_path.read_bytes()
    before_live = copy.deepcopy(endpoints.config)
    set_request(dict(_BASE_REQUEST, hardware_key="kiss"))

    result = endpoints.setup_wizard()

    assert result["success"] is False
    assert field in result["error"]
    assert config_path.read_bytes() == before_file
    assert endpoints.config == before_live


@pytest.mark.parametrize("jwt_secret", [None, ""])
def test_wizard_accepts_jwt_secret_generation_sentinels(
    wizard_env,
    jwt_secret,
):
    _tmp_path, config_path, endpoints, set_request = wizard_env
    configured = copy.deepcopy(_BASE_CONFIG)
    configured["repeater"]["security"].update(
        {
            "jwt_secret": jwt_secret,
            "jwt_expiry_minutes": 10_080,
        }
    )
    with open(config_path, "w") as file:
        yaml.safe_dump(configured, file)
    set_request(dict(_BASE_REQUEST, hardware_key="kiss"))

    result = endpoints.setup_wizard()

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["repeater"]["security"]["jwt_secret"] == jwt_secret
    assert written["repeater"]["security"]["jwt_expiry_minutes"] == 10_080


def test_wizard_save_failure_restores_live_and_persisted_config(
    wizard_env,
    monkeypatch,
):
    _tmp_path, config_path, endpoints, set_request = wizard_env
    before_file = config_path.read_bytes()
    before_live = json.loads(json.dumps(endpoints.config))
    monkeypatch.setattr(endpoints.config_manager, "save_to_file", lambda: False)
    set_request(dict(_BASE_REQUEST, hardware_key="kiss"))

    result = endpoints.setup_wizard()

    assert result == {
        "success": False,
        "error": "Setup could not be persisted; no changes were applied",
    }
    assert cherrypy.response.status == 503
    assert endpoints.config == before_live
    assert config_path.read_bytes() == before_file


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (
            b'{"node_name":"first","node_name":"second","hardware_key":"kiss"}',
            400,
        ),
        (
            json.dumps(
                {
                    "node_name": "safe",
                    "hardware_key": "kiss",
                    "padding": "x" * (64 * 1024),
                }
            ).encode("utf-8"),
            413,
        ),
    ],
)
def test_wizard_rejects_unsafe_json_before_mutation(wizard_env, raw, status):
    _tmp_path, config_path, endpoints, _set_request = wizard_env
    before = config_path.read_bytes()
    cherrypy.request.method = "POST"
    cherrypy.request.headers = {
        "Content-Length": str(len(raw)),
        "Content-Type": "application/json",
    }
    cherrypy.request.body = io.BytesIO(raw)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        endpoints.setup_wizard()

    assert int(str(exc_info.value.status).split()[0]) == status
    assert config_path.read_bytes() == before


def test_wizard_rejects_cross_origin_simple_content_type_before_mutation(
    wizard_env,
):
    _tmp_path, config_path, endpoints, _set_request = wizard_env
    before = config_path.read_bytes()
    raw = json.dumps(dict(_BASE_REQUEST, hardware_key="kiss")).encode("utf-8")
    cherrypy.request.method = "POST"
    cherrypy.request.headers = {
        "Content-Length": str(len(raw)),
        "Content-Type": "text/plain",
    }
    cherrypy.request.body = io.BytesIO(raw)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        endpoints.setup_wizard()

    assert int(str(exc_info.value.status).split()[0]) == 415
    assert config_path.read_bytes() == before
