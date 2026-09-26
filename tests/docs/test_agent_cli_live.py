"""Prove the downloadable agent CLI against the disposable example service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx
import pytest

ENABLED = os.getenv("STARTUNNEL_STANDALONE_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="Set STARTUNNEL_STANDALONE_E2E=1 in the isolated examples lane.",
)


class _QuietProxy(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def _loopback_proxy(target_base_url: str) -> Iterator[str]:
    """Forward loopback HTTP without logging credentials or private request data."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            content = self.rfile.read(length) if length else None
            headers = {
                name: value
                for name in ("Accept", "Authorization", "Content-Type", "Idempotency-Key")
                if (value := self.headers.get(name)) is not None
            }
            try:
                response = httpx.request(
                    self.command,
                    urljoin(target_base_url.rstrip("/") + "/", self.path.lstrip("/")),
                    content=content,
                    headers=headers,
                    timeout=30,
                )
            except httpx.HTTPError:
                self.send_error(502)
                return
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in {
                    "connection",
                    "content-encoding",
                    "content-length",
                    "transfer-encoding",
                }:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = _QuietProxy(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _command(
    cli_path: Path,
    *,
    base_url: str,
    key: str | None,
    arguments: list[str],
    body: dict[str, Any] | None = None,
    tutorial_keys: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["STARTUNNEL_BASE_URL"] = base_url
    if key is None:
        environment.pop("STARTUNNEL_AGENT_KEY", None)
    else:
        environment["STARTUNNEL_AGENT_KEY"] = key
    if tutorial_keys is not None:
        environment["STARTUNNEL_SENDER_KEY"] = tutorial_keys[0]
        environment["STARTUNNEL_RECEIVER_KEY"] = tutorial_keys[1]
    return subprocess.run(
        [sys.executable, "-I", str(cli_path), *arguments],
        cwd=cli_path.parent,
        env=environment,
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )


def _successful_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert completed.returncode == 0, (
        f"The downloaded CLI exited with {completed.returncode}: {completed.stderr.strip()}"
    )
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def _run_action(
    cli_path: Path,
    *,
    base_url: str,
    key: str,
    action: str,
    body: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    arguments = [action]
    if idempotency_key is not None:
        arguments.extend(("--idempotency-key", idempotency_key))
    return _successful_json(
        _command(
            cli_path,
            base_url=base_url,
            key=key,
            arguments=arguments,
            body=body,
        )
    )


def _prove_six_actions(
    cli_path: Path,
    *,
    base_url: str,
    sender_key: str,
    receiver_key: str,
) -> tuple[str, str, str]:
    create_key = f"standalone-create-{uuid4().hex}"
    created = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="create",
        idempotency_key=create_key,
        body={
            "label": "Downloaded CLI proof",
            "cycle": {
                "label": "Standalone command proof",
                "root": {
                    "content": {
                        "type": "json",
                        "value": {"task": "review"},
                    }
                },
            },
        },
    )
    address = str(created["tunnel"]["address"])
    cycle_id = str(created["cycle"]["id"])
    root_id = str(created["cycle"]["root"]["id"])
    initial_cursor = str(created["activity_cursor"])

    reply_key = f"standalone-reply-{uuid4().hex}"
    reply_body = {
        "address": address,
        "parent_id": root_id,
        "content": {"type": "text", "text": "The review is complete."},
    }
    replied = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="reply",
        idempotency_key=reply_key,
        body=reply_body,
    )
    repeated = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="reply",
        idempotency_key=reply_key,
        body=reply_body,
    )
    reply_id = str(replied["message"]["id"])
    assert repeated["message"]["id"] == reply_id

    activity = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="wait",
        body={"address": address, "after_cursor": initial_cursor},
    )
    assert [event["type"] for event in activity["events"]] == ["message_posted"]
    activity_cursor = str(activity["next_cursor"])

    context_body = {
        "address": address,
        "cycle_id": cycle_id,
        "focus_message_id": reply_id,
        "include_direct_replies": False,
        "include_recent_activity": False,
        "max_items": 3,
        "max_bytes": 32768,
    }
    context = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="read-context",
        body=context_body,
    )
    assert [item["message"]["id"] for item in context["items"]] == [root_id, reply_id]

    checkpoint = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="checkpoint",
        body={"address": address, "cursor": activity_cursor},
    )
    assert checkpoint["advanced"] is True

    closed = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="close",
        idempotency_key=f"standalone-close-{uuid4().hex}",
        body={"address": address, "expected_cycle_id": cycle_id},
    )
    assert closed == {}
    retained = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="read-context",
        body=context_body,
    )
    assert [item["message"]["id"] for item in retained["items"]] == [root_id, reply_id]
    return address, cycle_id, reply_id


def test_downloaded_agent_cli_runs_without_repository_imports(tmp_path: Path) -> None:
    required = {
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
    }
    missing = sorted(name for name in required if not os.getenv(name))
    assert not missing, "Missing standalone CLI setting names: " + ", ".join(missing)

    target_base_url = os.environ["STARTUNNEL_BASE_URL"]
    with _loopback_proxy(target_base_url) as loopback_url:
        download = httpx.get(f"{loopback_url}/downloads/star_tunnel.py", timeout=30)
        assert download.status_code == 200
        assert "attachment" in download.headers["Content-Disposition"]
        assert download.headers["X-Content-Type-Options"] == "nosniff"
        cli_path = tmp_path / "star_tunnel.py"
        cli_path.write_bytes(download.content)

        sender_key = os.environ["STARTUNNEL_SENDER_KEY"]
        receiver_key = os.environ["STARTUNNEL_RECEIVER_KEY"]
        tutorial = _successful_json(
            _command(
                cli_path,
                base_url=loopback_url,
                key=None,
                arguments=["tutorial"],
                tutorial_keys=(sender_key, receiver_key),
            )
        )
        assert tutorial["status"] == "complete"

        sender = _successful_json(
            _command(
                cli_path,
                base_url=loopback_url,
                key=sender_key,
                arguments=["me"],
            )
        )
        assert sender["agent"]["id"]

        _prove_six_actions(
            cli_path,
            base_url=loopback_url,
            sender_key=sender_key,
            receiver_key=receiver_key,
        )
