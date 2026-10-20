"""Tests for the generated CLI response contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts import generate_client_contract


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location(
        "contract_test_cli", generate_client_contract.CLIENT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _response_help(cli: Any, path: str) -> dict[str, Any]:
    commands = [{"command": "example", "method": "GET", "path": path, "stdin_json": False}]
    metadata = cli.response_help(commands)
    return {**metadata, "commands": commands}


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
        "GET /api/v1/items": {"200": responses["200"], "400": "400"},
        "POST /api/v1/items": {"204": {"description": "empty"}},
    }
    assert contract["common_headers"] == {}
    assert contract["error_responses"] == {"400": responses["400"]}
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

    assert contract["common_headers"] == {"X-Result": {"$ref": "#/components/headers/Result"}}
    assert contract["operations"]["GET /api/v1/items"]["200"] == {}
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


def test_render_contract_uses_compact_sorted_json() -> None:
    document: dict[str, Any] = {
        "paths": {"/api/v1/z": {"get": {"responses": {"200": {"description": "é"}}}}},
        "components": {"schemas": {}},
    }

    rendered = generate_client_contract.render_contract(document)

    assert rendered.startswith('CLIENT_RESPONSE_CONTRACT = json.loads(\n    r"""\n{')
    assert '"description":"é"' in rendered
    assert rendered.endswith('"""  # noqa: E501\n)\n')
    encoded = rendered.split('r"""\n', 1)[1].split('\n"""', 1)[0]
    assert encoded == json.dumps(
        json.loads(encoded), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert json.loads(encoded) == {
        "common_headers": {},
        "error_responses": {},
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


def test_factored_responses_restore_headers_errors_statuses_and_refs(
    cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = {"X-Shared": {"$ref": "#/components/headers/Shared"}}
    content = {"application/json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
    shared_error = {"description": "Unavailable", "headers": common, "content": content}
    first_error = {
        "description": "Invalid input",
        "headers": {**common, "X-Detail": {"description": "First detail"}},
        "content": content,
    }
    definitions = {
        "/api/v1/a": {
            "200": {"description": "Result", "headers": common, "content": content},
            "400": first_error,
            "401": {"description": "No access", "headers": common},
            "503": shared_error,
        },
        "/api/v1/b": {
            "204": {"description": "Empty", "headers": common},
            "302": {
                "description": "Redirect",
                "headers": {**common, "Location": {"schema": {"type": "string"}}},
            },
            "400": {**first_error, "description": "Different error"},
            "503": shared_error,
            "default": {"description": "Other status", "headers": common},
        },
        "/api/v1/c": {
            "200": {"description": "Result", "headers": common, "content": content},
            "400": {
                **first_error,
                "headers": {**common, "X-Detail": {"description": "Different detail"}},
            },
            "503": shared_error,
        },
    }
    document = {
        "paths": {
            path: {"get": {"responses": responses}} for path, responses in definitions.items()
        },
        "components": {
            "schemas": {
                "Problem": {"type": "object", "properties": {"message": {"type": "string"}}},
                "Header/Metadata": {"type": "string"},
            },
            "headers": {"Shared": {"schema": {"$ref": "#/components/schemas/Header~1Metadata"}}},
        },
    }
    original = deepcopy(document)
    contract = generate_client_contract.build_contract(document)
    assert document == original
    assert contract["common_headers"] == common
    assert set(contract["error_responses"]) == {"400", "401", "503"}
    assert contract["operations"]["GET /api/v1/a"]["400"] == "400"
    assert isinstance(contract["operations"]["GET /api/v1/b"]["400"], dict)
    assert isinstance(contract["operations"]["GET /api/v1/c"]["400"], dict)
    assert contract["operations"]["GET /api/v1/b"]["503"] == "503"
    assert "401" not in contract["operations"]["GET /api/v1/b"]
    assert contract["components"] == document["components"]

    monkeypatch.setattr(cli, "CLIENT_RESPONSE_CONTRACT", contract)
    before = deepcopy(contract)
    for path, responses in definitions.items():
        output = _response_help(cli, path)
        assert output["commands"][0]["responses"] == responses
        fresh = deepcopy(output)
        output["commands"][0]["responses"]["503"]["description"] = "Changed"
        output["commands"][0]["responses"]["503"]["headers"]["X-Shared"]["$ref"] = "Changed"
        output["components"]["schemas"]["Problem"]["properties"]["message"]["type"] = "Changed"
        assert contract == before
        assert _response_help(cli, path) == fresh


def test_missing_empty_and_different_headers_remain_distinct(
    cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = {
        "/api/v1/absent": {"200": {"description": "No headers"}},
        "/api/v1/empty": {"200": {"description": "Empty headers", "headers": {}}},
        "/api/v1/first": {
            "200": {"description": "First", "headers": {"X-Value": {"description": "First"}}}
        },
        "/api/v1/second": {
            "200": {"description": "Second", "headers": {"X-Value": {"description": "Second"}}}
        },
    }
    document = {
        "paths": {
            path: {"get": {"responses": responses}} for path, responses in definitions.items()
        }
    }
    contract = generate_client_contract.build_contract(document)
    assert contract["common_headers"] == {}
    monkeypatch.setattr(cli, "CLIENT_RESPONSE_CONTRACT", contract)
    for path, responses in definitions.items():
        assert _response_help(cli, path)["commands"][0]["responses"] == responses


def test_factoring_does_not_depend_on_path_or_status_order() -> None:
    responses = {
        "503": {"description": "Unavailable", "headers": {"X-Value": {"description": "Shared"}}},
        "400": {"description": "Invalid", "headers": {"X-Value": {"description": "Shared"}}},
    }
    paths: dict[str, Any] = {
        "/api/v1/b": {"get": {"responses": responses}},
        "/api/v1/a": {"get": {"responses": {**responses, "400": {"description": "Other"}}}},
    }
    reordered = {
        path: {"get": {"responses": dict(reversed(operation["get"]["responses"].items()))}}
        for path, operation in reversed(paths.items())
    }
    assert generate_client_contract.render_contract({"paths": paths}) == (
        generate_client_contract.render_contract({"paths": reordered})
    )


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
