"""The generated schema owns the complete public wire surface."""

from __future__ import annotations

import json

import pytest
from openapi_spec_validator import validate
from tests.json_values import json_at, json_elements, json_members, json_text

from api.router import api
from core.json_types import JsonObject, json_object


def _schema() -> JsonObject:
    return json_object(json.loads(json.dumps(api.get_openapi_schema())))


def test_live_openapi_is_valid_and_has_tree_routes() -> None:
    schema = _schema()
    validate(schema)
    required = {
        "/api/v1/me": {"get"},
        "/api/v1/enter": {"post"},
        "/api/v1/participants": {"post"},
        "/api/v1/beacons": {"post"},
        "/api/v1/beacons/board": {"post"},
        "/api/v1/beacons/withdraw": {"post"},
        "/api/v1/beacons/wait": {"post"},
        "/api/v1/tunnels": {"post"},
        "/api/v1/messages": {"post"},
        "/api/v1/tree": {"post"},
        "/api/v1/tree/message": {"post"},
        "/api/v1/tree/branch": {"post"},
        "/api/v1/tree/replies": {"post"},
        "/api/v1/cycles/close": {"post"},
    }
    for path, methods in required.items():
        assert path in json_members(schema["paths"]), f"OpenAPI is missing {path}."
        assert methods <= set(json_members(json_members(schema["paths"])[path])), (
            f"OpenAPI is missing a method for {path}."
        )


def _assert_addresses_stay_out_of_urls(schema: JsonObject) -> None:
    for path_value in json_members(schema["paths"]).values():
        path = json_members(path_value)
        operations = [path, *(value for value in path.values() if isinstance(value, dict))]
        for operation in operations:
            for parameter_value in json_elements(operation.get("parameters", [])):
                parameter = json_members(parameter_value)
                if "$ref" in parameter:
                    name = json_text(parameter["$ref"]).rsplit("/", 1)[-1]
                    parameter = json_members(
                        json_members(json_members(schema["components"])["parameters"])[name]
                    )
                if parameter["in"] in ("path", "query"):
                    assert parameter["name"] != "address"
                    assert not json_text(parameter["name"]).endswith("_address")


def test_addresses_stay_out_of_url_paths_and_query_parameters() -> None:
    _assert_addresses_stay_out_of_urls(_schema())


@pytest.mark.parametrize("location", ["path", "query"])
@pytest.mark.parametrize("owner", ["path", "operation", "reference"])
def test_url_contract_rejects_an_address_parameter(location: str, owner: str) -> None:
    schema = _schema()
    route: JsonObject = {"get": {}}
    json_members(schema["paths"])["/future/{address}" if location == "path" else "/future"] = route
    parameter: JsonObject = {"name": "address", "in": location, "schema": {"type": "string"}}
    if owner == "reference":
        json_members(schema["components"])["parameters"] = {"Address": parameter}
        json_members(route["get"])["parameters"] = [{"$ref": "#/components/parameters/Address"}]
    else:
        (route if owner == "path" else json_members(route["get"]))["parameters"] = [parameter]
    with pytest.raises(AssertionError):
        _assert_addresses_stay_out_of_urls(schema)


def test_tree_write_operations_require_idempotency_header() -> None:
    schema = _schema()
    operations = [
        json_at(schema, "paths", "/api/v1/tunnels", "post"),
        json_at(schema, "paths", "/api/v1/messages", "post"),
        json_at(schema, "paths", "/api/v1/cycles/close", "post"),
        json_at(schema, "paths", "/api/v1/beacons", "post"),
        json_at(schema, "paths", "/api/v1/beacons/withdraw", "post"),
    ]
    for operation in operations:
        parameter = next(
            parameter
            for parameter_value in json_elements(json_members(operation)["parameters"])
            if (parameter := json_members(parameter_value))["in"] == "header"
            and parameter["name"] == "Idempotency-Key"
        )
        assert parameter["required"] is True
        assert json_at(parameter, "schema", "minLength") == 8
        assert json_at(parameter, "schema", "maxLength") == 128
        assert json_at(parameter, "schema", "pattern") == r"^[\x20-\x7e]+$"


def test_cycle_lifetimes_labels_correlation_and_tree_pages_are_bounded() -> None:
    components = json_at(_schema(), "components", "schemas")
    cycle = json_at(components, "CycleCreateRequest", "properties")
    expiry = json_at(cycle, "expires_in_seconds", "anyOf", 0)
    assert json_at(cycle, "label", "maxLength") == 160
    assert json_at(expiry, "minimum") == 300
    assert json_at(expiry, "maximum") == 604_800
    root_correlation = json_at(
        components, "RootMessageRequest", "properties", "correlation_id", "anyOf", 0
    )
    reply_correlation = json_at(
        components, "PostReplyRequest", "properties", "correlation_id", "anyOf", 0
    )
    assert json_at(root_correlation, "maxLength") == 128
    assert json_at(reply_correlation, "maxLength") == 128
    tree_limit = json_at(components, "TreeRequest", "properties", "limit")
    assert json_at(tree_limit, "minimum") == 1
    assert json_at(tree_limit, "maximum") == 1_000


def test_authenticated_routes_document_internal_error_envelopes() -> None:
    schema = _schema()
    for path, methods in json_members(schema["paths"]).items():
        if not path.startswith("/api/v1"):
            continue
        for operation in json_members(methods).values():
            assert "500" in json_members(json_members(operation)["responses"])


def test_tree_content_is_a_discriminated_text_or_json_union() -> None:
    components = json_at(_schema(), "components", "schemas")
    for schema_name in ("RootMessageRequest", "PostReplyRequest", "MessageResponse"):
        content = json_at(json_members(components)[schema_name], "properties", "content")
        assert json_at(content, "discriminator") == {
            "mapping": {
                "json": "#/components/schemas/JsonContent",
                "text": "#/components/schemas/TextContent",
            },
            "propertyName": "type",
        }
        assert json_at(content, "oneOf") == [
            {"$ref": "#/components/schemas/TextContent"},
            {"$ref": "#/components/schemas/JsonContent"},
        ]


def test_participant_acknowledgement_fields_are_nullable() -> None:
    participant = json_at(_schema(), "components", "schemas", "ParticipantResponse", "properties")
    assert json_at(participant, "acknowledged_event_position", "anyOf") == [
        {"type": "integer"},
        {"type": "null"},
    ]
    assert json_at(participant, "last_advanced_at", "anyOf") == [
        {"format": "date-time", "type": "string"},
        {"type": "null"},
    ]


def test_create_requires_nested_cycle_and_root_and_reply_requires_parent() -> None:
    components = json_at(_schema(), "components", "schemas")
    assert "cycle" in json_elements(json_at(components, "CreateTunnelRequest", "required"))
    assert "root" in json_elements(json_at(components, "CycleCreateRequest", "required"))
    assert "content" in json_elements(json_at(components, "RootMessageRequest", "required"))
    reply_required = {
        json_text(value)
        for value in json_elements(json_at(components, "PostReplyRequest", "required"))
    }
    assert {"address", "parent_id", "content"} <= reply_required
