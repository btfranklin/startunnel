"""The generated schema owns the complete public wire surface."""

from __future__ import annotations

import json
from typing import Any, cast

from openapi_spec_validator import validate

from api.router import api


def _schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(api.get_openapi_schema())))


def test_live_openapi_is_valid_and_has_tree_routes() -> None:
    schema = _schema()
    validate(schema)
    required = {
        "/api/v1/me": {"get"},
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


def test_phase_one_relay_and_id_metadata_routes_are_removed() -> None:
    paths = _schema()["paths"]
    removed = {
        "/api/v1/claims",
        "/api/v1/claims/acknowledge",
        "/api/v1/claims/release",
        "/api/v1/claims/extend",
        "/api/v1/tunnels/{tunnel_id}",
        "/api/v1/teams/{team_id}/tunnels/{tunnel_id}",
    }
    assert removed.isdisjoint(paths)


def test_no_address_message_receipt_or_cycle_is_a_tunnel_path_parameter() -> None:
    paths = _schema()["paths"]
    forbidden = ("{address", "{message", "{receipt", "{cycle")
    assert all(not any(value in path for value in forbidden) for path in paths)


def test_tree_write_operations_require_idempotency_header() -> None:
    schema = _schema()
    operations = [
        schema["paths"]["/api/v1/tunnels"]["post"],
        schema["paths"]["/api/v1/messages"]["post"],
        schema["paths"]["/api/v1/cycles/close"]["post"],
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


def test_create_requires_nested_cycle_and_root_and_reply_requires_parent() -> None:
    components = _schema()["components"]["schemas"]
    assert "cycle" in components["CreateTunnelRequest"]["required"]
    assert "root" in components["CycleCreateRequest"]["required"]
    assert "content" in components["RootMessageRequest"]["required"]
    reply_required = set(components["PostReplyRequest"]["required"])
    assert {"address", "parent_id", "content"} <= reply_required
