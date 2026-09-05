"""Provisioning must not reopen when optional node/radio settings change."""

import copy
import json
import threading
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import Mock
from wsgiref.simple_server import WSGIRequestHandler, make_server

import cherrypy
import pytest
import yaml

from repeater.config_manager import ConfigManager
from repeater.web.api_endpoints import APIEndpoints


def make_config(password="custom-test-password", node_name="mesh-repeater-01", radio=None):
    return {
        "repeater": {"node_name": node_name, "security": {"admin_password": password}},
        "radio_type": radio,
    }


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    config = make_config()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    api = APIEndpoints(config=config, config_path=str(path))
    api.config_manager = ConfigManager(str(path), config)
    monkeypatch.setattr(api.config_manager, "live_update_daemon", Mock(return_value=True))
    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(method="POST", json={}, user=None, headers={}, params={}),
    )
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(status=200, headers={}))
    monkeypatch.setattr(threading.Thread, "start", Mock())
    return api, path


@pytest.mark.parametrize(
    "radio", [None, "", "none", "null", "disabled", "off", "no_radio", "modem_usb"]
)
@pytest.mark.parametrize("node_name", ["", "mesh-repeater-01", "configured-node"])
def test_custom_password_closes_setup_independent_of_optional_settings(radio, node_name):
    api = APIEndpoints.__new__(APIEndpoints)
    needs_setup, reasons = api._setup_status_from_config(
        make_config(node_name=node_name, radio=radio)
    )
    assert needs_setup is False
    assert reasons["default_password"] is False


@pytest.mark.parametrize("password", [None, "", "admin123", "   ", "\t\n"])
def test_legacy_fresh_config_remains_usable(password):
    api = APIEndpoints.__new__(APIEndpoints)
    assert api._setup_status_from_config(make_config(password=password))[0] is True


@pytest.mark.parametrize("endpoint", ["setup_wizard", "config_import"])
def test_anonymous_mutation_denied_from_persisted_custom_password(api_env, endpoint):
    api, path = api_env
    before = path.read_bytes()
    # Persisted credentials take precedence over stale startup defaults.
    api.config["repeater"]["security"]["admin_password"] = "admin123"
    cherrypy.request.json = {"config": make_config(password="replacement-password")}
    result = getattr(api, endpoint)()
    assert cherrypy.response.status == 403
    assert result["success"] is False
    assert path.read_bytes() == before
    api.config_manager.live_update_daemon.assert_not_called()


def test_completed_wizard_persists_marker_even_with_legacy_default_password(api_env):
    api, path = api_env
    api.config.update(make_config(password="admin123"))
    path.write_text(yaml.safe_dump(api.config))
    cherrypy.request.json = {
        "node_name": "mesh-repeater-01",
        "hardware_key": "kiss",
        "admin_password": "admin123",
        "radio_preset": {"frequency": 869.618, "bandwidth": 62.5},
    }
    assert api.setup_wizard()["success"] is True
    persisted = yaml.safe_load(path.read_text())
    assert persisted["setup_completed"] is True
    assert api.config["setup_completed"] is True
    persisted["radio_type"] = None
    assert api._setup_status_from_config(persisted)[0] is False
    assert api.needs_setup()["needs_setup"] is False
    assert api.setup_wizard()["success"] is False
    assert cherrypy.response.status == 403


def test_explicit_false_marker_cannot_override_custom_password():
    api = APIEndpoints.__new__(APIEndpoints)
    config = make_config()
    config["setup_completed"] = False
    assert api._setup_status_from_config(config)[0] is False


def test_authenticated_restore_of_defaults_does_not_reopen_provisioning(api_env):
    api, path = api_env
    cherrypy.request.user = {"username": "admin", "auth_type": "jwt"}
    cherrypy.request.json = {
        "config": dict(make_config(password="admin123"), setup_completed=False)
    }
    assert api.config_import()["success"] is True
    persisted = yaml.safe_load(path.read_text())
    assert persisted["setup_completed"] is True
    assert api._setup_status_from_config(persisted)[0] is False


@pytest.mark.parametrize("password", [False, 12345678, [], {}, " admin123 "])
def test_malformed_or_nonempty_custom_credentials_do_not_open_setup(password):
    api = APIEndpoints.__new__(APIEndpoints)
    assert api._setup_status_from_config(make_config(password=password))[0] is False


@pytest.mark.parametrize("password", [None, "admin123"])
def test_fresh_partial_restore_still_allows_wizard(api_env, password):
    api, path = api_env
    api.config.update(make_config(password=password))
    path.write_text(yaml.safe_dump(api.config))
    cherrypy.request.json = {"config": {"radio_type": None}}
    assert api.config_import()["success"] is True
    assert api.needs_setup()["needs_setup"] is True


def test_fresh_restore_custom_password_closes_followup_restore(api_env):
    api, path = api_env
    api.config.update(make_config(password="admin123"))
    path.write_text(yaml.safe_dump(api.config))
    cherrypy.request.json = {"config": make_config()}
    assert api.config_import()["success"] is True
    assert api.needs_setup()["needs_setup"] is False
    assert api.config_import()["success"] is False
    assert cherrypy.response.status == 403


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


@pytest.mark.parametrize("endpoint", ["setup_wizard", "config_import"])
def test_anonymous_loopback_dispatch_rejects_provisioned_null_radio(
    tmp_path, endpoint, monkeypatch
):
    # Older direct-handler tests leave synthetic request/response objects behind.
    # A real WSGI dispatch must use CherryPy's thread-local request context.
    monkeypatch.setattr(cherrypy, "request", cherrypy._ThreadLocalProxy("request"))
    monkeypatch.setattr(cherrypy, "response", cherrypy._ThreadLocalProxy("response"))
    config = make_config()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    api = APIEndpoints(config=copy.deepcopy(config), config_path=str(path))
    api.config_manager = Mock()
    api.config_manager.save_to_file.return_value = True
    # Same public provisioning routes: authorization must happen in the handlers.
    app = cherrypy.Application(
        SimpleNamespace(api=api),
        config={
            "/api/setup_wizard": {"tools.require_auth.on": False},
            "/api/config_import": {"tools.require_auth.on": False},
        },
    )
    with make_server("127.0.0.1", 0, app, handler_class=QuietHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(
                "POST",
                f"/api/{endpoint}",
                json.dumps({"config": make_config()}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = json.loads(response.read())
            assert response.status == 403
            assert body["success"] is False
            assert yaml.safe_load(path.read_text()) == config
            api.config_manager.update_and_save.assert_not_called()
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=5)
