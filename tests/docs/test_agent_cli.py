from __future__ import annotations

import ast
import hashlib
import importlib.util
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
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
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


def test_api_error_keeps_authored_message_and_discards_unknown_fields() -> None:
    request_id = "064c1aab-8f0f-42d3-b2db-f132ba86363c"
    response = {
        "error": {
            "code": "tunnel_unavailable",
            "message": "This tunnel is not available. Check the address or create a new tunnel.",
            "request_id": request_id,
            "field_errors": [],
            "unexpected": "unknown-field-visible-marker",
        }
    }
    with serve(Response(404, response)) as (base_url, _scenario):
        result = run_cli(base_url, "checkpoint", body={"address": "private-glyphs", "cursor": "c"})

    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error == {
        "code": "tunnel_unavailable",
        "message": "This tunnel is not available. Check the address or create a new tunnel.",
        "status": 404,
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


def test_missing_command_argument_has_standard_parser_diagnostic() -> None:
    result = run_cli("http://127.0.0.1:8000", "reply")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--idempotency-key" in result.stderr


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
        "ast",
        "collections",
        "contextlib",
        "dataclasses",
        "email",
        "http",
        "hashlib",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "sys",
        "stat",
        "tempfile",
        "time",
        "typing",
        "urllib",
        "uuid",
    }


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
        "sha256": hashlib.sha256(CLI.read_bytes()).hexdigest(),
    }


@pytest.mark.skipif(os.name != "posix", reason="Executable installation requires Linux or macOS.")
def test_install_produces_a_standalone_command_and_replaces_an_existing_copy(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "bin"
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
    unrelated = tmp_path / "unrelated"
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
    tmp_path: Path,
) -> None:
    command = tmp_path / "startunnel"
    command.write_bytes(CLI.read_bytes())
    command.chmod(0o755)
    updated = CLI.read_bytes().replace(
        b'USER_AGENT = "startunnel-agent-cli"', b'USER_AGENT = "startunnel-agent-cli-updated"'
    )
    release_client = tmp_path / "release.py"
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
    assert list(tmp_path.iterdir()) == [command]
    assert scenario.requests[0]["method"] == "GET"
    assert scenario.requests[0]["path"] == "/downloads/star_tunnel.py"
    assert "Authorization" not in scenario.requests[0]["headers"]
    assert scenario.requests[1]["headers"]["User-Agent"] == "startunnel-agent-cli-updated"
    version = subprocess.run(
        [str(command), "version"], capture_output=True, text=True, env=environment, check=False
    )
    assert version.returncode == 0, version.stderr
    assert json.loads(version.stdout) == {
        "version": "v0.1.0",
        "source_revision": "a" * 40,
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
        Response(200, raw=b"x" * (2 * 1024 * 1024 + 1)),
    ],
    ids=["http-error", "redirect", "html", "syntax", "truncated", "too-large"],
)
def test_failed_update_preserves_the_installed_command(tmp_path: Path, response: Response) -> None:
    command = tmp_path / "startunnel"
    original = CLI.read_bytes()
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
    assert list(tmp_path.iterdir()) == [command]


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

    for action, (method, suffix, _expected) in cli.ROUTES.items():
        operation = document["paths"]["/api/v1" + suffix][method.lower()]
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
    assert request["headers"]["Idempotency-Key"] == f"start-session-{command}"


@pytest.mark.parametrize(
    "action",
    [
        "me",
        "create",
        "reply",
        "wait",
        "enter",
        "participants",
        "read-context",
        "checkpoint",
        "close",
        "beacon-publish",
        "beacon-withdraw",
        "beacon-board",
        "beacon-wait",
        "tutorial",
    ],
)
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
    if action in {"create", "reply", "close", "beacon-publish", "beacon-withdraw"}:
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
