from pathlib import Path

import yaml


def test_create_identity_schema_matches_supported_identity_types():
    spec_path = Path(__file__).parents[1] / "repeater" / "web" / "openapi.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    schema = spec["paths"]["/create_identity"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    properties = schema["properties"]

    assert properties["type"]["enum"] == ["companion", "room_server"]
    assert {"node_name", "frame_enabled", "tcp_port", "bind_address"} <= set(
        properties["settings"]["properties"]
    )
    assert set(
        spec["paths"]["/create_identity"]["post"]["requestBody"]["content"]["application/json"][
            "examples"
        ]
    ) == {"companion", "room_server"}
    response_data = spec["paths"]["/create_identity"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["properties"]["data"]["properties"]
    assert response_data["activation_pending"]["type"] == "boolean"
