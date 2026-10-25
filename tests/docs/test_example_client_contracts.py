"""Outgoing reference-client requests follow the published OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from examples.python.startunnel_client import StarTunnelClient
from jsonschema import Draft202012Validator, FormatChecker
from scripts.json_data import JsonObject, JsonValue, json_array, json_object, json_string

ROOT = Path(__file__).resolve().parents[2]
ID = "5af6d40b-776a-4e3c-9c2c-f01cb2507595"
CONTENT: JsonObject = {"type": "text", "text": "Review the example."}


def _example_value(schema: JsonObject, components: JsonObject) -> JsonValue:
    reference = schema.get("$ref")
    if reference is not None:
        group, name = json_string(reference).removeprefix("#/components/").split("/", 1)
        return _example_value(json_object(json_object(components[group])[name]), components)
    for choice in ("anyOf", "oneOf"):
        if choice in schema:
            return _example_value(json_object(json_array(schema[choice])[0]), components)
    values = schema.get("enum")
    if isinstance(values, list):
        return values[0]
    if "const" in schema:
        return schema["const"]
    match schema.get("type"):
        case "object":
            return {
                name: _example_value(json_object(value), components)
                for name, value in json_object(schema.get("properties", {})).items()
            }
        case "array":
            return []
        case "string":
            return "example-value"
        case "integer":
            return 0
        case "number":
            return 0.0
        case "boolean":
            return False
        case _:
            return None


def _response_body(document: JsonObject, method: str, path: str, status: int) -> JsonValue:
    if status == 204:
        return None
    operation = json_object(json_object(json_object(document["paths"])[path])[method.lower()])
    response = json_object(json_object(operation["responses"])[str(status)])
    media = json_object(json_object(response["content"])["application/json"])
    return _example_value(json_object(media["schema"]), json_object(document["components"]))


def _optional_string(arguments: JsonObject, name: str) -> str | None:
    value = arguments.get(name)
    return None if value is None else json_string(value)


async def _call_method(client: StarTunnelClient, method: str, arguments: JsonObject) -> None:
    address = "example-address"
    key = "example-contract-write"
    content = json_object(arguments.get("root_content", arguments.get("content", CONTENT)))
    match method:
        case "me":
            await client.me()
        case "create_tunnel":
            await client.create_tunnel(
                label=json_string(arguments["label"]),
                root_content=content,
                idempotency_key=key,
                root_session=_optional_string(arguments, "root_session"),
            )
        case "post_reply":
            await client.post_reply(
                address=address,
                parent_id=json_string(arguments["parent_id"]),
                content=content,
                idempotency_key=key,
                session=_optional_string(arguments, "session"),
            )
        case "read_tree":
            await client.read_tree(address=address)
        case "read_message":
            await client.read_message(
                address=address, message_id=json_string(arguments["message_id"])
            )
        case "read_branch":
            await client.read_branch(address=address, leaf_id=json_string(arguments["leaf_id"]))
        case "read_replies":
            await client.read_replies(
                address=address, message_id=json_string(arguments["message_id"])
            )
        case "read_subtree":
            await client.read_subtree(
                address=address, message_id=json_string(arguments["message_id"])
            )
        case "read_leaves":
            await client.read_leaves(address=address)
        case "read_activity":
            await client.read_activity(address=address)
        case "commit_checkpoint":
            await client.commit_checkpoint(address=address, cursor=json_string(arguments["cursor"]))
        case "get_context":
            await client.get_context(
                address=address, focus_message_id=json_string(arguments["focus_message_id"])
            )
        case "search_messages":
            await client.search_messages(
                address=address, query=_optional_string(arguments, "query")
            )
        case "list_participants":
            await client.list_participants(address=address)
        case "close_cycle":
            await client.close_cycle(address=address, expected_cycle_id=ID, idempotency_key=key)
        case "get_tunnel_status":
            await client.get_tunnel_status(address=address)
        case "list_cycles":
            await client.list_cycles(address=address)
        case "start_cycle":
            await client.start_cycle(
                address=address,
                expected_address_generation=1,
                root_content=content,
                idempotency_key=key,
                root_session=_optional_string(arguments, "root_session"),
            )
        case "rollover_cycle":
            await client.rollover_cycle(
                address=address,
                expected_address_generation=1,
                expected_cycle_id=ID,
                root_content=content,
                idempotency_key=key,
                root_session=_optional_string(arguments, "root_session"),
            )
        case _:
            raise AssertionError(f"Unknown client method: {method}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "arguments", "path", "status"),
    [
        ("me", {}, "/me", 200),
        ("create_tunnel", {"label": "Example", "root_content": CONTENT}, "/tunnels", 201),
        ("post_reply", {"parent_id": ID, "content": CONTENT}, "/messages", 201),
        ("read_tree", {}, "/tree", 200),
        ("read_message", {"message_id": ID}, "/tree/message", 200),
        ("read_branch", {"leaf_id": ID}, "/tree/branch", 200),
        ("read_replies", {"message_id": ID}, "/tree/replies", 200),
        ("read_subtree", {"message_id": ID}, "/tree/subtree", 200),
        ("read_leaves", {}, "/tree/leaves", 200),
        ("read_activity", {}, "/activity", 200),
        ("commit_checkpoint", {"cursor": "example-cursor"}, "/activity/checkpoint", 200),
        ("get_context", {"focus_message_id": ID}, "/context", 200),
        ("search_messages", {"query": "review"}, "/search", 200),
        ("list_participants", {}, "/participants", 200),
        ("close_cycle", {"expected_cycle_id": ID}, "/cycles/close", 204),
        ("get_tunnel_status", {}, "/tunnel/status", 200),
        ("list_cycles", {}, "/cycles", 200),
        (
            "start_cycle",
            {"expected_address_generation": 1, "root_content": CONTENT},
            "/cycles/start",
            201,
        ),
        (
            "rollover_cycle",
            {"expected_address_generation": 1, "expected_cycle_id": ID, "root_content": CONTENT},
            "/cycles/rollover",
            201,
        ),
    ],
)
async def test_reference_client_request_contract(
    method: str, arguments: JsonObject, path: str, status: int
) -> None:
    document = json_object(json.loads((ROOT / "generated/openapi.json").read_text()))
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1" + path
        operation = json_object(
            json_object(json_object(document["paths"])[request.url.path])[request.method.lower()]
        )
        if "requestBody" in operation:
            schema = json_object(
                json_object(
                    json_object(json_object(operation["requestBody"])["content"])[
                        "application/json"
                    ]
                )["schema"]
            )
            Draft202012Validator(
                {**schema, "components": document["components"]},
                format_checker=FormatChecker(),
            ).validate(json.loads(request.content))
        for parameter_value in json_array(operation.get("parameters", [])):
            parameter = json_object(parameter_value)
            if parameter["in"] == "header" and parameter.get("required"):
                assert request.headers.get(json_string(parameter["name"]))
        return httpx.Response(
            status, json=_response_body(document, request.method, request.url.path, status)
        )

    client = StarTunnelClient("https://startunnel.example", "example-key")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example", transport=httpx.MockTransport(handler)
    )
    async with client:
        await _call_method(client, method, arguments)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create_tunnel", "post_reply", "start_cycle", "rollover_cycle"])
async def test_message_writes_preserve_session_labels(method: str) -> None:
    requests: list[JsonObject] = []
    document = json_object(json.loads((ROOT / "generated/openapi.json").read_text()))

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json_object(json.loads(request.content)))
        return httpx.Response(
            201, json=_response_body(document, request.method, request.url.path, 201)
        )

    client = StarTunnelClient("https://startunnel.example", "example-key")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example", transport=httpx.MockTransport(handler)
    )
    kwargs: JsonObject = {"idempotency_key": "example-session-write"}
    if method == "create_tunnel":
        kwargs.update(label="Example", root_content=CONTENT, root_session="review-session")
    elif method == "post_reply":
        kwargs.update(
            address="example-address", parent_id=ID, content=CONTENT, session="review-session"
        )
    else:
        kwargs.update(
            address="example-address",
            expected_address_generation=1,
            root_content=CONTENT,
            root_session="review-session",
        )
        if method == "rollover_cycle":
            kwargs["expected_cycle_id"] = ID
    async with client:
        await _call_method(client, method, kwargs)
    message = (
        requests[0]
        if method == "post_reply"
        else json_object(json_object(requests[0]["cycle"])["root"])
    )
    assert message["session"] == "review-session"


def test_catalog_runs_python_examples_and_the_cli_tutorial(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import run_example_catalog

    for name in (
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
        "STARTUNNEL_RECEIVER_KEYS",
    ):
        monkeypatch.setenv(name, "example-setting")
    commands: list[list[str]] = []
    monkeypatch.setattr(run_example_catalog, "_safe_run", commands.append)

    assert run_example_catalog.main() == 0
    assert len(commands) == len(run_example_catalog.PYTHON_EXAMPLES) + 1
    assert commands[-1][1:] == [str(ROOT / "cli/star_tunnel.py"), "tutorial"]
    assert all("example-setting" not in argument for command in commands for argument in command)
