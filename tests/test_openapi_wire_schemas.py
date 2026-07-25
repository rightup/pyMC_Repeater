"""Wire-shape regressions that route-only OpenAPI checks cannot detect."""

from pathlib import Path

import yaml

SPEC_PATH = Path(__file__).parents[1] / "repeater" / "web" / "openapi.yaml"


def _spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _success_data_schema(operation: dict) -> dict:
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    return schema["allOf"][1]["properties"]["data"]


def test_identities_response_documents_all_runtime_collections_and_counts():
    spec = _spec()
    data = _success_data_schema(spec["paths"]["/identities"]["get"])

    expected = {
        "registered",
        "configured",
        "configured_companions",
        "total_registered",
        "total_configured",
        "total_configured_companions",
    }
    assert set(data["required"]) == expected
    assert set(data["properties"]) == expected
    assert data["properties"]["registered"]["items"]["$ref"].endswith("/RegisteredIdentity")
    assert data["properties"]["configured"]["items"]["$ref"].endswith("/Identity")
    assert data["properties"]["configured_companions"]["items"]["$ref"].endswith("/Identity")


def test_configured_identity_schema_is_public_and_malformed_key_safe():
    spec = _spec()
    identity = spec["components"]["schemas"]["Identity"]

    assert set(identity["required"]) == {
        "name",
        "type",
        "settings",
        "hash",
        "public_key",
        "address",
        "registered",
    }
    for field in ("hash", "public_key", "address"):
        assert identity["properties"][field]["nullable"] is True

    runtime = identity["properties"]["runtime"]
    assert runtime["required"] == ["registered"]
    assert {
        "hash",
        "public_key",
        "address",
        "type",
        "registered",
        "matches_configuration",
    } == set(runtime["properties"])

    update_lookup = spec["paths"]["/update_identity"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["lookup_identity_key"]
    assert update_lookup["deprecated"] is True
    delete_params = spec["paths"]["/delete_identity"]["delete"]["parameters"]
    assert "lookup_identity_key" not in {parameter["name"] for parameter in delete_params}
    assert "public_key_prefix" in {parameter["name"] for parameter in delete_params}


def test_room_password_update_contract_is_write_only_and_explicit():
    spec = _spec()
    update = spec["paths"]["/update_identity"]["put"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["properties"]["settings"]
    fields = update["properties"]

    assert fields["admin_password"]["writeOnly"] is True
    assert fields["guest_password"]["writeOnly"] is True
    assert fields["clear_admin_password"]["writeOnly"] is True
    assert fields["clear_guest_password"]["writeOnly"] is True
    assert fields["clear_admin_password"]["type"] == "boolean"
    assert fields["clear_guest_password"]["type"] == "boolean"


def test_legacy_sse_documents_exact_operator_query_jwt_escape_hatch():
    spec = _spec()
    operation = spec["paths"]["/companion/events"]["get"]
    token = next(
        parameter for parameter in operation["parameters"] if parameter.get("name") == "token"
    )

    assert token["in"] == "query"
    assert token["required"] is False
    assert "Operator JWT only" in token["description"]
    assert "text/event-stream" in operation["responses"]["200"]["content"]


def test_common_and_mobile_wire_objects_require_every_always_present_key():
    schemas = _spec()["components"]["schemas"]

    assert set(schemas["SuccessResponse"]["required"]) == {"success", "data"}
    assert set(schemas["ErrorResponse"]["required"]) == {"success", "error"}
    assert set(schemas["MobileCompanionSummary"]["required"]) == {
        "name",
        "companion_hash",
        "node_name",
        "public_key",
    }
    assert set(schemas["MobileJournalEvent"]["required"]) == {
        "seq",
        "type",
        "ts",
        "packet_hash",
        "data",
    }
    assert set(schemas["MobileMessage"]["required"]) == {
        "id",
        "companion_hash",
        "sender_key",
        "recipient_key",
        "sender_prefix",
        "txt_type",
        "timestamp",
        "text",
        "is_channel",
        "channel_idx",
        "path_len",
        "snr",
        "rssi",
        "channel_data_type",
        "channel_data_payload",
        "packet_hash",
        "created_at",
        "observation_count",
        "unique_path_count",
        "direction",
        "state",
        "expected_ack",
        "source",
    }


def test_mobile_expected_ack_is_documented_as_an_unsigned_32_bit_value():
    spec = _spec()
    message_ack = spec["components"]["schemas"]["MobileMessage"]["properties"][
        "expected_ack"
    ]
    send_ack = _success_data_schema(
        spec["paths"]["/v1/companions/{name}/messages"]["post"]
    )["properties"]["expected_ack"]

    for schema in (message_ack, send_ack):
        assert schema["type"] == "integer"
        assert schema["nullable"] is True
        assert schema["minimum"] == 0
        assert schema["maximum"] == (1 << 32) - 1


def test_mobile_cursor_bounds_and_empty_action_bodies_match_runtime():
    spec = _spec()
    schemas = spec["components"]["schemas"]

    for schema_name, cursor_field in (
        ("MobileSnapshot", "cursor"),
        ("MobileSyncDelta", "next_cursor"),
        ("MobileSyncReset", "next_cursor"),
    ):
        assert schemas[schema_name]["properties"][cursor_field]["maxLength"] == 128
        epoch = schemas[schema_name]["properties"]["journal_epoch"]
        assert epoch["maxLength"] == 128
        assert epoch["pattern"] == "^[0-9a-f]+$"

    event_seq = schemas["MobileJournalEvent"]["properties"]["seq"]
    assert event_seq["minimum"] == 1
    assert event_seq["maximum"] == (1 << 63) - 1

    for operation_name in ("status_request", "telemetry_request", "reset_path"):
        operation = spec["paths"][
            f"/v1/companions/{{name}}/contacts/{{pubkey}}/{operation_name}"
        ]["post"]
        body = operation["requestBody"]
        assert body["required"] is False
        assert body["content"]["application/json"]["schema"] == {
            "type": "object",
            "additionalProperties": False,
        }

    sync_cursor = next(
        parameter
        for parameter in spec["paths"]["/v1/companions/{name}/sync"]["get"][
            "parameters"
        ]
        if parameter["name"] == "cursor"
    )
    assert all(
        choice["maxLength"] == 128
        for choice in sync_cursor["schema"]["oneOf"]
    )

    event_parameters = {
        parameter["name"]: parameter
        for parameter in spec["paths"]["/v1/companions/{name}/events"]["get"][
            "parameters"
        ]
    }
    for parameter_name in ("cursor", "Last-Event-ID"):
        assert all(
            choice["maxLength"] == 128
            for choice in event_parameters[parameter_name]["schema"]["oneOf"]
        )


def test_snapshot_and_sync_schemas_distinguish_complete_and_reset_documents():
    spec = _spec()
    schemas = spec["components"]["schemas"]
    snapshot = schemas["MobileSnapshot"]

    assert set(snapshot["required"]) == {
        "journal_epoch",
        "cursor",
        "self",
        "contacts",
        "channels",
        "messages",
        "server",
    }
    assert set(snapshot["properties"]["self"]["required"]) == {
        "public_key",
        "node_name",
        "adv_type",
        "latitude",
        "longitude",
        "autoadd_config",
        "autoadd_max_hops",
        "path_hash_mode",
        "rx_delay_base",
        "airtime_factor",
        "client_repeat",
        "manual_add_contacts",
        "telemetry_mode_base",
        "telemetry_mode_location",
        "telemetry_mode_environment",
        "advert_loc_policy",
        "multi_acks",
        "default_scope_name",
    }
    assert snapshot["properties"]["channels"]["items"]["required"] == ["index", "name"]
    assert snapshot["properties"]["server"]["required"] == ["version"]
    assert snapshot["properties"]["server"]["properties"]["version"]["nullable"] is True

    sync_data = _success_data_schema(spec["paths"]["/v1/companions/{name}/sync"]["get"])
    assert [choice["$ref"] for choice in sync_data["oneOf"]] == [
        "#/components/schemas/MobileSyncDelta",
        "#/components/schemas/MobileSyncReset",
    ]
    assert set(schemas["MobileSyncDelta"]["required"]) == {
        "journal_epoch",
        "events",
        "next_cursor",
        "has_more",
        "snapshot_required",
    }
    assert schemas["MobileSyncDelta"]["properties"]["snapshot_required"]["enum"] == [False]
    assert set(schemas["MobileSyncReset"]["required"]) == {
        "journal_epoch",
        "events",
        "next_cursor",
        "has_more",
        "snapshot_required",
        "reset_reason",
    }
    assert schemas["MobileSyncReset"]["properties"]["snapshot_required"]["enum"] == [True]
    assert schemas["MobileSyncReset"]["properties"]["events"]["maxItems"] == 0


def test_legacy_contact_import_documents_each_result_counter():
    spec = _spec()
    data = _success_data_schema(spec["paths"]["/companion/import_repeater_contacts"]["post"])

    counters = {"imported", "added", "updated", "retained", "removed"}
    assert set(data["required"]) == counters
    assert set(data["properties"]) == counters
    assert all(data["properties"][name]["minimum"] == 0 for name in counters)


def test_remote_login_session_endpoints_document_distinct_state_and_send_results():
    spec = _spec()

    connection = _success_data_schema(
        spec["paths"]["/v1/companions/{name}/contacts/{pubkey}/connection"]["get"]
    )
    assert connection["required"] == ["connected"]
    assert connection["properties"]["connected"]["type"] == "boolean"

    logout = _success_data_schema(
        spec["paths"]["/v1/companions/{name}/contacts/{pubkey}/logout"]["post"]
    )
    assert set(logout["required"]) == {"logged_out", "sent"}
    assert logout["properties"]["logged_out"]["enum"] == [True]
    assert logout["properties"]["sent"]["type"] == "boolean"


def test_contact_upsert_excludes_core_non_contact_advert_type():
    spec = _spec()
    operation = spec["paths"]["/v1/companions/{name}/contacts/{pubkey}"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    adv_type = body["properties"]["adv_type"]

    assert adv_type["minimum"] == 1
    assert adv_type["maximum"] == 255
    assert adv_type["default"] == 1


def test_send_failure_documents_human_readable_reason():
    spec = _spec()
    operation = spec["paths"]["/v1/companions/{name}/messages"]["post"]
    data = _success_data_schema(operation)

    assert data["properties"]["reason"]["type"] == "string"
    assert "failed" in data["properties"]["reason"]["description"]


def test_first_run_contract_exposes_permission_boundary_and_body_limits():
    spec = _spec()

    setup_status = spec["paths"]["/needs_setup"]["get"]
    status_schema = setup_status["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(status_schema["required"]) == {
        "needs_setup",
        "public_bootstrap_allowed",
    }

    setup = spec["paths"]["/setup_wizard"]["post"]
    assert "65,536 bytes" in setup["description"]
    assert {"400", "403", "405", "413", "415"} <= set(setup["responses"])
    setup_body = setup["requestBody"]["content"]["application/json"]["schema"]
    assert set(setup_body["required"]) == {
        "node_name",
        "hardware_key",
        "radio_preset",
        "admin_password",
    }
    assert setup_body["properties"]["admin_password"]["writeOnly"] is True

    config_import = spec["paths"]["/config_import"]["post"]
    assert "1,048,576 bytes" in config_import["description"]
    assert {"400", "401", "403", "405", "413", "415"} <= set(
        config_import["responses"]
    )
    import_body = config_import["requestBody"]["content"]["application/json"]["schema"]
    assert import_body["required"] == ["config"]


def test_json_only_public_and_credential_mutations_document_415():
    spec = _spec()
    operations = (
        ("/auth/login", "post"),
        ("/auth/refresh", "post"),
        ("/auth/change_password", "post"),
        ("/auth/tokens", "post"),
        ("/setup_wizard", "post"),
        ("/config_import", "post"),
        ("/v1/pair/start", "post"),
        ("/v1/pair", "post"),
    )

    for path, method in operations:
        assert "415" in spec["paths"][path][method]["responses"]
