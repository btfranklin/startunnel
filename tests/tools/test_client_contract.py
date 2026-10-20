"""Tests for the generated CLI response contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import generate_client_contract


def test_build_contract_keeps_public_responses_and_transitive_component_closure() -> None:
    responses = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}},
        },
        "400": {"description": "bad request", "content": {"x": None}},
    }
    document: dict[str, Any] = {
        "paths": {
            "/api/v1/items": {
                "get": {"responses": responses},
                "post": {"responses": {"204": {"description": "empty"}}},
                "put": {"responses": {"200": {}}},
            },
            "/health": {"get": {"responses": {"200": {}}}},
        },
        "components": {
            "schemas": {
                "Envelope": {
                    "type": "object",
                    "required": ["item"],
                    "properties": {
                        "item": {
                            "allOf": [{"$ref": "#/components/schemas/Item"}],
                            "description": "May be null",
                            "nullable": True,
                        },
                        "also_item": {"$ref": "#/components/schemas/Item"},
                    },
                },
                "Item": {
                    "type": "object",
                    "properties": {"envelope": {"$ref": "#/components/schemas/Envelope"}},
                },
                "Unused": {"type": "string"},
            }
        },
    }

    contract = generate_client_contract.build_contract(document)

    assert contract["operations"] == {
        "GET /api/v1/items": responses,
        "POST /api/v1/items": {"204": {"description": "empty"}},
    }
    assert contract["components"] == {
        "schemas": {
            "Envelope": document["components"]["schemas"]["Envelope"],
            "Item": document["components"]["schemas"]["Item"],
        },
        "headers": {},
    }
    assert contract["operations"]["GET /api/v1/items"]["200"] == responses["200"]
    assert contract["components"]["schemas"]["Envelope"]["properties"]["item"]["nullable"] is True


def test_build_contract_follows_header_schema_and_escaped_component_references() -> None:
    header = {
        "description": "Result metadata",
        "schema": {"$ref": "#/components/schemas/Result~1Metadata"},
    }
    schema = {
        "type": "object",
        "properties": {"related": {"$ref": "#/components/headers/Related~0Header"}},
    }
    document: dict[str, Any] = {
        "paths": {
            "/api/v1/items": {
                "get": {
                    "responses": {
                        "200": {"headers": {"X-Result": {"$ref": "#/components/headers/Result"}}}
                    }
                }
            }
        },
        "components": {
            "schemas": {"Result/Metadata": schema, "Unused": {"type": "string"}},
            "headers": {
                "Result": header,
                "Related~Header": {"description": "Related"},
                "Unused": {"description": "Unused"},
            },
        },
    }

    contract = generate_client_contract.build_contract(document)

    assert contract["components"] == {
        "schemas": {"Result/Metadata": schema},
        "headers": {
            "Result": header,
            "Related~Header": document["components"]["headers"]["Related~Header"],
        },
    }


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("#/components/schemas/Missing", "does not exist"),
        ("#/components/responses/Bad", "outside supported component groups"),
        ("#/components/headers/Missing", "does not exist"),
    ],
)
def test_build_contract_rejects_invalid_response_references(reference: str, message: str) -> None:
    document: dict[str, Any] = {
        "paths": {"/api/v1/items": {"get": {"responses": {"200": {"$ref": reference}}}}},
        "components": {"schemas": {}},
    }

    with pytest.raises(ValueError, match=message):
        generate_client_contract.build_contract(document)


def test_render_contract_uses_readable_sorted_json() -> None:
    document: dict[str, Any] = {
        "paths": {"/api/v1/z": {"get": {"responses": {"200": {"description": "é"}}}}},
        "components": {"schemas": {}},
    }

    rendered = generate_client_contract.render_contract(document)

    assert rendered.startswith('CLIENT_RESPONSE_CONTRACT = json.loads(\n    r"""\n{\n')
    assert '"description": "é"' in rendered
    assert rendered.endswith('"""  # noqa: E501\n)\n')
    assert json.loads(rendered.split('r"""', 1)[1].split('"""', 1)[0]) == {
        "operations": {"GET /api/v1/z": {"200": {"description": "é"}}},
        "components": {"schemas": {}, "headers": {}},
    }


def test_resource_parameter_names_match_cli_templates() -> None:
    response = {"200": {"description": "Resource"}}
    document = {
        "paths": {
            "/api/v1/admin/tunnels/{tunnel_id}/cycles": {"get": {"responses": response}},
        }
    }
    assert generate_client_contract.build_contract(document)["operations"] == {
        "GET /api/v1/admin/tunnels/{id}/cycles": response,
    }
    document["paths"]["/api/v1/admin/tunnels/{other_id}/cycles"] = {"get": {"responses": response}}
    with pytest.raises(ValueError, match="paths overlap"):
        generate_client_contract.build_contract(document)


def test_generation_replaces_only_marked_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openapi = tmp_path / "openapi.json"
    client = tmp_path / "client.py"
    document: dict[str, Any] = {
        "paths": {"/api/v1/items": {"get": {"responses": {"200": {}}}}},
        "components": {"schemas": {}},
    }
    openapi.write_text(json.dumps(document), encoding="utf-8")
    original_prefix = "# retained before\nvalue = 1\n"
    original_suffix = "# retained after\nvalue = 2\n"
    client.write_text(
        original_prefix
        + generate_client_contract.BEGIN_MARKER
        + "\nold = True\n"
        + generate_client_contract.END_MARKER
        + "\n"
        + original_suffix,
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_client_contract, "OPENAPI", openapi)
    monkeypatch.setattr(generate_client_contract, "CLIENT", client)
    monkeypatch.setattr(sys, "argv", ["generate_client_contract"])

    assert generate_client_contract.main() == 0
    source = client.read_text(encoding="utf-8")
    assert source.startswith(original_prefix + generate_client_contract.BEGIN_MARKER + "\n")
    assert source.endswith(generate_client_contract.END_MARKER + "\n" + original_suffix)
    assert generate_client_contract.render_contract(document) in source


def test_check_reports_drift_without_modifying_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    openapi = tmp_path / "openapi.json"
    client = tmp_path / "client.py"
    document: dict[str, Any] = {
        "paths": {},
        "components": {"schemas": {}},
    }
    openapi.write_text(json.dumps(document), encoding="utf-8")
    original = (
        generate_client_contract.BEGIN_MARKER
        + "\nold = True\n"
        + generate_client_contract.END_MARKER
        + "\n"
    )
    client.write_text(original, encoding="utf-8")
    monkeypatch.setattr(generate_client_contract, "OPENAPI", openapi)
    monkeypatch.setattr(generate_client_contract, "CLIENT", client)
    monkeypatch.setattr(sys, "argv", ["generate_client_contract", "--check"])

    assert generate_client_contract.main() == 1
    assert client.read_text(encoding="utf-8") == original
    assert "pdm run openapi" in capsys.readouterr().err
