"""Example clients and source files stay runnable against the current contract."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TypedDict

import httpx
import pytest
from examples.python.startunnel_client import StarTunnelClient, StarTunnelError
from scripts import run_tutorial
from scripts.json_data import JsonValue, json_object
from tests.docs.test_example_client_contracts import _response_body

ROOT = Path(__file__).resolve().parents[2]


class CapturedRun(TypedDict, total=False):
    command: list[str]
    cwd: Path
    env: dict[str, str]
    check: bool


@pytest.mark.asyncio
async def test_reference_client_keeps_tree_values_in_request_bodies() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/tree"
        body = request.read().decode("utf-8")
        assert "private-address" in body
        assert "private-cycle" in body
        assert "private-cursor" in body
        assert "private-address" not in request.url.path
        return httpx.Response(
            200,
            request=request,
            json={
                **json_object(
                    _response_body(
                        json_object(json.loads((ROOT / "generated/openapi.json").read_text())),
                        "POST",
                        "/api/v1/tree",
                        200,
                    )
                ),
                "snapshot_cursor": "private-cursor",
            },
        )

    client = StarTunnelClient("https://startunnel.example", "safe-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.read_tree(
            address="private-address",
            cycle_id="private-cycle",
            snapshot_cursor="private-cursor",
        )
    finally:
        await client.aclose()
    assert result["snapshot_cursor"] == "private-cursor"


@pytest.mark.asyncio
async def test_reference_client_error_has_request_id_but_not_bearer_values() -> None:
    address = "address-value-must-stay-private"
    parent_id = "parent-value-must-stay-private"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            request=request,
            json={
                "error": {
                    "code": "lifecycle_conflict",
                    "message": "The cycle state changed.",
                    "request_id": "request-safe-42",
                    "field_errors": [],
                }
            },
        )

    client = StarTunnelClient("https://startunnel.example", "safe-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StarTunnelError) as captured:
        await client.post_reply(
            address=address,
            parent_id=parent_id,
            content={"type": "text", "text": "private payload"},
            idempotency_key="example-reply-0001",
        )
    await client.aclose()
    assert captured.value.code == "lifecycle_conflict"
    assert captured.value.request_id == "request-safe-42"
    assert address not in str(captured.value)
    assert parent_id not in str(captured.value)
    assert "private payload" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", ["private-invalid-nodes", [{"id": "private-incomplete-message"}]])
async def test_reference_client_rejects_malformed_success_responses(nodes: JsonValue) -> None:
    document = json_object(json.loads((ROOT / "generated/openapi.json").read_text()))
    body = json_object(_response_body(document, "POST", "/api/v1/tree", 200))
    body["nodes"] = nodes

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, headers={"X-Request-ID": "safe-validation-request"})

    client = StarTunnelClient("https://startunnel.example", "safe-placeholder")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://startunnel.example", transport=httpx.MockTransport(handler)
    )
    async with client:
        with pytest.raises(StarTunnelError) as captured:
            await client.read_tree(address="private-address")
    assert captured.value.code == "invalid_response"
    assert captured.value.request_id == "safe-validation-request"
    assert "private" not in str(captured.value)


def test_tutorial_environment_file_declares_its_client_settings() -> None:
    values = run_tutorial.read_env(ROOT / "examples/.env.tutorial.example")
    assert {
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
    } <= values.keys()
    assert "OPENAI_API_KEY" not in values


def test_tutorial_runner_rejects_a_missing_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / ".env.tutorial"
    monkeypatch.setattr(run_tutorial, "TUTORIAL_ENV", missing)

    with pytest.raises(SystemExit, match="Create \\.env\\.tutorial"):
        run_tutorial.main()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are required")
def test_tutorial_runner_rejects_unsafe_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env.tutorial"
    env_path.write_text("STARTUNNEL_BASE_URL=http://localhost:8000\n", encoding="utf-8")
    env_path.chmod(0o644)
    monkeypatch.setattr(run_tutorial, "TUTORIAL_ENV", env_path)

    with pytest.raises(SystemExit, match="chmod 0600"):
        run_tutorial.main()


def test_tutorial_runner_rejects_placeholder_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env.tutorial"
    env_path.write_text(
        "STARTUNNEL_BASE_URL=http://localhost:8000\n"
        "STARTUNNEL_SENDER_KEY=replace-with-sender-key\n"
        "STARTUNNEL_RECEIVER_KEY=replace-with-receiver-key\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(run_tutorial, "TUTORIAL_ENV", env_path)

    with pytest.raises(SystemExit, match="Replace every tutorial placeholder"):
        run_tutorial.main()


def test_tutorial_runner_constructs_the_profile_gated_compose_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env.tutorial"
    env_path.write_text(
        "STARTUNNEL_BASE_URL=http://localhost:8000\n"
        "STARTUNNEL_SENDER_KEY=safe-test-sender\n"
        "STARTUNNEL_RECEIVER_KEY=safe-test-receiver\n"
        "STARTUNNEL_TUTORIAL_PRIORITY=4\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(run_tutorial, "TUTORIAL_ENV", env_path)
    captured: CapturedRun = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update({"command": command, "cwd": cwd, "env": env, "check": check})
        return subprocess.CompletedProcess(command, returncode=7)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_tutorial.main() == 7
    assert captured["command"] == [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.dev.yaml",
        "--profile",
        "tutorial",
        "run",
        "--rm",
        "tutorial",
    ]
    assert captured["cwd"] == run_tutorial.PROJECT_ROOT
    assert captured["check"] is False
    environment = captured["env"]
    assert environment["STARTUNNEL_TUTORIAL_BASE_URL"] == "http://web:8000"
    assert environment["STARTUNNEL_TUTORIAL_PRIORITY"] == "4"


def test_tutorial_runner_does_not_start_local_dependencies_for_a_hosted_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env.tutorial"
    env_path.write_text(
        "STARTUNNEL_BASE_URL=https://beta.startunnel.example\n"
        "STARTUNNEL_SENDER_KEY=safe-test-sender\n"
        "STARTUNNEL_RECEIVER_KEY=safe-test-receiver\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(run_tutorial, "TUTORIAL_ENV", env_path)
    captured: CapturedRun = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update({"command": command, "cwd": cwd, "env": env, "check": check})
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_tutorial.main() == 0
    assert "--no-deps" in captured["command"]
    assert captured["env"]["STARTUNNEL_TUTORIAL_BASE_URL"] == ("https://beta.startunnel.example")
