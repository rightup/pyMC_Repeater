"""Document ambiguous plugin IPC completion rather than claiming cancellation."""

from pathlib import Path

import yaml


def test_plugin_operations_document_unknown_completion():
    schema = yaml.safe_load((Path(__file__).parents[1] / "repeater/web/openapi.yaml").read_text())
    response = schema["components"].get("responses", {}).get("PluginOutcomeUnknown")
    assert response is not None
    body = response["content"]["application/json"]["schema"]
    assert body["properties"]["outcome"]["enum"] == ["unknown"]
    for path, item in schema["paths"].items():
        if path != "/plugins" and not path.startswith("/plugins/"):
            continue
        for method, operation in item.items():
            if method not in {"get", "post", "delete", "put", "patch", "head"}:
                continue
            assert operation["responses"]["504"] == {
                "$ref": "#/components/responses/PluginOutcomeUnknown"
            }, (path, method)
