"""Acceptance checks for the standalone CLI's published response contract."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "cli" / "star_tunnel.py"
OPENAPI = PROJECT_ROOT / "generated" / "openapi.json"


def load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("startunnel_response_contract", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalized_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{id}", path)


def operation_for(document: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    matches = [
        operation
        for source_path, methods in document["paths"].items()
        if normalized_path(source_path) == path
        for source_method, operation in methods.items()
        if source_method.upper() == method
    ]
    assert len(matches) == 1, f"expected one OpenAPI operation for {method} {path}"
    return cast(dict[str, Any], matches[0])


def schema_closure(document: dict[str, Any], commands: list[dict[str, Any]]) -> dict[str, Any]:
    components = document["components"]
    pending: list[tuple[str, str]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if reference is not None:
                prefix = "#/components/"
                assert reference.startswith(prefix), reference
                group, name = reference.removeprefix(prefix).split("/", 1)
                pending.append((group, name.replace("~1", "/").replace("~0", "~")))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for command in commands:
        collect(command["responses"])
        collect(command["stdout_schemas"])

    selected: dict[str, dict[str, Any]] = {}
    while pending:
        group, name = pending.pop()
        definitions = selected.setdefault(group, {})
        if name in definitions:
            continue
        assert name in components[group], name
        definitions[name] = components[group][name]
        collect(definitions[name])
    return selected


def validate_output(schema: dict[str, Any], value: Any, components: dict[str, Any]) -> None:
    root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "components": {"schemas": components},
        "allOf": [schema],
    }
    Draft202012Validator(root, format_checker=FormatChecker()).validate(value)


def valid_value(schema: dict[str, Any], components: dict[str, Any]) -> Any:
    """Create a small valid example from the published schema definition."""
    reference = schema.get("$ref")
    if reference is not None:
        name = reference.removeprefix("#/components/schemas/")
        return valid_value(components[name], components)
    if "const" in schema:
        return schema["const"]
    choices = schema.get("anyOf") or schema.get("oneOf")
    if choices:
        return valid_value(
            next(choice for choice in choices if choice.get("type") != "null"), components
        )
    if "enum" in schema:
        return schema["enum"][0]
    value_type = schema.get("type")
    if value_type == "object":
        return {
            name: valid_value(schema["properties"][name], components)
            for name in schema.get("required", [])
        }
    if value_type == "array":
        return []
    if value_type == "string":
        if schema.get("format") == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        if schema.get("format") == "date-time":
            return "2025-01-01T00:00:00Z"
        return "example"
    if value_type == "integer" or value_type == "number":
        return 1
    if value_type == "boolean":
        return True
    if value_type == "null":
        return None
    raise AssertionError(f"unsupported response schema shape: {schema}")


def test_schema_outputs_match_openapi_and_only_include_reachable_types() -> None:
    cli = load_cli()
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))

    for schema_output in (cli.agent_schema(), cli.admin_schema()):
        commands = schema_output["commands"]
        for command in commands:
            operation = operation_for(document, command["method"], command["path"])
            assert command["responses"] == operation["responses"]
        assert schema_output["components"] == schema_closure(document, commands)


def test_admin_doctor_alias_publishes_status_operation_responses() -> None:
    cli = load_cli()
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    commands = {item["command"]: item for item in cli.admin_schema()["commands"]}
    doctor = commands["admin doctor"]
    status = commands["admin status"]

    assert doctor["method"] == "GET"
    assert doctor["path"] == status["path"] == "/api/v1/admin/status"
    assert doctor["responses"] == status["responses"]
    assert doctor["responses"] == operation_for(document, "GET", doctor["path"])["responses"]


def test_admin_schema_filter_subprocess_is_public_and_sanitizes_invalid_filter() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    filtered = subprocess.run(
        [sys.executable, str(CLI), "admin", "schema", "agents", "create"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert filtered.returncode == 0, filtered.stderr
    selected = json.loads(filtered.stdout)
    assert [item["command"] for item in selected["commands"]] == ["admin agents create"]
    assert "stdout_schemas" in selected["commands"][0]

    invalid = subprocess.run(
        [sys.executable, str(CLI), "admin", "schema", "agents", "st_private-filter"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert "st_private-filter" not in invalid.stderr
    error = json.loads(invalid.stderr)
    validate_output(selected["error_output"]["schema"], error, selected["components"]["schemas"])
    assert error["error"]["code"] == "usage_error"


@pytest.mark.parametrize(
    ("command", "body", "resource", "issued_secret"),
    [
        (
            "accounts create",
            {"username": "person", "password": "long-password-value"},
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "username": "person",
                "active": True,
                "browser_access": False,
                "created_at": "2025-01-01T00:00:00Z",
            },
            False,
        ),
        (
            "accounts create",
            {"username": "person"},
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "username": "person",
                "active": True,
                "browser_access": False,
                "created_at": "2025-01-01T00:00:00Z",
            },
            True,
        ),
        (
            "keys create",
            {"admin_id": "00000000-0000-4000-8000-000000000001", "name": "operator"},
            {
                "id": "00000000-0000-4000-8000-000000000002",
                "admin_id": "00000000-0000-4000-8000-000000000001",
                "name": "operator",
                "display_prefix": "sta_prefix",
                "created_at": "2025-01-01T00:00:00Z",
                "last_used_at": None,
                "expires_at": None,
                "revoked_at": None,
                "available": True,
            },
            True,
        ),
    ],
)
def test_key_issuance_stdout_matches_published_schema_and_saves_private_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    body: dict[str, Any],
    resource: dict[str, Any],
    issued_secret: bool,
) -> None:
    cli = load_cli()
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "https://example.test")
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY", "sta_test-admin-key")
    monkeypatch.delenv("STARTUNNEL_ADMIN_KEY_FILE", raising=False)
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps(body).encode("utf-8"))),
    )
    secret_path = tmp_path / "issued.key"
    secret = "sta_issued-private-value"
    api_response: dict[str, Any] = {
        "resource": resource,
        "operation": {
            "id": "00000000-0000-4000-8000-000000000003",
            "audit_event_id": "00000000-0000-4000-8000-000000000004",
        },
    }
    if issued_secret:
        api_response["secret"] = secret

    def request(self: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        del self, method, path, kwargs
        return copy.deepcopy(api_response)

    monkeypatch.setattr(cli.StarTunnelTransport, "request", request)
    arguments = ["admin", *command.split(), "--idempotency-key", "test-issue-001"]
    if command != "accounts create" or issued_secret:
        arguments.extend(("--secret-output", str(secret_path)))

    assert cli.main(arguments) == 0
    output = json.loads(capsys.readouterr().out)
    admin_command = next(
        item for item in cli.admin_schema()["commands"] if item["command"] == "admin " + command
    )
    output_schema = admin_command["stdout_schemas"]["200"]
    assert "secret" not in output_schema["properties"]
    assert "secret" not in output_schema.get("required", [])
    validate_output(output_schema, output, cli.admin_schema()["components"]["schemas"])
    assert "secret" not in output
    if issued_secret:
        assert output["secret_output"] == str(secret_path)
        assert secret_path.read_bytes() == (secret + "\n").encode()
        if os.name == "posix":
            assert secret_path.stat().st_mode & 0o777 == 0o600
    else:
        assert "secret_output" not in output
        assert not secret_path.exists()


def test_cli_error_schema_accepts_local_and_http_errors_but_api_error_stays_raw(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = load_cli()
    public_schema = cli.agent_schema()
    error_schema = public_schema["error_output"]["schema"]
    monkeypatch.delenv("STARTUNNEL_BASE_URL", raising=False)
    monkeypatch.delenv("STARTUNNEL_AGENT_KEY", raising=False)
    monkeypatch.delenv("STARTUNNEL_AGENT_KEY_FILE", raising=False)

    assert cli.main(["me"]) == 1
    local_error = json.loads(capsys.readouterr().err)
    validate_output(error_schema, local_error, public_schema["components"]["schemas"])
    assert local_error["error"]["code"] == "configuration_error"

    api_error_schema = public_schema["commands"][0]["responses"]["401"]["content"][
        "application/json"
    ]["schema"]
    api_error = {
        "error": {
            "code": "invalid_credential",
            "message": "The credential is not valid.",
            "request_id": "request-1",
            "field_errors": [],
        }
    }
    validate_output(api_error_schema, api_error, public_schema["components"]["schemas"])

    monkeypatch.setenv("STARTUNNEL_BASE_URL", "https://example.test")
    monkeypatch.setenv("STARTUNNEL_AGENT_KEY", "st_test-agent-key")

    def fail_request(self: Any, method: str, path: str, **kwargs: Any) -> Any:
        del self, method, path, kwargs
        raise cli.CliError(
            "rate_limited",
            status=429,
            request_id="request-2",
            retry_after=1.5,
            message="Wait before retrying.",
        )

    monkeypatch.setattr(cli.StarTunnelTransport, "request", fail_request)
    assert cli.main(["me"]) == 1
    http_error = json.loads(capsys.readouterr().err)
    validate_output(error_schema, http_error, public_schema["components"]["schemas"])
    assert http_error["error"]["status"] == 429
    assert http_error["error"]["retry_after"] == 1.5
    assert "status" not in api_error["error"]


def test_published_request_fields_and_stdout_schemas_support_workflow_chaining() -> None:
    cli = load_cli()
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schema = cli.agent_schema()
    commands = {item["command"]: item for item in schema["commands"]}
    components = schema["components"]["schemas"]

    def check_request(name: str, value: dict[str, Any]) -> None:
        definition = commands[name]
        assert set(definition["required_fields"]) <= value.keys()
        assert value.keys() <= set(definition["required_fields"] + definition["optional_fields"])

    def response(name: str, value: dict[str, Any], status: str) -> dict[str, Any]:
        definition = commands[name]
        validate_output(definition["stdout_schemas"][status], value, components)
        return value

    create_request = {"cycle": {"root": {"content": {"type": "text", "text": "Root"}}}}
    check_request("create", create_request)
    create_schema = commands["create"]["stdout_schemas"]["201"]
    create_result = valid_value(create_schema, components)
    create_result["tunnel"]["address"] = "address-from-create"
    create_result["cycle"]["root"]["id"] = "00000000-0000-4000-8000-000000000005"
    response("create", create_result, "201")

    reply_request = {
        "address": create_result["tunnel"]["address"],
        "parent_id": create_result["cycle"]["root"]["id"],
        "content": {"type": "text", "text": "Reply"},
    }
    check_request("reply", reply_request)
    response("reply", valid_value(commands["reply"]["stdout_schemas"]["201"], components), "201")

    publish_request = {"address": reply_request["address"], "text": "Beacon"}
    check_request("beacon-publish", publish_request)
    published = valid_value(commands["beacon-publish"]["stdout_schemas"]["201"], components)
    published["beacon"]["id"] = "00000000-0000-4000-8000-000000000006"
    response("beacon-publish", published, "201")

    withdraw_request = {
        "address": publish_request["address"],
        "beacon_id": published["beacon"]["id"],
    }
    check_request("beacon-withdraw", withdraw_request)
    response(
        "beacon-withdraw",
        valid_value(commands["beacon-withdraw"]["stdout_schemas"]["200"], components),
        "200",
    )

    board = valid_value(commands["beacon-board"]["stdout_schemas"]["200"], components)
    board["items"] = [valid_value(document["components"]["schemas"]["BeaconBoardItem"], components)]
    board["items"][0]["address"] = publish_request["address"]
    board["next_page_cursor"] = "next-page"
    response("beacon-board", board, "200")
    page_request = {"page_cursor": board["next_page_cursor"]}
    check_request("beacon-board", page_request)
    final_board_page = {**board, "items": [], "next_page_cursor": None, "complete": True}
    response("beacon-board", final_board_page, "200")
    enter_request = {"address": board["items"][0]["address"]}
    check_request("enter", enter_request)
    response("enter", valid_value(commands["enter"]["stdout_schemas"]["200"], components), "200")

    tree_page = valid_value(commands["read-tree"]["stdout_schemas"]["200"], components)
    tree_page["next_page_cursor"] = "tree-page"
    tree_page["complete"] = False
    response("read-tree", tree_page, "200")
    tree_request = {
        "address": create_result["tunnel"]["address"],
        "snapshot_cursor": tree_page["snapshot_cursor"],
        "page_cursor": tree_page["next_page_cursor"],
    }
    check_request("read-tree", tree_request)
    final_tree_page = {**tree_page, "next_page_cursor": None, "complete": True}
    response("read-tree", final_tree_page, "200")

    close_request = {
        "address": create_result["tunnel"]["address"],
        "expected_cycle_id": create_result["cycle"]["id"],
    }
    check_request("close", close_request)
    validate_output(commands["close"]["stdout_schemas"]["204"], {}, components)


def test_close_prints_204_empty_object_matching_published_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = load_cli()
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "https://example.test")
    monkeypatch.setenv("STARTUNNEL_AGENT_KEY", "st_test-agent-key")
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "address": "address-for-close",
                        "expected_cycle_id": "00000000-0000-4000-8000-000000000007",
                    }
                ).encode("utf-8")
            )
        ),
    )
    requests: list[Any] = []

    class EmptyResponse:
        status = 204

        def __enter__(self) -> EmptyResponse:
            return self

        def __exit__(self, *args: Any) -> None:
            del args

        def read(self) -> bytes:
            return b""

    class FakeOpener:
        def open(self, request: Any, *, timeout: int) -> EmptyResponse:
            requests.append((request, timeout))
            return EmptyResponse()

    monkeypatch.setattr(cli, "build_opener", lambda *_handlers: FakeOpener())

    assert cli.main(["close", "--idempotency-key", "close-test-204"]) == 0
    output = json.loads(capsys.readouterr().out)
    schema = next(item for item in cli.agent_schema()["commands"] if item["command"] == "close")
    response_204 = schema["responses"]["204"]
    assert "content" not in response_204
    validate_output(
        schema["stdout_schemas"]["204"], output, cli.agent_schema()["components"]["schemas"]
    )
    request, _timeout = requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://example.test/api/v1/cycles/close"
    assert request.headers["Idempotency-key"] == "close-test-204"
