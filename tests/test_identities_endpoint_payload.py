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
    repeater_id, companion_id = _distinct_identities(2)

    config = {
        "identities": {
            "companions": [
                {
                    "name": "phone",
                    "identity_key": companion_id.get_private_key().hex(),
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
    rejected by the collision rules, and the endpoint reports it
    unregistered rather than silently matching the repeater's entry."""
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
    assert room_entry["hash"] is None
