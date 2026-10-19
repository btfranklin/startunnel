"""Outgoing reference-client requests follow the published OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from examples.python.startunnel_client import StarTunnelClient
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
ID = "5af6d40b-776a-4e3c-9c2c-f01cb2507595"
CONTENT = {"type": "text", "text": "Review the example."}


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
    method: str, arguments: dict[str, Any], path: str, status: int
) -> None:
    document = json.loads((ROOT / "generated/openapi.json").read_text())
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1" + path
        operation = document["paths"][request.url.path][request.method.lower()]
        if "requestBody" in operation:
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            Draft202012Validator(
                {**schema, "components": document["components"]},
                format_checker=FormatChecker(),
            ).validate(json.loads(request.content))
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "header" and parameter.get("required"):
                assert request.headers.get(parameter["name"])
        return httpx.Response(status, json={} if status != 204 else None)

    kwargs = dict(arguments)
    if method not in {"me", "create_tunnel"}:
        kwargs["address"] = "example-address"
    if method in {"create_tunnel", "post_reply", "close_cycle", "start_cycle", "rollover_cycle"}:
        kwargs["idempotency_key"] = "example-contract-write"
    client = StarTunnelClient("https://startunnel.example", "example-key")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example", transport=httpx.MockTransport(handler)
    )
    async with client:
        await getattr(client, method)(**kwargs)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create_tunnel", "post_reply", "start_cycle", "rollover_cycle"])
async def test_message_writes_preserve_session_labels(method: str) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(201, json={})

    client = StarTunnelClient("https://startunnel.example", "example-key")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example", transport=httpx.MockTransport(handler)
    )
    kwargs: dict[str, Any] = {"idempotency_key": "example-session-write"}
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
        await getattr(client, method)(**kwargs)
    message = requests[0] if method == "post_reply" else requests[0]["cycle"]["root"]
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
