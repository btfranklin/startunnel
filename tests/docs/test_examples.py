"""Example clients and source files stay runnable against the current contract."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from examples.python.startunnel_client import StarTunnelClient, StarTunnelError
from scripts import run_tutorial

ROOT = Path(__file__).resolve().parents[2]


def test_every_python_example_compiles() -> None:
    for path in sorted((ROOT / "examples").rglob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


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
            json={"nodes": [], "snapshot_cursor": "private-cursor", "page_cursor": None},
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


def test_tutorial_script_has_no_secret_prints() -> None:
    path = ROOT / "examples/python/tutorial_exchange.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    printed_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "print":
            continue
        printed_names.update(
            child.id
            for argument in node.args
            for child in ast.walk(argument)
            if isinstance(child, ast.Name)
        )
    assert not {
        "sender_key",
        "receiver_key",
        "snapshot_cursor",
        "page_cursor",
        "activity_cursor",
    }.intersection(printed_names)


def test_tutorial_environment_file_contains_placeholders_only() -> None:
    text = (ROOT / "examples/.env.tutorial.example").read_text(encoding="utf-8")
    assert "replace-with" in text
    assert "OPENAI_API_KEY" not in text
    assert "st_" not in text


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
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, cwd=cwd, env=env, check=check)
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
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, cwd=cwd, env=env, check=check)
        return subprocess.CompletedProcess(command, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_tutorial.main() == 0
    assert "--no-deps" in captured["command"]
    assert captured["env"]["STARTUNNEL_TUTORIAL_BASE_URL"] == ("https://beta.startunnel.example")
