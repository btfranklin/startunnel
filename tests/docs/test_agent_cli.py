from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "cli" / "star_tunnel.py"


@dataclass
class Response:
    status: int
    value: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    declared_length: int | None = None


@dataclass
class Scenario:
    responses: list[Response]
    requests: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def serve(*responses: Response) -> Iterator[tuple[str, Scenario]]:
    scenario = Scenario(list(responses))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            scenario.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                }
            )
            response = scenario.responses.pop(0)
            encoded = (
                json.dumps(response.value, separators=(",", ":")).encode("utf-8")
                if response.value is not None
                else b""
            )
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Length",
                str(
                    response.declared_length
                    if response.declared_length is not None
                    else len(encoded)
                ),
            )
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        yield f"http://{host}:{port}", scenario
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_cli(
    base_url: str,
    *arguments: str,
    body: dict[str, Any] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "STARTUNNEL_BASE_URL": base_url,
            "STARTUNNEL_AGENT_KEY": "st_test-agent-key",
        }
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, str(CLI), *arguments],
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


@pytest.mark.parametrize(
    ("action", "arguments", "body", "suffix", "status", "response"),
    [
        (
            "create",
            ["--idempotency-key", "create-test-001"],
            {"label": "Example", "cycle": {"root": {"content": {"type": "text", "text": "Hi"}}}},
            "/tunnels",
            201,
            {"created": True},
        ),
        (
            "reply",
            ["--idempotency-key", "reply-test-001"],
            {"address": "glyphs", "parent_id": "parent", "content": {"type": "text", "text": "Hi"}},
            "/messages",
            201,
            {"posted": True},
        ),
        (
            "wait",
            [],
            {"address": "glyphs", "after_cursor": "cursor"},
            "/activity",
            200,
            {"events": [], "next_cursor": "next", "has_more": False},
        ),
        (
            "read-context",
            [],
            {"address": "glyphs", "focus_message_id": "focus", "max_items": 10},
            "/context",
            200,
            {"items": []},
        ),
        (
            "checkpoint",
            [],
            {"address": "glyphs", "cursor": "cursor"},
            "/activity/checkpoint",
            200,
            {"advanced": True},
        ),
        (
            "close",
            ["--idempotency-key", "close-test-001"],
            {"address": "glyphs", "expected_cycle_id": "cycle"},
            "/cycles/close",
            204,
            {},
        ),
    ],
)
def test_commands_preserve_public_request_bodies_and_map_scope(
    action: str,
    arguments: list[str],
    body: dict[str, Any],
    suffix: str,
    status: int,
    response: dict[str, Any],
) -> None:
    with serve(Response(status, None if status == 204 else response)) as (base_url, scenario):
        result = run_cli(base_url, action, *arguments, body=body)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == response
    assert result.stderr == ""
    request = scenario.requests[0]
    assert request["path"] == "/api/v1" + suffix
    expected_body = dict(body)
    if action == "wait":
        expected_body["wait_seconds"] = 20
    assert request["body"] == expected_body
    assert request["headers"]["Authorization"] == "Bearer st_test-agent-key"
    if action in {"create", "reply", "close"}:
        assert request["headers"]["Idempotency-Key"] == arguments[1]
    else:
        assert "Idempotency-Key" not in request["headers"]


def test_me_uses_get_and_returns_the_native_response() -> None:
    response = {"id": "agent-id", "name": "Build agent", "limits": {"messages": 100}}
    with serve(Response(200, response)) as (base_url, scenario):
        result = run_cli(base_url, "me")

    assert result.returncode == 0
    assert json.loads(result.stdout) == response
    assert scenario.requests == [
        {
            "method": "GET",
            "path": "/api/v1/me",
            "headers": scenario.requests[0]["headers"],
            "body": None,
        }
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.test",
        "http://localhost:8000?credential=visible-marker",
        "http://user:visible-marker@localhost:8000",
        "http://localhost:8000/#visible-marker",
        "http://local\nhost:8000",
    ],
)
def test_cli_rejects_unsafe_base_urls_without_exposing_them(base_url: str) -> None:
    result = run_cli(base_url, "me")

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "configuration_error"
    assert "visible-marker" not in result.stderr
    assert "st_test-agent-key" not in result.stderr


def test_api_error_keeps_safe_fields_and_discards_server_text() -> None:
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    response = {
        "error": {
            "code": "tunnel_unavailable",
            "message": "private-message-visible-marker",
            "request_id": request_id,
            "field_errors": [{"message": "another-visible-marker"}],
        }
    }
    with serve(Response(404, response)) as (base_url, _scenario):
        result = run_cli(base_url, "checkpoint", body={"address": "private-glyphs", "cursor": "c"})

    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error == {
        "code": "tunnel_unavailable",
        "message": "The StarTunnel request failed.",
        "status": 404,
        "request_id": request_id,
    }
    assert "visible-marker" not in result.stderr
    assert "private-glyphs" not in result.stderr


def test_api_error_discards_reflected_credentials_in_safe_fields() -> None:
    reflected_key = "st_reflected-visible-marker"
    response = {
        "error": {
            "code": reflected_key,
            "message": reflected_key,
            "request_id": reflected_key,
        }
    }
    with serve(Response(400, response, headers={"X-Request-ID": reflected_key})) as (
        base_url,
        _scenario,
    ):
        result = run_cli(base_url, "checkpoint", body={"address": "glyphs", "cursor": "c"})

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == {
        "code": "http_error",
        "message": "The StarTunnel request failed.",
        "status": 400,
    }
    assert reflected_key not in result.stderr


def test_non_ascii_agent_key_produces_a_sanitized_configuration_error() -> None:
    with serve(Response(200, {"id": "unused"})) as (base_url, scenario):
        result = run_cli(
            base_url,
            "me",
            extra_env={"STARTUNNEL_AGENT_KEY": "st_non-ascii-visible-marker-🔑"},
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "configuration_error"
    assert "visible-marker" not in result.stderr
    assert scenario.requests == []


def test_cli_refuses_redirects_and_does_not_send_a_second_request() -> None:
    with serve(
        Response(
            307,
            {"error": {"message": "redirect-visible-marker"}},
            headers={"Location": "/credential-collector-visible-marker"},
        )
    ) as (base_url, scenario):
        result = run_cli(base_url, "me")

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == {
        "code": "http_error",
        "message": "The StarTunnel request failed.",
        "status": 307,
    }
    assert len(scenario.requests) == 1
    assert "visible-marker" not in result.stderr


def test_cli_rejects_a_declared_oversized_response() -> None:
    with serve(Response(200, declared_length=2 * 1024 * 1024 + 1)) as (base_url, _scenario):
        result = run_cli(base_url, "me")

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "response_too_large"


def test_tutorial_runs_the_complete_exchange_with_separate_credentials() -> None:
    sender_id = "11854e39-39cb-4f66-b963-9035064b72eb"
    receiver_id = "922395a1-c9b9-45d6-8eb9-0fe568c8f44d"
    cycle_id = "352ba38a-d722-4efe-b86e-26ef39638404"
    root_id = "5af6d40b-776a-4e3c-9c2c-f01cb2507595"
    reply_id = "bc14fc4f-8105-4940-9474-f40a73364ff3"
    address = "synthetic-glyph-address"
    responses = [
        Response(200, {"agent": {"id": sender_id}}),
        Response(200, {"agent": {"id": receiver_id}}),
        Response(
            201,
            {
                "tunnel": {"address": address, "display_address": "synthetic display"},
                "cycle": {"id": cycle_id, "root": {"id": root_id}},
            },
        ),
        Response(200, {"nodes": [{"id": root_id}]}),
        Response(201, {"message": {"id": reply_id, "parent_id": root_id}}),
        Response(200, {"message": {"id": reply_id}}),
        Response(
            200,
            {
                "cycle_id": cycle_id,
                "items": [
                    {"reason": "ancestor", "message": {"id": root_id}},
                    {"reason": "focus", "message": {"id": reply_id}},
                ],
            },
        ),
        Response(204),
        Response(
            200,
            {
                "nodes": [
                    {"id": root_id, "parent_id": None},
                    {"id": reply_id, "parent_id": root_id},
                ]
            },
        ),
    ]
    with serve(*responses) as (base_url, scenario):
        result = run_cli(
            base_url,
            "tutorial",
            extra_env={
                "STARTUNNEL_SENDER_KEY": "st_sender-test-key",
                "STARTUNNEL_RECEIVER_KEY": "st_receiver-test-key",
            },
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "complete",
        "checks": [
            "sender_identity",
            "receiver_identity",
            "root_created",
            "root_read",
            "reply_created",
            "reply_read",
            "context_read",
            "cycle_closed",
            "retained_tree_read",
        ],
        "display_address": "synthetic display",
        "cycle_id": cycle_id,
        "root_message_id": root_id,
        "reply_message_id": reply_id,
        "retained_message_count": 2,
    }
    assert result.stderr == ""
    assert [request["path"] for request in scenario.requests] == [
        "/api/v1/me",
        "/api/v1/me",
        "/api/v1/tunnels",
        "/api/v1/tree",
        "/api/v1/messages",
        "/api/v1/tree/message",
        "/api/v1/context",
        "/api/v1/cycles/close",
        "/api/v1/tree",
    ]
    assert [request["headers"]["Authorization"] for request in scenario.requests] == [
        "Bearer st_sender-test-key",
        "Bearer st_receiver-test-key",
        "Bearer st_sender-test-key",
        "Bearer st_receiver-test-key",
        "Bearer st_receiver-test-key",
        "Bearer st_sender-test-key",
        "Bearer st_sender-test-key",
        "Bearer st_sender-test-key",
        "Bearer st_receiver-test-key",
    ]
    write_requests = [scenario.requests[index] for index in (2, 4, 7)]
    for request in write_requests:
        uuid.UUID(request["headers"]["Idempotency-Key"])


def test_tutorial_rejects_one_identity_used_as_both_agents() -> None:
    agent_id = "11854e39-39cb-4f66-b963-9035064b72eb"
    with serve(
        Response(200, {"agent": {"id": agent_id}}),
        Response(200, {"agent": {"id": agent_id}}),
    ) as (
        base_url,
        scenario,
    ):
        result = run_cli(
            base_url,
            "tutorial",
            extra_env={
                "STARTUNNEL_SENDER_KEY": "st_first-test-key",
                "STARTUNNEL_RECEIVER_KEY": "st_second-test-key",
            },
        )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "configuration_error"
    assert len(scenario.requests) == 2


def test_downloadable_cli_uses_python_3_10_syntax_and_only_the_standard_library() -> None:
    source = CLI.read_text(encoding="utf-8")
    ast.parse(source, filename=str(CLI), feature_version=(3, 10))

    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            assert node.module is not None
            imports.add(node.module.split(".", 1)[0])
    assert imports <= {
        "argparse",
        "collections",
        "contextlib",
        "dataclasses",
        "ipaddress",
        "http",
        "json",
        "os",
        "re",
        "sys",
        "typing",
        "urllib",
        "uuid",
    }
