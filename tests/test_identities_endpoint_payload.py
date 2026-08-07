"""Regression: /api/identities payload shape with the registered repeater identity.

Since the daemon registers its own default identity in the IdentityManager
(so companion/room-server collisions against the repeater's hash byte are
caught), the endpoint's raw ``registered`` list carries a
``repeater:repeater`` entry and ``total_registered`` counts it. The web UI
is unaffected — it renders only the per-entry ``registered`` boolean on
configured room servers/companions, which match on ``room_server:``/
``companion:``-prefixed names — but external API consumers see the new
entry, so this test pins the intended payload.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from openhop_core.protocol import LocalIdentity

from repeater.identity_manager import IdentityManager
from repeater.web.api_endpoints import APIEndpoints


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    request = SimpleNamespace(method="GET", params={}, json={})
    response = SimpleNamespace(headers={}, status=200)
    monkeypatch.setattr(cherrypy, "request", request, raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    return request, response


def _distinct_identities(count):
    """LocalIdentities with pairwise-distinct first hash bytes."""
    picked = []
    seen = set()
    while len(picked) < count:
        identity = LocalIdentity()
        hash_byte = identity.get_public_key()[0]
        if hash_byte not in seen:
            seen.add(hash_byte)
            picked.append(identity)
    return picked


def _make_api(config, identity_manager):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api.daemon_instance = SimpleNamespace(identity_manager=identity_manager)
    api.send_advert_func = None
    api.event_loop = None
    api.stats_getter = None
    api._config_path = "/tmp/test-config.yaml"
    api.config_manager = MagicMock()
    return api


def test_registered_list_includes_repeater_identity(cherrypy_ctx):
    companion_key = "11" * 32
    companion_id = LocalIdentity(seed=bytes.fromhex(companion_key))
    repeater_id = LocalIdentity()
    while repeater_id.get_public_key()[0] == companion_id.get_public_key()[0]:
        repeater_id = LocalIdentity()

    config = {
        "identities": {
            "companions": [
                {
                    "name": "phone",
                    "identity_key": companion_key,
                    "settings": {},
                }
            ],
            "room_servers": [],
        }
    }
    manager = IdentityManager(config)
    assert manager.register_identity("repeater", repeater_id, config, "repeater")
    assert manager.register_identity("phone", companion_id, {}, "companion")

    api = _make_api(config, manager)
    payload = api.identities()

    assert payload["success"] is True
    data = payload["data"]
    registered = data["registered"]
    assert data["total_registered"] == len(registered) == 2

    by_name = {entry["name"]: entry for entry in registered}
    repeater_entry = by_name["repeater:repeater"]
    assert repeater_entry["type"] == "repeater"
    assert repeater_entry["hash"] == f"0x{repeater_id.get_public_key()[0]:02X}"
    assert repeater_entry["public_key"] == repeater_id.get_public_key().hex()

    companion_entry = by_name["companion:phone"]
    assert companion_entry["type"] == "companion"

    # The configured-companion view (what the web UI renders) matches the
    # companion by its prefixed name and is not disturbed by the repeater
    # entry: it still reports the companion as registered.
    assert data["total_configured_companions"] == 1
    ui_entry = data["configured_companions"][0]
    assert ui_entry["name"] == "phone"
    assert ui_entry["registered"] is True


def test_name_repeater_is_reserved_by_the_default_identity(cherrypy_ctx):
    """Registering the default repeater identity reserves the bare name
    "repeater": a room server or companion configured with that name is
    rejected by the collision rules, and the endpoint reports the configured
    public identity as unregistered rather than borrowing the repeater's
    runtime fields."""
    repeater_id, room_id = _distinct_identities(2)

    config = {
        "identities": {
            "room_servers": [{"name": "repeater", "identity_key": "aa" * 32, "settings": {}}],
            "companions": [],
        }
    }
    manager = IdentityManager(config)
    assert manager.register_identity("repeater", repeater_id, config, "repeater")
    assert not manager.register_identity("repeater", room_id, {}, "room_server")

    api = _make_api(config, manager)
    data = api.identities()["data"]

    assert data["total_registered"] == 1
    room_entry = data["configured"][0]
    assert room_entry["registered"] is False
    configured_identity = LocalIdentity(seed=bytes.fromhex("aa" * 32))
    assert room_entry["hash"] == (f"0x{configured_identity.get_public_key()[0]:02x}")
    assert room_entry["public_key"] == configured_identity.get_public_key().hex()
    assert room_entry["public_key"] != repeater_id.get_public_key().hex()


def test_same_type_runtime_mismatch_stays_separate_from_config(cherrypy_ctx):
    configured_key = "22" * 32
    configured_identity = LocalIdentity(seed=bytes.fromhex(configured_key))
    runtime_identity = LocalIdentity(seed=bytes.fromhex("33" * 32))
    config = {
        "identities": {
            "companions": [
                {
                    "name": "phone",
                    "identity_key": configured_key,
                    "settings": {},
                }
            ],
            "room_servers": [],
        }
    }
    manager = IdentityManager(config)
    assert manager.register_identity(
        "phone",
        runtime_identity,
        {},
        "companion",
    )
    api = _make_api(config, manager)

    listed = api.identities()["data"]["configured_companions"][0]
    fetched = api.identity(name="phone")["data"]

    for entry in (listed, fetched):
        assert entry["public_key"] == configured_identity.get_public_key().hex()
        assert entry["hash"] == (f"0x{configured_identity.get_public_key()[0]:02x}")
        assert entry["registered"] is False
        assert entry["runtime"]["public_key"] == runtime_identity.get_public_key().hex()
        assert entry["runtime"]["type"] == "companion"
        assert entry["runtime"]["registered"] is True
        assert entry["runtime"]["matches_configuration"] is False


def test_delete_companion_uses_public_key_prefix_not_private_key(cherrypy_ctx):
    request, _response = cherrypy_ctx
    request.method = "DELETE"
    private_key = "11" * 32
    identity = LocalIdentity(seed=bytes.fromhex(private_key))
    config = {
        "identities": {
            "companions": [
                {
                    "name": "phone",
                    "identity_key": private_key,
                    "settings": {},
                }
            ],
            "room_servers": [],
        }
    }
    api = _make_api(config, IdentityManager(config))
    api.config_manager.save_to_file.return_value = True

    result = api.delete_identity(
        type="companion",
        public_key_prefix=identity.get_public_key().hex()[:16],
    )

    assert result["success"] is True
    assert config["identities"]["companions"] == []
    with pytest.raises(TypeError):
        api.delete_identity(
            type="companion",
            lookup_identity_key=private_key,
        )


def _room_api_with_credentials():
    config = {
        "identities": {
            "room_servers": [
                {
                    "name": "main",
                    "identity_key": "44" * 32,
                    "settings": {
                        "node_name": "Main room",
                        "admin_password": "admin-secret",
                        "guest_password": "guest-secret",
                    },
                }
            ],
            "companions": [],
        }
    }
    api = _make_api(config, IdentityManager(config))
    api.config_manager.save_to_file.return_value = True
    return api, config


def test_public_settings_round_trip_keeps_room_credentials(cherrypy_ctx):
    request, _response = cherrypy_ctx
    api, config = _room_api_with_credentials()

    public_settings = api.identity(name="main")["data"]["settings"]
    assert public_settings == {
        "node_name": "Main room",
        "admin_password_configured": True,
        "guest_password_configured": True,
    }

    request.method = "PUT"
    request.json = {
        "name": "main",
        "settings": {
            **public_settings,
            "node_name": "Renamed room",
            # The bundled legacy UI supplied empty inputs on an unrelated edit.
            "admin_password": "",
            "guest_password": "",
        },
    }
    result = api.update_identity()

    assert result["success"] is True
    stored = config["identities"]["room_servers"][0]["settings"]
    assert stored == {
        "node_name": "Renamed room",
        "admin_password": "admin-secret",
        "guest_password": "guest-secret",
    }
    assert result["data"]["settings"] == {
        "node_name": "Renamed room",
        "admin_password_configured": True,
        "guest_password_configured": True,
    }
    assert "admin-secret" not in str(result)
    assert "guest-secret" not in str(result)


def test_room_password_update_and_explicit_clear_are_unambiguous(cherrypy_ctx):
    request, response = cherrypy_ctx
    api, config = _room_api_with_credentials()
    request.method = "PUT"

    request.json = {
        "name": "main",
        "settings": {"admin_password": "replacement"},
    }
    replaced = api.update_identity()
    assert replaced["success"] is True
    stored = config["identities"]["room_servers"][0]["settings"]
    assert stored["admin_password"] == "replacement"
    assert "replacement" not in str(replaced)

    request.json = {
        "name": "main",
        "settings": {
            "clear_guest_password": True,
            "guest_password": "",
        },
    }
    cleared = api.update_identity()
    assert cleared["success"] is True
    stored = config["identities"]["room_servers"][0]["settings"]
    assert stored["guest_password"] == ""
    assert "clear_guest_password" not in stored
    assert cleared["data"]["settings"]["guest_password_configured"] is False

    request.json = {
        "name": "main",
        "settings": {
            "clear_admin_password": True,
            "admin_password": "ambiguous",
        },
    }
    rejected = api.update_identity()
    assert rejected["success"] is False
    assert response.status == 400
    assert config["identities"]["room_servers"][0]["settings"]["admin_password"] == ("replacement")


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"clear_admin_password": 1}, "clear_admin_password must be a boolean"),
        ({"guest_password": None}, "guest_password must be a string"),
    ],
)
def test_room_password_update_rejects_ambiguous_types(
    cherrypy_ctx,
    settings,
    error,
):
    request, response = cherrypy_ctx
    api, config = _room_api_with_credentials()
    before = config["identities"]["room_servers"][0]["settings"].copy()
    request.method = "PUT"
    request.json = {"name": "main", "settings": settings}

    result = api.update_identity()

    assert result["success"] is False
    assert response.status == 400
    assert error in result["error"]
    assert config["identities"]["room_servers"][0]["settings"] == before
