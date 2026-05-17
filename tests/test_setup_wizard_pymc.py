"""Tests for setup_wizard pymc_usb / pymc_tcp branches.

These verify that when the first-run /setup wizard is finished with one of
the two pymc_* hardware tiles selected, api_endpoints.setup_wizard() writes
a config.yaml that matches what get_radio_for_board() expects (see
repeater/config.py and tests/test_radio_config.py).
"""

import json
import sys
import types

import cherrypy
import pytest
import yaml

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
        "repeater": {
            "storage_dir": str(tmp_path),
            "node_name": "mesh-repeater-01",
            "security": {"admin_password": "admin123"},
        }
    }
    endpoints = APIEndpoints(config=config, config_path=str(config_path))

    # Stub the post-wizard service restart — we don't want a real systemctl call.
    fake_service_utils = types.ModuleType("repeater.service_utils")
    fake_service_utils.restart_service = lambda: None
    monkeypatch.setitem(sys.modules, "repeater.service_utils", fake_service_utils)

    def _set_request(body):
        # cherrypy.request is a thread-local — populate the bits the handler reads.
        cherrypy.request.method = "POST"
        cherrypy.request.json = body

    return tmp_path, config_path, endpoints, _set_request


def _read_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


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
