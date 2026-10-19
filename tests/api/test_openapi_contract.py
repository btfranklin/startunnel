"""The generated schema owns the complete public wire surface."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openapi_spec_validator import validate

from api.router import api


def _schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(api.get_openapi_schema())))


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
        assert path in schema["paths"], f"OpenAPI is missing {path}."
        assert methods <= set(schema["paths"][path]), f"OpenAPI is missing a method for {path}."


def _assert_addresses_stay_out_of_urls(schema: dict[str, Any]) -> None:
    for path in schema["paths"].values():
        operations = [path, *(value for value in path.values() if isinstance(value, dict))]
        for operation in operations:
            for parameter in operation.get("parameters", []):
                if "$ref" in parameter:
                    parameter = schema["components"]["parameters"][
                        parameter["$ref"].rsplit("/", 1)[-1]
                    ]
                if parameter["in"] in {"path", "query"}:
                    assert parameter["name"] != "address"
                    assert not parameter["name"].endswith("_address")


def test_addresses_stay_out_of_url_paths_and_query_parameters() -> None:
    _assert_addresses_stay_out_of_urls(_schema())


@pytest.mark.parametrize("location", ["path", "query"])
@pytest.mark.parametrize("owner", ["path", "operation", "reference"])
def test_url_contract_rejects_an_address_parameter(location: str, owner: str) -> None:
    schema = _schema()
    route: dict[str, Any] = {"get": {}}
    schema["paths"]["/future/{address}" if location == "path" else "/future"] = route
    parameter = {"name": "address", "in": location, "schema": {"type": "string"}}
    if owner == "reference":
        schema["components"]["parameters"] = {"Address": parameter}
        route["get"]["parameters"] = [{"$ref": "#/components/parameters/Address"}]
    else:
        (route if owner == "path" else route["get"])["parameters"] = [parameter]
    with pytest.raises(AssertionError):
        _assert_addresses_stay_out_of_urls(schema)


def test_tree_write_operations_require_idempotency_header() -> None:
    schema = _schema()
    operations = [
        schema["paths"]["/api/v1/tunnels"]["post"],
        schema["paths"]["/api/v1/messages"]["post"],
        schema["paths"]["/api/v1/cycles/close"]["post"],
        schema["paths"]["/api/v1/beacons"]["post"],
        schema["paths"]["/api/v1/beacons/withdraw"]["post"],
    ]
    for operation in operations:
        parameter = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        assert parameter["required"] is True
        assert parameter["schema"]["minLength"] == 8
        assert parameter["schema"]["maxLength"] == 128
        assert parameter["schema"]["pattern"] == r"^[\x20-\x7e]+$"


def test_cycle_lifetimes_labels_correlation_and_tree_pages_are_bounded() -> None:
    components = _schema()["components"]["schemas"]
    cycle = components["CycleCreateRequest"]["properties"]
    expiry = cycle["expires_in_seconds"]["anyOf"][0]
    assert cycle["label"]["maxLength"] == 160
    assert expiry["minimum"] == 300
    assert expiry["maximum"] == 604_800
    root_correlation = components["RootMessageRequest"]["properties"]["correlation_id"]["anyOf"][0]
    reply_correlation = components["PostReplyRequest"]["properties"]["correlation_id"]["anyOf"][0]
    assert root_correlation["maxLength"] == 128
    assert reply_correlation["maxLength"] == 128
    tree_limit = components["TreeRequest"]["properties"]["limit"]
    assert tree_limit["minimum"] == 1
    assert tree_limit["maximum"] == 1_000


def test_authenticated_routes_document_internal_error_envelopes() -> None:
    schema = _schema()
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for operation in methods.values():
            assert "500" in operation["responses"]


def test_tree_content_is_a_discriminated_text_or_json_union() -> None:
    components = _schema()["components"]["schemas"]
    for schema_name in ("RootMessageRequest", "PostReplyRequest", "MessageResponse"):
        content = components[schema_name]["properties"]["content"]
        assert content["discriminator"] == {
            "mapping": {
                "json": "#/components/schemas/JsonContent",
                "text": "#/components/schemas/TextContent",
            },
            "propertyName": "type",
        }
        assert content["oneOf"] == [
            {"$ref": "#/components/schemas/TextContent"},
            {"$ref": "#/components/schemas/JsonContent"},
        ]


def test_participant_acknowledgement_fields_are_nullable() -> None:
    participant = _schema()["components"]["schemas"]["ParticipantResponse"]["properties"]
    assert participant["acknowledged_event_position"]["anyOf"] == [
        {"type": "integer"},
        {"type": "null"},
    ]
    assert participant["last_advanced_at"]["anyOf"] == [
        {"format": "date-time", "type": "string"},
        {"type": "null"},
    ]


def test_create_requires_nested_cycle_and_root_and_reply_requires_parent() -> None:
    components = _schema()["components"]["schemas"]
    assert "cycle" in components["CreateTunnelRequest"]["required"]
    assert "root" in components["CycleCreateRequest"]["required"]
    assert "content" in components["RootMessageRequest"]["required"]
    reply_required = set(components["PostReplyRequest"]["required"])
    assert {"address", "parent_id", "content"} <= reply_required
