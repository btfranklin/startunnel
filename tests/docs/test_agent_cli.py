from __future__ import annotations

import ast
import errno
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from scripts.stamp_client import stamp_client

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "cli" / "star_tunnel.py"


@dataclass
class Response:
    status: int
    value: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    declared_content_length: str | None = None
    omit_content_length: bool = False
    chunked: bool = False


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
                response.raw
                if response.raw is not None
                else (
                    json.dumps(response.value, separators=(",", ":")).encode("utf-8")
                    if response.value is not None
                    else b""
                )
            )
            if response.chunked:
                self.protocol_version = "HTTP/1.1"
                self.close_connection = True
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            if response.chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif not response.omit_content_length:
                self.send_header(
                    "Content-Length",
                    response.declared_content_length or str(len(encoded)),
                )
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                if response.chunked:
                    for offset in range(0, len(encoded), 17):
                        chunk = encoded[offset : offset + 17]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                elif encoded:
                    self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
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
            "enter",
            [],
            {"address": "glyphs", "limit": 20, "max_bytes": 262144},
            "/enter",
            200,
            {"status": "active", "recent_messages": []},
        ),
        (
            "participants",
            [],
            {"address": "glyphs", "limit": 100},
            "/participants",
            200,
            {"participants": [], "complete": True},
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
            "beacon-publish",
            ["--idempotency-key", "beacon-publish-001"],
            {"address": "glyphs", "text": "Review the draft."},
            "/beacons",
            201,
            {"beacon": {"id": "beacon-id"}, "idempotent_replay": False},
        ),
        (
            "beacon-withdraw",
            ["--idempotency-key", "beacon-withdraw-001"],
            {"address": "glyphs", "beacon_id": "beacon-id"},
            "/beacons/withdraw",
            200,
            {"beacon": {"id": "beacon-id"}, "idempotent_replay": False},
        ),
        (
            "beacon-wait",
            [],
            {"after_cursor": "board-cursor"},
            "/beacons/wait",
            200,
            {"refresh_required": False, "board_cursor": "board-cursor"},
        ),
        (
            "beacon-board",
            [],
            {},
            "/beacons/board",
            200,
            {"items": [], "next_page_cursor": None, "complete": True},
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
    if action in {"wait", "beacon-wait"}:
        expected_body["wait_seconds"] = 20
    assert request["body"] == expected_body
    assert request["headers"]["Authorization"] == "Bearer st_test-agent-key"
    assert request["headers"]["X-Startunnel-Client-Version"] == "unreleased"
    if action in {"create", "reply", "close", "beacon-publish", "beacon-withdraw"}:
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
    assert scenario.requests[0]["headers"]["X-Startunnel-Client-Version"] == "unreleased"


@pytest.mark.parametrize(
    ("action", "suffix", "status"),
    [
        ("read-tree", "/tree", 200),
        ("read-message", "/tree/message", 200),
        ("read-branch", "/tree/branch", 200),
        ("read-replies", "/tree/replies", 200),
        ("read-subtree", "/tree/subtree", 200),
        ("read-leaves", "/tree/leaves", 200),
        ("search", "/search", 200),
        ("status", "/tunnel/status", 200),
        ("cycles", "/cycles", 200),
        ("start", "/cycles/start", 201),
        ("rollover", "/cycles/rollover", 201),
    ],
)
def test_complete_agent_commands_preserve_bodies_and_write_headers(
    action: str, suffix: str, status: int
) -> None:
    cli = _load_cli_module()
    body = cli.AGENT_COMMANDS[action]["example"]
    arguments = (
        ["--idempotency-key", "complete-command-001"] if action in {"start", "rollover"} else []
    )
    response = {"result": action}
    with serve(Response(status, response)) as (base_url, scenario):
        result = run_cli(base_url, action, *arguments, body=body)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == response
    assert len(scenario.requests) == 1
    request = scenario.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/api/v1" + suffix
    assert request["body"] == body
    assert request["headers"]["Authorization"] == "Bearer st_test-agent-key"
    if action in {"start", "rollover"}:
        assert request["headers"]["Idempotency-Key"] == "complete-command-001"
        assert request["body"]["cycle"]["root"]["session"] == "review"
    else:
        assert "Idempotency-Key" not in request["headers"]


@pytest.mark.parametrize(
    "base_url",
    [
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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://web:8000",
        "http://192.168.1.20:8000",
        "http://10.8.0.5:8080",
    ],
)
def test_cli_accepts_http_origins_for_private_networks(base_url: str) -> None:
    assert _load_cli_module().validate_base_url(base_url) == base_url


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (
            404,
            "tunnel_unavailable",
            "This tunnel is not available. Check the address or create a new tunnel.",
        ),
        (500, "internal_error", "An unexpected server error occurred."),
    ],
)
def test_api_error_keeps_authored_message_and_discards_unknown_fields(
    status: int, code: str, message: str
) -> None:
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    response = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "field_errors": [],
            "unexpected": "unknown-field-visible-marker",
        }
    }
    with serve(Response(status, response)) as (base_url, _scenario):
        result = run_cli(base_url, "checkpoint", body={"address": "private-glyphs", "cursor": "c"})

    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error == {
        "code": code,
        "message": message,
        "status": status,
        "request_id": request_id,
        "field_errors": [],
    }
    assert "visible-marker" not in result.stderr
    assert "private-glyphs" not in result.stderr


def test_missing_field_diagnostic_survives_transport_and_client() -> None:
    response = {
        "error": {
            "code": "invalid_request",
            "message": "The request is not valid. Correct the marked fields and try again.",
            "request_id": "064c1aab-8f0f-42d3-b2db-f132ba86363c",
            "field_errors": [
                {"field": "body.focus_message_id", "message": "Field required", "extra": "ignored"}
            ],
        }
    }
    with serve(Response(400, response)) as (base_url, scenario):
        result = run_cli(base_url, "read-context", body={"address": "glyphs"})

    assert result.returncode == 1
    assert scenario.requests[0]["path"] == "/api/v1/context"
    assert json.loads(result.stderr)["error"] == {
        "code": "invalid_request",
        "message": "The request is not valid. Correct the marked fields and try again.",
        "status": 400,
        "request_id": "064c1aab-8f0f-42d3-b2db-f132ba86363c",
        "field_errors": [{"field": "body.focus_message_id", "message": "Field required"}],
    }


def test_update_required_invalid_request_message_and_field_error_are_preserved() -> None:
    message = "Update the StarTunnel CLI to version 0.3.0 or later."
    response = {
        "error": {
            "code": "invalid_request",
            "message": message,
            "field_errors": [
                {
                    "field": "X-StarTunnel-Client-Version",
                    "message": "This client version is no longer supported.",
                }
            ],
        }
    }
    with serve(Response(400, response)) as (base_url, scenario):
        result = run_cli(base_url, "me")

    assert result.returncode == 1
    assert result.stdout == ""
    assert len(scenario.requests) == 1
    assert scenario.requests[0]["headers"]["X-Startunnel-Client-Version"] == "unreleased"
    assert json.loads(result.stderr)["error"] == {
        "code": "invalid_request",
        "message": message,
        "status": 400,
        "field_errors": [
            {
                "field": "X-StarTunnel-Client-Version",
                "message": "This client version is no longer supported.",
            }
        ],
    }


@pytest.mark.parametrize("write", [False, True])
def test_unicode_results_work_with_ascii_output_encoding(write: bool) -> None:
    value = "caf\u00e9 \U0001f680"
    response = (
        {"message": {"content": {"type": "text", "text": value}}}
        if write
        else {"agent": {"name": value}}
    )
    arguments = ("reply", "--idempotency-key", "unicode-output-001") if write else ("me",)
    body = (
        {
            "address": "test-address",
            "parent_id": "test-parent",
            "content": {"type": "text", "text": "Hello"},
        }
        if write
        else None
    )
    with serve(Response(201 if write else 200, response)) as (base_url, scenario):
        result = run_cli(base_url, *arguments, body=body, extra_env={"PYTHONIOENCODING": "ascii"})
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.isascii()
    assert json.loads(result.stdout) == response
    assert len(scenario.requests) == 1


@pytest.mark.parametrize("raw", [b'{"value":' + b"1" * 5000 + b"}", b"{", b"\xff"])
def test_invalid_json_input_returns_a_json_error(raw: bytes) -> None:
    result = subprocess.run(
        [sys.executable, str(CLI), "read-tree"],
        input=raw,
        env={
            **os.environ,
            "STARTUNNEL_BASE_URL": "http://127.0.0.1:1",
            "STARTUNNEL_AGENT_KEY": "st_test-agent-key",
            "PYTHONINTMAXSTRDIGITS": "4300",
        },
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_input"


def test_surrogate_input_reaches_server_unicode_validation() -> None:
    body = {"address": "\ud800"}
    with serve(Response(400, {"error": {"code": "invalid_address"}})) as (base_url, scenario):
        result = run_cli(base_url, "read-tree", body=body)
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_address"
    assert scenario.requests[0]["body"] == body


@pytest.mark.parametrize("arguments", [("version",), ("schema",), ("--help",)])
def test_closed_output_pipe_exits_without_a_traceback(arguments: tuple[str, ...]) -> None:
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "wb") as output:
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments],
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    assert result.returncode == 1
    assert result.stderr == b""


@pytest.mark.parametrize(
    ("platform", "error_number", "handled"),
    [("win32", errno.EINVAL, True), ("linux", errno.EINVAL, False), ("win32", errno.EACCES, False)],
)
def test_windows_closed_pipe_error_keeps_other_io_errors_visible(
    monkeypatch: pytest.MonkeyPatch, platform: str, error_number: int, handled: bool
) -> None:
    cli = _load_cli_module()
    failure = OSError(error_number, "synthetic I/O failure")

    def fail_output(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    redirected: list[int] = []
    monkeypatch.setattr(cli, "print", fail_output, raising=False)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform=platform, stdout=sys.stdout))
    monkeypatch.setattr(cli.os, "dup2", lambda source, target: redirected.append(target))
    if handled:
        assert cli.main(["version"]) == 1
        assert redirected == [sys.stdout.fileno()]
    else:
        with pytest.raises(OSError) as raised:
            cli.main(["version"])
        assert raised.value is failure
        assert redirected == []


def test_missing_command_argument_has_json_usage_error() -> None:
    result = run_cli("http://127.0.0.1:8000", "reply")

    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "usage_error"
    assert "--idempotency-key" in error["usage"]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["admin"],
        ["admin", "accounts"],
        ["start", "--idempotency-key"],
        ["admin", "agents", "create", "--idempotency-key", "test-key"],
        ["schema", "st_private-argument"],
        ["--unknown=st_private-argument"],
        ["me", "st_private-argument"],
        ["st_private-argument"],
        ["admin", "st_private-argument"],
        ["admin", "accounts", "st_private-argument"],
        ["admin", "keys", "list", "--limit", "st_private-argument"],
        ["install", "--bin-dir"],
    ],
)
def test_argument_errors_are_json_and_do_not_repeat_input_values(arguments: list[str]) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    result = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert {"code", "message", "usage"} <= set(error)
    assert set(error) <= {"code", "message", "usage", "field_errors"}
    assert error["code"] == "usage_error"
    assert error["message"]
    assert error["usage"].startswith("usage: startunnel")
    assert "st_private-argument" not in result.stderr
    assert str(CLI) not in result.stderr


def test_invalid_argument_names_its_field_without_copying_the_value() -> None:
    result = run_cli("http://127.0.0.1:1", "admin", "keys", "list", "--limit", "private-value")
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["field_errors"][0]["field"] == "--limit"
    assert "private-value" not in result.stderr


@pytest.mark.parametrize(
    ("environment", "field"),
    [
        ({"STARTUNNEL_BASE_URL": ""}, "STARTUNNEL_BASE_URL"),
        ({"STARTUNNEL_AGENT_KEY": ""}, "STARTUNNEL_AGENT_KEY"),
    ],
)
def test_configuration_errors_name_the_setting(environment: dict[str, str], field: str) -> None:
    result = run_cli("http://127.0.0.1:1", "me", extra_env=environment)
    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration_error"
    assert error["field_errors"][0]["field"] == field


@pytest.mark.parametrize(
    "arguments", [["--help"], ["admin", "agents", "create", "--help"], ["schema", "--help"]]
)
def test_explicit_help_remains_offline_text(arguments: list[str]) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    result = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.startswith("usage: startunnel")
    assert "--help" in result.stdout


def test_invalid_idempotency_key_is_a_usage_error_before_any_request() -> None:
    with serve() as (base_url, scenario):
        result = run_cli(
            base_url,
            "reply",
            "--idempotency-key",
            "short",
            body={
                "address": "glyphs",
                "parent_id": "parent",
                "content": {"type": "text", "text": "Hi"},
            },
        )
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "usage_error"
    assert "short" not in result.stderr
    assert scenario.requests == []


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


@pytest.mark.parametrize("admin", [False, True])
def test_interrupted_api_error_returns_json_and_preserves_retry_request(admin: bool) -> None:
    attempts = 3 if admin else 1
    arguments = (
        ("admin", "tunnels", "close", "--idempotency-key", "review-close-001") if admin else ("me",)
    )
    body = {"tunnel_id": "test-tunnel", "expected_cycle_id": "test-cycle"} if admin else None
    responses = [
        Response(
            503,
            raw=b'{"error":',
            declared_content_length="80",
            headers={"Retry-After": "0"},
        )
        for _ in range(attempts)
    ]
    with serve(*responses) as (base_url, scenario):
        result = run_cli(
            base_url,
            *arguments,
            body=body,
            extra_env={"STARTUNNEL_ADMIN_KEY": "sta_test-admin-key"},
        )
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "http_error"
    assert error["status"] == 503
    assert error["retry_after"] == 0
    assert len(scenario.requests) == attempts
    assert all(request["body"] == body for request in scenario.requests)
    if admin:
        assert all(
            request["headers"]["Idempotency-Key"] == "review-close-001"
            for request in scenario.requests
        )


def test_malformed_api_error_does_not_print_response_data() -> None:
    response = {"error": {"code": "invalid_request", "message": ["private-visible-marker"]}}
    with serve(Response(400, response)) as (base_url, _scenario):
        result = run_cli(base_url, "read-context", body={"address": "glyphs"})

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == {
        "code": "invalid_request",
        "message": "The request is not valid.",
        "status": 400,
    }
    assert "visible-marker" not in result.stderr


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


def test_cli_reads_a_large_tree_page() -> None:
    response = {"nodes": [{"content": {"type": "text", "text": "x" * 65536}} for _ in range(33)]}
    assert len(json.dumps(response).encode()) > 2 * 1024 * 1024
    with serve(Response(200, response)) as (base_url, _scenario):
        transport = _load_cli_module().StarTunnelTransport(base_url, "st_test-agent-key")
        result = transport.request(
            "POST", "/api/v1/tree", body={"address": "glyphs", "limit": 100}, expected={200}
        )

    assert result == response


class TrackedBody(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes: list[int | None] = []
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        content = super().read(size)
        self.bytes_read += len(content)
        return content


@pytest.mark.parametrize("declared", [None, "invalid", "-1", "1"])
@pytest.mark.parametrize("overflow", [False, True])
def test_response_reader_bounds_each_read_and_total_bytes(
    declared: str | None, overflow: bool
) -> None:
    cli = _load_cli_module()
    limit = 64 * 1024 + 5
    stream = TrackedBody(b"x" * (limit + int(overflow)))
    headers = {} if declared is None else {"Content-Length": declared}
    response = SimpleNamespace(headers=headers, status=200, read=stream.read)
    if overflow:
        with pytest.raises(cli.CliError) as failure:
            cli._read_response_bytes(response, limit=limit)
        assert failure.value.code == "invalid_response"
        assert failure.value.status == 200
    else:
        assert cli._read_response_bytes(response, limit=limit) == b"x" * limit
        assert stream.read_sizes[-1] == 1
    assert stream.bytes_read == limit + int(overflow)
    assert all(size is not None and 0 < size <= 64 * 1024 for size in stream.read_sizes)


def test_response_reader_rejects_a_large_declared_length_without_reading() -> None:
    cli = _load_cli_module()
    stream = TrackedBody(b"private-visible-marker")
    response = SimpleNamespace(headers={"Content-Length": "33"}, status=200, read=stream.read)
    with pytest.raises(cli.CliError) as failure:
        cli._read_response_bytes(response, limit=32)
    assert failure.value.code == "invalid_response"
    assert failure.value.status == 200
    assert stream.read_sizes == []


@pytest.mark.parametrize("fault", ["valid", "oversize", "invalid_json", "read_error"])
def test_server_error_closes_its_body_and_keeps_safe_metadata(
    fault: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli_module()
    valid = {
        "error": {"code": "dependency_unavailable", "message": "Try again.", "field_errors": []}
    }
    raw = json.dumps(valid).encode() if fault == "valid" else b"private-visible-marker" * 4
    stream = TrackedBody(raw)
    headers = HTTPMessage()
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    headers["X-Request-ID"] = request_id
    headers["Retry-After"] = "2"
    monkeypatch.setattr(cli, "MAX_ERROR_BYTES", 512 if fault in {"valid", "invalid_json"} else 32)
    if fault == "read_error":

        def fail_read(size: int) -> bytes:
            raise OSError("private-visible-marker")

        monkeypatch.setattr(stream, "read", fail_read)
    error = HTTPError("http://example.test/api/v1/me", 503, "Unavailable", headers, stream)
    result = cli._safe_server_error(error)
    assert stream.closed
    assert result.code == ("dependency_unavailable" if fault == "valid" else "http_error")
    assert result.status == 503
    assert result.request_id == request_id
    assert result.retry_after == 2
    assert stream.bytes_read <= cli.MAX_ERROR_BYTES + 1
    cli.print_error(result)
    assert "private-visible-marker" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "framing", ["length", "missing", "chunked", "small", "invalid", "negative"]
)
@pytest.mark.parametrize("status", [200, 503])
def test_http_response_limits_keep_structured_errors_and_safe_metadata(
    framing: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 64)
    monkeypatch.setattr(cli, "MAX_ERROR_BYTES", 64)
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    response = Response(
        status,
        raw=b'{"private-visible-marker":"' + b"x" * 100 + b'"}',
        headers={"X-Request-ID": request_id, "Retry-After": "2"},
        omit_content_length=framing == "missing",
        chunked=framing == "chunked",
        declared_content_length={"small": "1", "invalid": "invalid", "negative": "-1"}.get(framing),
    )
    with serve(response) as (base_url, _scenario):
        transport = cli.StarTunnelTransport(base_url, "st_test-agent-key")
        with pytest.raises(cli.CliError) as failure:
            transport.request("GET", "/api/v1/me", expected={200})
    result = failure.value
    assert result.code == ("invalid_response" if status == 200 else "http_error")
    assert result.status == status
    assert "private-visible-marker" not in (result.message or "")
    if status == 503:
        assert result.request_id == request_id
        assert result.retry_after == 2


@pytest.mark.parametrize("chunked", [False, True])
def test_http_success_without_content_length_preserves_json(chunked: bool) -> None:
    cli = _load_cli_module()
    value = {"items": ["Review café with 代理 and 🔭."]}
    with serve(Response(200, value, omit_content_length=True, chunked=chunked)) as (
        base_url,
        _scenario,
    ):
        transport = cli.StarTunnelTransport(base_url, "st_test-agent-key")
        assert transport.request("GET", "/api/v1/me", expected={200}) == value


@pytest.mark.parametrize(
    "raw",
    [
        b'{"private-visible-marker":"\xff"}',
        b'{"private-visible-marker":' + b"1" * 5000 + b"}",
        b'{"private-visible-marker":' + b"[" * 2000 + b"0" + b"]" * 1999 + b"}",
    ],
    ids=["invalid_utf8", "large_integer", "deep_nesting"],
)
@pytest.mark.parametrize("status", [200, 400])
def test_malformed_response_json_stays_a_structured_cli_error(raw: bytes, status: int) -> None:
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    response = Response(status, raw=raw, headers={"X-Request-ID": request_id, "Retry-After": "2"})
    with serve(response) as (base_url, _scenario):
        result = run_cli(base_url, "me", extra_env={"PYTHONINTMAXSTRDIGITS": "4300"})
    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == ("invalid_response" if status == 200 else "http_error")
    assert error["status"] == status
    assert "private-visible-marker" not in result.stderr
    assert "Traceback" not in result.stderr
    if status == 400:
        assert error["request_id"] == request_id
        assert error["retry_after"] == 2


@pytest.mark.parametrize("status", [200, 400])
def test_response_decoder_recursion_errors_stay_structured(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli_module()

    def fail_decode(value: str) -> Any:
        raise RecursionError("private-visible-marker")

    monkeypatch.setattr(cli.json, "loads", fail_decode)
    with serve(Response(status, raw=b'{"message":"Example"}')) as (base_url, _scenario):
        transport = cli.StarTunnelTransport(base_url, "st_test-agent-key")
        with pytest.raises(cli.CliError) as failure:
            transport.request("GET", "/api/v1/me", expected={200})
    assert failure.value.code == ("invalid_response" if status == 200 else "http_error")
    assert failure.value.status == status
    assert "private-visible-marker" not in (failure.value.message or "")


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
    assert all(
        request["headers"]["X-Startunnel-Client-Version"] == "unreleased"
        for request in scenario.requests
    )
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
    assert imports <= sys.stdlib_module_names


def _load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location("startunnel_cli_contract", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_client_version_is_offline_and_describes_the_running_file() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    result = subprocess.run(
        [sys.executable, str(CLI), "version"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "version": "unreleased",
        "source_revision": None,
        "command_path": str(CLI.resolve()),
        "sha256": hashlib.sha256(CLI.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    ("version", "expected_header"),
    [
        ("v0.3.0", "v0.3.0"),
        ("unreleased", "unreleased"),
        ("custom 'quoted' release\nname", "unreleased"),
        ("v999999999999999999999.0.0", "unreleased"),
    ],
)
def test_stamped_client_sends_only_safe_version_headers(
    tmp_path: Path, version: str, expected_header: str
) -> None:
    client = tmp_path / "star_tunnel.py"
    client.write_bytes(CLI.read_bytes())
    stamp_client(client, version, 'revision"\\value')
    with serve(Response(200, {"id": "agent-id"})) as (base_url, scenario):
        result = subprocess.run(
            [sys.executable, str(client), "me"],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "STARTUNNEL_BASE_URL": base_url,
                "STARTUNNEL_AGENT_KEY": "st_test-agent-key",
            },
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"id": "agent-id"}
    request = scenario.requests[0]
    assert request["headers"]["X-Startunnel-Client-Version"] == expected_header
    assert request["headers"]["Authorization"] == "Bearer st_test-agent-key"


@pytest.mark.parametrize("command", [None, "me", "start", "read-tree"])
def test_agent_schema_is_offline_and_can_filter_one_command(command: str | None) -> None:
    cli = _load_cli_module()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    arguments = ["schema"] + ([command] if command is not None else [])
    result = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    schema = json.loads(result.stdout)
    assert schema["authentication"] == ["STARTUNNEL_AGENT_KEY", "STARTUNNEL_AGENT_KEY_FILE"]
    assert schema["base_url_environment"] == "STARTUNNEL_BASE_URL"
    assert schema["openapi_path"] == "/api/v1/openapi.json"
    all_commands = cli.agent_schema()["commands"]
    assert {item["command"] for item in all_commands} == {"me", *cli.ROUTES}
    assert schema["commands"] == [
        item for item in all_commands if command is None or item["command"] == command
    ]
    if command is not None:
        assert len(schema["commands"]) == 1


def test_agent_config_is_offline_and_reports_normalized_source() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test/",
            "STARTUNNEL_AGENT_KEY": "st_private-config-key",
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "base_url": "https://example.test",
        "base_url_source": "STARTUNNEL_BASE_URL",
        "intended_role": "agent",
        "credential_source": "STARTUNNEL_AGENT_KEY",
        "credential_file": None,
        "authentication_verified": False,
    }
    assert "st_private-config-key" not in result.stdout


def test_agent_config_accepts_both_roles_but_reports_agent_source(tmp_path: Path) -> None:
    agent_file = tmp_path / "agent.key"
    admin_file = tmp_path / "admin.key"
    agent_file.write_text("st_agent-private\n", encoding="utf-8")
    admin_file.write_text("sta_admin-private\n", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test",
            "STARTUNNEL_AGENT_KEY_FILE": str(agent_file),
            "STARTUNNEL_ADMIN_KEY_FILE": str(admin_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["intended_role"] == "agent"
    assert value["credential_source"] == "STARTUNNEL_AGENT_KEY_FILE"
    assert value["credential_file"] == str(agent_file.resolve())
    assert "st_agent-private" not in result.stdout


def test_admin_config_is_offline_and_uses_its_own_credential_file(tmp_path: Path) -> None:
    key_file = tmp_path / "admin.key"
    key_file.write_text("sta_admin-private\n", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test/",
            "STARTUNNEL_ADMIN_KEY_FILE": str(key_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "admin", "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["intended_role"] == "admin"
    assert value["credential_source"] == "STARTUNNEL_ADMIN_KEY_FILE"
    assert value["credential_file"] == str(key_file.resolve())
    assert value["authentication_verified"] is False
    assert "admin-private" not in result.stdout


def test_agent_key_file_is_used_by_the_api_request(tmp_path: Path) -> None:
    key_file = tmp_path / "agent.key"
    key_file.write_text("st_file-agent-key\n", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "unused",
            "STARTUNNEL_AGENT_KEY_FILE": str(key_file),
        }
    )
    with serve(Response(200, {"agent": {"id": "safe-id"}})) as (base_url, scenario):
        environment["STARTUNNEL_BASE_URL"] = base_url
        result = subprocess.run(
            [sys.executable, str(CLI), "me"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert scenario.requests[0]["headers"]["Authorization"] == "Bearer st_file-agent-key"


def test_inherited_key_source_can_be_overridden_in_one_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STARTUNNEL_AGENT_KEY", "st_parent-key")
    monkeypatch.delenv("STARTUNNEL_AGENT_KEY_FILE", raising=False)
    key_file = tmp_path / "agent.key"
    key_file.write_text("st_child-key", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("STARTUNNEL_AGENT_KEY")
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test/",
            "STARTUNNEL_AGENT_KEY_FILE": str(key_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["credential_source"] == "STARTUNNEL_AGENT_KEY_FILE"
    assert os.environ["STARTUNNEL_AGENT_KEY"] == "st_parent-key"


def test_config_rejects_direct_and_file_sources_for_one_role(tmp_path: Path) -> None:
    key_file = tmp_path / "agent.key"
    key_file.write_text("st_private-key", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test",
            "STARTUNNEL_AGENT_KEY": "st_private-direct-key",
            "STARTUNNEL_AGENT_KEY_FILE": str(key_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "STARTUNNEL_AGENT_KEY" in result.stderr
    assert "STARTUNNEL_AGENT_KEY_FILE" in result.stderr
    assert "private" not in result.stderr


def test_empty_direct_setting_conflicts_with_a_file_source(tmp_path: Path) -> None:
    key_file = tmp_path / "agent.key"
    key_file.write_text("st_private-key", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test",
            "STARTUNNEL_AGENT_KEY": "",
            "STARTUNNEL_AGENT_KEY_FILE": str(key_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "STARTUNNEL_AGENT_KEY" in result.stderr
    assert "STARTUNNEL_AGENT_KEY_FILE" in result.stderr
    assert "private" not in result.stderr


@pytest.mark.parametrize(
    ("contents", "directory", "mode"),
    [
        ("missing-file", False, 0o600),
        ("", False, 0o600),
        ("x" * 4097, False, 0o600),
        (None, True, 0o700),
        (b"\xff", False, 0o600),
    ],
)
def test_config_rejects_invalid_key_files_safely(
    tmp_path: Path, contents: str | bytes | None, directory: bool, mode: int
) -> None:
    key_file = tmp_path / "setting-visible-marker"
    if directory:
        key_file.mkdir(mode=mode)
    else:
        if contents not in {None, "missing-file"}:
            key_file.write_bytes(
                contents.encode("utf-8") if isinstance(contents, str) else contents
            )
        if contents != "missing-file":
            key_file.chmod(mode)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment.update(
        {
            "STARTUNNEL_BASE_URL": "https://example.test",
            "STARTUNNEL_AGENT_KEY_FILE": str(key_file),
        }
    )
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration_error"
    assert error["field_errors"][0]["field"] == "STARTUNNEL_AGENT_KEY_FILE"
    assert str(key_file) not in result.stderr
    assert "setting-visible-marker" not in result.stderr


def test_config_rejects_missing_agent_credential() -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment["STARTUNNEL_BASE_URL"] = "https://example.test"
    result = subprocess.run(
        [sys.executable, str(CLI), "config"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "configuration_error"
    assert {item["field"] for item in error["field_errors"]} == {
        "STARTUNNEL_AGENT_KEY",
        "STARTUNNEL_AGENT_KEY_FILE",
    }


def test_config_rejects_malformed_origin_without_reflection() -> None:
    result = run_cli("http://user:private-origin-value@localhost:8", "config")

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["field_errors"][0]["field"] == ("STARTUNNEL_BASE_URL")
    assert "private-origin-value" not in result.stderr


def test_revoked_agent_key_me_failure_does_not_reflect_key() -> None:
    with serve(
        Response(
            401,
            {"error": {"code": "invalid_credential", "message": "bad key"}},
        )
    ) as (base_url, scenario):
        result = run_cli(
            base_url,
            "me",
            extra_env={"STARTUNNEL_AGENT_KEY": "st_revoked-private-key"},
        )

    assert result.returncode == 1
    assert "st_revoked-private-key" not in result.stderr
    assert scenario.requests[0]["path"] == "/api/v1/me"


@pytest.mark.skipif(os.name != "posix", reason="Executable installation requires Linux or macOS.")
def test_install_produces_a_standalone_command_and_replaces_an_existing_copy(
    executable_dir: Path,
) -> None:
    directory = executable_dir / "bin"
    directory.mkdir()
    command = directory / "startunnel"
    command.write_text("old client", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")
    installed = subprocess.run(
        [sys.executable, str(CLI), "install", "--bin-dir", str(directory)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert installed.stderr == ""
    assert json.loads(installed.stdout) == {
        "command_path": str(command),
        "bin_dir": str(directory),
        "on_path": True,
    }
    assert command.read_bytes() == CLI.read_bytes()
    assert command.stat().st_mode & 0o777 == 0o755
    unrelated = executable_dir / "unrelated"
    unrelated.mkdir()
    help_result = subprocess.run(
        ["startunnel", "--help"],
        cwd=unrelated,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert help_result.stderr == ""
    assert "--help" in help_result.stdout
    assert list(directory.iterdir()) == [command]


def test_install_defaults_to_local_bin_without_instance_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("STARTUNNEL_BASE_URL", raising=False)
    monkeypatch.delenv("STARTUNNEL_AGENT_KEY", raising=False)
    assert cli.main(["install"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "command_path": str(tmp_path / ".local/bin/startunnel"),
        "bin_dir": str(tmp_path / ".local/bin"),
        "on_path": False,
    }


@pytest.mark.parametrize("operation", ["write", "replace"])
def test_install_failure_preserves_existing_command_and_removes_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    cli = _load_cli_module()
    command = tmp_path / "startunnel"
    command.write_bytes(b"existing executable")

    def fail(*args: object) -> None:
        del args
        raise OSError("private filesystem detail")

    monkeypatch.setattr(cli.os, "fchmod" if operation == "write" else "replace", fail)
    assert cli.main(["install", "--bin-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "installation_failed"
    assert "private filesystem detail" not in output.err
    assert command.read_bytes() == b"existing executable"
    assert list(tmp_path.iterdir()) == [command]


def test_installed_command_updates_from_the_selected_instance_without_credentials(
    executable_dir: Path,
) -> None:
    command = executable_dir / "startunnel"
    command.write_bytes(CLI.read_bytes())
    command.chmod(0o755)
    updated = CLI.read_bytes().replace(
        b'USER_AGENT = "startunnel-agent-cli"', b'USER_AGENT = "startunnel-agent-cli-updated"'
    )
    release_client = executable_dir / "release.py"
    release_client.write_bytes(updated)
    stamp_client(release_client, "v0.1.0", "a" * 40)
    updated = release_client.read_bytes()
    release_client.unlink()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    with serve(Response(200, raw=updated), Response(200, {"agent": {"id": "agent-id"}})) as (
        base_url,
        scenario,
    ):
        environment["STARTUNNEL_BASE_URL"] = base_url
        result = subprocess.run(
            [str(command), "update"], capture_output=True, text=True, env=environment, check=False
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "command_path": str(command),
            "source_instance": base_url,
        }
        environment["STARTUNNEL_AGENT_KEY"] = "st_test-agent-key"
        identity = subprocess.run(
            [str(command), "me"], capture_output=True, text=True, env=environment, check=False
        )
    assert identity.returncode == 0, identity.stderr
    assert json.loads(identity.stdout) == {"agent": {"id": "agent-id"}}
    assert command.read_bytes() == updated
    assert command.stat().st_mode & 0o777 == 0o755
    assert list(executable_dir.iterdir()) == [command]
    assert scenario.requests[0]["method"] == "GET"
    assert scenario.requests[0]["path"] == "/downloads/star_tunnel.py"
    assert "Authorization" not in scenario.requests[0]["headers"]
    assert "X-StarTunnel-Client-Version" not in scenario.requests[0]["headers"]
    assert scenario.requests[1]["headers"]["User-Agent"] == "startunnel-agent-cli-updated"
    assert scenario.requests[1]["headers"]["X-Startunnel-Client-Version"] == "v0.1.0"
    version = subprocess.run(
        [str(command), "version"], capture_output=True, text=True, env=environment, check=False
    )
    assert version.returncode == 0, version.stderr
    assert json.loads(version.stdout) == {
        "version": "v0.1.0",
        "source_revision": "a" * 40,
        "command_path": str(command.resolve()),
        "sha256": hashlib.sha256(updated).hexdigest(),
    }


@pytest.mark.parametrize(
    "response",
    [
        Response(404, raw=b"private server error"),
        Response(302, headers={"Location": "http://127.0.0.1:1/other"}),
        Response(200, raw=b"<html>Not a client</html>"),
        Response(200, raw=b"#!/usr/bin/env python3\ndef main(:\n"),
        Response(200, raw=CLI.read_bytes().split(b'if __name__ == "__main__":')[0]),
        Response(
            200,
            raw=CLI.read_bytes().rsplit(b"raise SystemExit", 1)[0] + b"raise SystemExit",
            declared_content_length=str(len(CLI.read_bytes())),
        ),
        Response(200, raw=CLI.read_bytes(), declared_content_length="invalid"),
        Response(200, raw=b"x" * (2 * 1024 * 1024 + 1)),
    ],
    ids=[
        "http-error",
        "redirect",
        "html",
        "syntax",
        "truncated-structure",
        "premature-eof",
        "invalid-content-length",
        "too-large",
    ],
)
def test_failed_update_preserves_the_installed_command(
    executable_dir: Path, response: Response
) -> None:
    command = executable_dir / "startunnel"
    original = CLI.read_bytes()
    if response.declared_content_length == str(len(original)):
        assert response.raw is not None
        module = ast.parse(response.raw)
        compile(module, "star_tunnel.py", "exec")
        assert any(
            isinstance(statement, ast.FunctionDef) and statement.name == "main"
            for statement in module.body
        )
        assert isinstance(module.body[-1], ast.If)
        assert len(response.raw) < len(original)
    command.write_bytes(original)
    command.chmod(0o700)
    with serve(response) as (base_url, scenario):
        environment = {
            **os.environ,
            "STARTUNNEL_BASE_URL": base_url,
            "STARTUNNEL_AGENT_KEY": "st_private-agent-key",
            "STARTUNNEL_ADMIN_KEY": "st_private-admin-key",
        }
        result = subprocess.run(
            [str(command), "update"], capture_output=True, text=True, env=environment, check=False
        )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "update_failed"
    assert "private" not in result.stderr
    assert len(scenario.requests) == 1
    assert "Authorization" not in scenario.requests[0]["headers"]
    assert command.read_bytes() == original
    assert command.stat().st_mode & 0o777 == 0o700
    assert list(executable_dir.iterdir()) == [command]


def test_update_succeeds_without_content_length(executable_dir: Path) -> None:
    command = executable_dir / "startunnel"
    original = CLI.read_bytes()
    command.write_bytes(original)
    command.chmod(0o700)
    with serve(Response(200, raw=original, omit_content_length=True)) as (
        base_url,
        _scenario,
    ):
        environment = {
            **os.environ,
            "STARTUNNEL_BASE_URL": base_url,
            "STARTTUNNEL_AGENT_KEY": "st_test-agent-key",
        }
        result = subprocess.run(
            [str(command), "update"], capture_output=True, text=True, env=environment, check=False
        )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "command_path": str(command),
        "source_instance": base_url,
    }
    assert command.read_bytes() == original
    assert command.stat().st_mode & 0o777 == 0o755
    assert list(executable_dir.iterdir()) == [command]


def test_update_write_failure_preserves_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _load_cli_module()
    command = tmp_path / "startunnel"
    original = CLI.read_bytes()
    command.write_bytes(original)
    command.chmod(0o700)
    monkeypatch.setattr(cli, "__file__", str(command))

    def fail(*args: object) -> None:
        del args
        raise OSError("private filesystem detail")

    monkeypatch.setattr(cli.os, "replace", fail)
    with serve(Response(200, raw=original)) as (base_url, _scenario):
        monkeypatch.setenv("STARTUNNEL_BASE_URL", base_url)
        assert cli.main(["update"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "update_failed"
    assert "private" not in output.err
    assert command.read_bytes() == original
    assert command.stat().st_mode & 0o777 == 0o700
    assert list(tmp_path.iterdir()) == [command]


def _resolve_schema(schema: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if reference is None:
        return schema
    assert reference.startswith("#/components/schemas/")
    name = reference.rsplit("/", 1)[1]
    resolved = document["components"]["schemas"][name]
    return _resolve_schema(resolved, document)


def test_cli_help_fields_and_examples_match_generated_openapi() -> None:
    document = json.loads((PROJECT_ROOT / "generated" / "openapi.json").read_text())
    cli = _load_cli_module()
    commands = cli.AGENT_COMMANDS
    assert set(commands) == {"me", "tutorial", *cli.ROUTES}
    assert commands["me"]["example"] is None
    assert commands["tutorial"]["example"] is None
    agent_operations = {
        (method.upper(), path)
        for path, operations in document["paths"].items()
        if path.startswith("/api/v1/") and not path.startswith("/api/v1/admin/")
        for method in operations
        if method in {"get", "post"}
    }
    assert {(method, "/api/v1" + suffix) for method, suffix, _expected in cli.ROUTES.values()} | {
        ("GET", "/api/v1/me")
    } == agent_operations
    schema_commands = {item["command"]: item for item in cli.agent_schema()["commands"]}
    assert {(item["method"], item["path"]) for item in schema_commands.values()} == agent_operations
    for action, item in schema_commands.items():
        operation = document["paths"][item["path"]][item["method"].lower()]
        assert item["stdin_json"] == ("requestBody" in operation)
        assert item["idempotency_key_required"] == (action in cli.WRITE_COMMANDS)
        assert item["success_statuses"] == sorted(
            int(status) for status in operation["responses"] if status.startswith("2")
        )
        assert item["required_fields"] == list(commands[action]["required"])
        assert item["optional_fields"] == list(commands[action]["optional"])
        assert item["example"] == commands[action]["example"]

    for action, (method, suffix, _expected) in cli.ROUTES.items():
        operation = document["paths"]["/api/v1" + suffix][method.lower()]
        requires_key = any(
            parameter["in"] == "header"
            and parameter["name"] == "Idempotency-Key"
            and parameter.get("required")
            for parameter in operation.get("parameters", [])
        )
        assert (action in cli.WRITE_COMMANDS) == requires_key
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        resolved = _resolve_schema(request_schema, document)
        properties = resolved["properties"]
        required = set(resolved.get("required", ()))
        assert set(commands[action]["required"]) == required
        assert set(commands[action]["optional"]) == set(properties) - required
        validation_schema = {**request_schema, "components": document["components"]}
        Draft202012Validator(validation_schema, format_checker=FormatChecker()).validate(
            commands[action]["example"]
        )

    for command in cli.ADMIN_COMMANDS.values():
        if command.method != "POST":
            continue
        operation = document["paths"][f"/api/v1/admin/{command.suffix}"]["post"]
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        resolved = _resolve_schema(request_schema, document)
        properties = resolved["properties"]
        required = set(resolved.get("required", ()))
        assert set(command.required_fields) == required
        assert set(command.optional_fields) == set(properties) - required


@pytest.mark.parametrize(
    ("command", "suffix", "body"),
    [
        (
            "start",
            "start",
            {
                "tunnel_id": "tunnel-id",
                "expected_address_generation": 1,
                "root_content": {"type": "text", "text": "Root message."},
                "root_session": "root-session-start",
            },
        ),
        (
            "rollover",
            "rollover",
            {
                "tunnel_id": "tunnel-id",
                "expected_cycle_id": "cycle-id",
                "expected_address_generation": 1,
                "root_content": {"type": "text", "text": "Root message."},
                "root_session": "root-session-rollover",
            },
        ),
    ],
)
def test_admin_cycle_commands_forward_root_session(
    command: str,
    suffix: str,
    body: dict[str, Any],
) -> None:
    response = {"resource": {"cycle_id": "cycle-id"}, "operation": {"id": "receipt-id"}}
    with serve(Response(200, response)) as (base_url, scenario):
        result = run_cli(
            base_url,
            "admin",
            "tunnels",
            command,
            "--idempotency-key",
            f"start-session-{command}",
            body=body,
            extra_env={"STARTUNNEL_ADMIN_KEY": "sta_test-admin-key"},
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == response
    request = scenario.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == f"/api/v1/admin/tunnels/{suffix}"
    assert request["body"] == body
    assert request["headers"]["Authorization"] == "Bearer sta_test-admin-key"
    assert request["headers"]["X-Startunnel-Client-Version"] == "unreleased"
    assert request["headers"]["Idempotency-Key"] == f"start-session-{command}"


@pytest.mark.parametrize("action", tuple(_load_cli_module().AGENT_COMMANDS))
def test_agent_command_help_is_offline_and_shows_request_contract(action: str) -> None:
    result = subprocess.run(
        [sys.executable, str(CLI), action, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    spec = _load_cli_module().AGENT_COMMANDS[action]
    for field_name in (*spec["required"], *spec["optional"]):
        assert field_name in result.stdout
    if action in _load_cli_module().WRITE_COMMANDS:
        assert "--idempotency-key" in result.stdout


@pytest.mark.parametrize("command", ["start", "rollover"])
def test_admin_cycle_help_lists_root_session(command: str) -> None:
    result = subprocess.run(
        [sys.executable, str(CLI), "admin", "tunnels", command, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "root_session" in result.stdout


def _admin_contract_path(path: str) -> str:
    return "/".join("{id}" if part.startswith("{") else part for part in path.split("/"))


def test_admin_commands_cover_all_openapi_operations() -> None:
    cli = _load_cli_module()
    document = json.loads((PROJECT_ROOT / "generated" / "openapi.json").read_text())
    operations = {
        (method.upper(), _admin_contract_path(path))
        for path, routes in document["paths"].items()
        if path.startswith("/api/v1/admin/")
        for method in routes
        if method in {"get", "post"}
    }
    assert {
        (command.method, "/api/v1/admin/" + command.suffix)
        for command in cli.ADMIN_COMMANDS.values()
    } == operations


@pytest.mark.parametrize(
    "name",
    [
        name
        for name, command in _load_cli_module().ADMIN_COMMANDS.items()
        if command.method == "GET"
    ],
)
def test_admin_get_commands_forward_all_openapi_query_fields(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli_module()
    document = json.loads((PROJECT_ROOT / "generated" / "openapi.json").read_text())
    command = cli.ADMIN_COMMANDS[name]
    path = "/api/v1/admin/" + command.suffix
    operation = next(
        routes["get"]
        for candidate, routes in document["paths"].items()
        if _admin_contract_path(candidate) == path
    )
    resource_id = "00000000-0000-4000-8000-000000000001"
    query_values = {
        "limit": "7",
        "cursor": "page+cursor/with=padding",
        "state": "active",
        "action": "keys.revoke",
        "target_type": "tunnel",
    }
    expected_query = {
        parameter["name"]: query_values.get(parameter["name"], resource_id)
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "query"
    }
    arguments = ["admin", *name.split()]
    if "{id}" in path:
        arguments.append(resource_id)
    for query_field, value in expected_query.items():
        arguments.extend(("--" + query_field.replace("_", "-"), value))
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY", "sta_test-admin-key")
    monkeypatch.delenv("STARTUNNEL_ADMIN_KEY_FILE", raising=False)
    calls = []

    def request(self: Any, method: str, request_path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((self.agent_key, method, request_path, kwargs))
        return {"healthy": True}

    monkeypatch.setattr(cli.StarTunnelTransport, "request", request)
    assert cli.run_admin(cli.build_parser().parse_args(arguments)) == {"healthy": True}
    assert len(calls) == 1
    key, method, request_path, kwargs = calls[0]
    assert key == "sta_test-admin-key"
    assert method == "GET"
    parsed = urlsplit(request_path)
    assert parsed.path == path.replace("{id}", resource_id)
    assert parse_qs(parsed.query) == {
        query_field: [value] for query_field, value in expected_query.items()
    }
    assert kwargs["body"] is None
    assert kwargs["idempotency_key"] is None
    assert kwargs["retry"] is True


@pytest.mark.parametrize("failed_path", ["/api/v1/tree/message", "/api/v1/cycles/close"])
def test_tutorial_failure_closes_its_cycle_with_a_stable_retry_key(
    failed_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli_module()
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("STARTUNNEL_SENDER_KEY", "st_sender-test-key")
    monkeypatch.setenv("STARTUNNEL_RECEIVER_KEY", "st_receiver-test-key")
    failure = cli.CliError("connection_failed")
    calls = []
    responses: dict[str, dict[str, Any]] = {
        "/api/v1/tunnels": {
            "tunnel": {"address": "synthetic-address"},
            "cycle": {"id": "cycle-id", "root": {"id": "root-id"}},
        },
        "/api/v1/tree": {"nodes": [{"id": "root-id"}]},
        "/api/v1/messages": {"message": {"id": "reply-id", "parent_id": "root-id"}},
        "/api/v1/tree/message": {"message": {"id": "reply-id"}},
        "/api/v1/context": {
            "cycle_id": "cycle-id",
            "items": [{"message": {"id": "root-id"}}, {"message": {"id": "reply-id"}}],
        },
        "/api/v1/cycles/close": {},
    }
    failed = False

    def request(self: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal failed
        calls.append({"key": self.agent_key, "method": method, "path": path, **kwargs})
        if path == failed_path and not failed:
            failed = True
            raise failure
        if path == "/api/v1/me":
            return {"agent": {"id": self.agent_key}}
        return responses[path]

    monkeypatch.setattr(cli.StarTunnelTransport, "request", request)
    with pytest.raises(cli.CliError) as raised:
        cli.run_tutorial()
    assert raised.value is failure
    closes = [call for call in calls if call["path"] == "/api/v1/cycles/close"]
    assert len(closes) == (2 if failed_path == "/api/v1/cycles/close" else 1)
    assert calls[-1] == closes[-1]
    for close in closes:
        assert close["key"] == "st_sender-test-key"
        assert close["method"] == "POST"
        assert close["body"] == {"address": "synthetic-address", "expected_cycle_id": "cycle-id"}
        assert close["expected"] == {204}
        uuid.UUID(close["idempotency_key"])
    assert len({close["idempotency_key"] for close in closes}) == 1
