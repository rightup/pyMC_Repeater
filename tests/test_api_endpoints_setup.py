from repeater.web.api_endpoints import APIEndpoints


def _make_api(config):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    return api


def test_needs_setup_stays_closed_when_radio_type_missing():
    api = _make_api(
        {
            "repeater": {
                "node_name": "mesh-node-01",
                "security": {"admin_password": "strong-password"},
            }
        }
    )

    result = api.needs_setup()

    assert result["needs_setup"] is False
    assert result["reasons"]["radio_not_configured"] is True
    assert result["reasons"]["default_name"] is False
    assert result["reasons"]["default_password"] is False


def test_needs_setup_does_not_trigger_for_configured_radio():
    api = _make_api(
        {
            "radio_type": "sx1262",
            "repeater": {
                "node_name": "mesh-node-01",
                "security": {"admin_password": "strong-password"},
            },
        }
    )

    result = api.needs_setup()

    assert result["needs_setup"] is False
    assert result["reasons"]["radio_not_configured"] is False
