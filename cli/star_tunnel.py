#!/usr/bin/env python3
"""A small, dependency-free command line client for StarTunnel agents.

This file is designed to be downloaded and run with Python 3.10 or later.
It accepts public API request objects through standard input and writes one
JSON result to standard output.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "startunnel-agent-cli"

SAFE_ERROR_MESSAGES = {
    "configuration_error": "Check the required environment settings.",
    "connection_failed": "The service could not be reached. Check the base URL and service health.",
    "input_too_large": "The JSON input is too large.",
    "invalid_address": "The address is not valid.",
    "invalid_credential": "The agent credential is not valid.",
    "invalid_cursor": "The cursor is not valid or has expired.",
    "invalid_input": "Provide one JSON object through standard input.",
    "invalid_request": "The request is not valid.",
    "invalid_response": "The service returned an unexpected response.",
    "response_too_large": "The service response is too large.",
    "usage_error": "The command arguments are not valid. Run --help for usage.",
}
PUBLIC_ERROR_CODES = {
    "checkpoint_regression",
    "context_budget_too_small",
    "cycle_closed",
    "cycle_unavailable",
    "dependency_unavailable",
    "idempotency_conflict",
    "internal_error",
    "invalid_address",
    "invalid_credential",
    "invalid_cursor",
    "invalid_request",
    "lifecycle_conflict",
    "payload_too_large",
    "quota_exceeded",
    "rate_limited",
    "tunnel_unavailable",
}


@dataclass
class CliError(Exception):
    """A safe error that can be printed without exposing request data."""

    code: str
    status: int | None = None
    request_id: str | None = None


class SafeArgumentParser(argparse.ArgumentParser):
    """Convert argument errors to the CLI's JSON error format."""

    def error(self, message: str) -> NoReturn:
        del message
        raise CliError("usage_error")


class NoRedirectHandler(HTTPRedirectHandler):
    """Keep credentials on the configured origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_base_url(value: str) -> str:
    """Return a safe origin URL for authenticated API requests."""

    if (
        not value
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise CliError("configuration_error")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise CliError("configuration_error") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CliError("configuration_error")
    if parsed.scheme == "http" and not _is_loopback(hostname):
        raise CliError("configuration_error")
    if port is not None and not 1 <= port <= 65535:
        raise CliError("configuration_error")
    return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, "", "", ""))


def require_key(name: str) -> str:
    """Read a credential without including it in an error."""

    value = os.environ.get(name, "")
    if not value or any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise CliError("configuration_error")
    return value


def require_idempotency_key(value: str) -> str:
    """Apply the public header constraints before making a request."""

    if not 8 <= len(value) <= 128 or any(
        ord(character) < 32 or ord(character) > 126 for character in value
    ):
        raise CliError("usage_error")
    return value


def read_input_object() -> dict[str, Any]:
    """Read one bounded JSON object from standard input."""

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise CliError("input_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CliError("invalid_input") from error
    if not isinstance(value, dict):
        raise CliError("invalid_input")
    return value


def _safe_server_error(error: HTTPError) -> CliError:
    code = "http_error"
    request_id = error.headers.get("X-Request-ID")
    try:
        raw = error.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) <= MAX_RESPONSE_BYTES:
            value = json.loads(raw.decode("utf-8"))
            envelope = value.get("error") if isinstance(value, dict) else None
            if isinstance(envelope, dict):
                candidate_code = envelope.get("code")
                candidate_request_id = envelope.get("request_id")
                if isinstance(candidate_code, str) and candidate_code in PUBLIC_ERROR_CODES:
                    code = candidate_code
                if isinstance(candidate_request_id, str):
                    try:
                        request_id = str(uuid.UUID(candidate_request_id))
                    except ValueError:
                        request_id = None
    except OSError:
        pass
    except UnicodeDecodeError:
        pass
    except json.JSONDecodeError:
        pass
    if isinstance(request_id, str):
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            request_id = None
    else:
        request_id = None
    return CliError(code=code, status=error.code, request_id=request_id)


class StarTunnelTransport:
    """A bounded JSON transport that does not follow redirects."""

    def __init__(self, base_url: str, agent_key: str) -> None:
        self.base_url = validate_base_url(base_url)
        self.agent_key = agent_key
        self.opener = build_opener(NoRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected: set[int],
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.agent_key,
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_INPUT_BYTES:
                raise CliError("input_too_large")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = require_idempotency_key(idempotency_key)
        try:
            request = Request(self.base_url + path, data=encoded, headers=headers, method=method)
            with self.opener.open(request, timeout=timeout) as response:
                if response.status not in expected:
                    raise CliError("http_error", status=response.status)
                if response.status == 204:
                    return {}
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) > MAX_RESPONSE_BYTES:
                            raise CliError("response_too_large", status=response.status)
                    except ValueError:
                        pass
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise _safe_server_error(error) from error
        except (URLError, OSError, TimeoutError) as error:
            raise CliError("connection_failed") from error
        except (UnicodeError, ValueError) as error:
            raise CliError("configuration_error") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CliError("response_too_large", status=response.status)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CliError("invalid_response", status=response.status) from error
        if not isinstance(value, dict):
            raise CliError("invalid_response", status=response.status)
        return value


ROUTES = {
    "create": ("POST", "/tunnels", {201}),
    "reply": ("POST", "/messages", {201}),
    "wait": ("POST", "/activity", {200}),
    "read-context": ("POST", "/context", {200}),
    "checkpoint": ("POST", "/activity/checkpoint", {200}),
    "close": ("POST", "/cycles/close", {204}),
}


def route_path(action: str) -> str:
    """Map one high-level action to its public API route."""

    return "/api/v1" + ROUTES[action][1]


def run_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Run one direct API action."""

    base_url = validate_base_url(os.environ.get("STARTUNNEL_BASE_URL", ""))
    transport = StarTunnelTransport(base_url, require_key("STARTUNNEL_AGENT_KEY"))
    if arguments.action == "me":
        return transport.request("GET", "/api/v1/me", expected={200})

    body = read_input_object()
    if arguments.action == "wait":
        body.setdefault("wait_seconds", 20)
    method, _suffix, expected = ROUTES[arguments.action]
    idempotency_key = getattr(arguments, "idempotency_key", None)
    timeout = REQUEST_TIMEOUT_SECONDS
    if arguments.action == "wait":
        wait_seconds = body.get("wait_seconds")
        if isinstance(wait_seconds, int) and not isinstance(wait_seconds, bool):
            timeout = max(REQUEST_TIMEOUT_SECONDS, min(wait_seconds, 20) + 10)
    return transport.request(
        method,
        route_path(arguments.action),
        body=body,
        idempotency_key=idempotency_key,
        expected=expected,
        timeout=timeout,
    )


def _object(value: Any, name: str) -> dict[str, Any]:
    del name
    if not isinstance(value, dict):
        raise CliError("invalid_response")
    return value


def _string(value: Any, name: str) -> str:
    del name
    if not isinstance(value, str) or not value:
        raise CliError("invalid_response")
    return value


def _items(value: Any, name: str) -> Sequence[Any]:
    del name
    if not isinstance(value, list):
        raise CliError("invalid_response")
    return value


def run_tutorial() -> dict[str, Any]:
    """Perform one complete two-agent exchange through the public API."""

    base_url = validate_base_url(os.environ.get("STARTUNNEL_BASE_URL", ""))
    sender = StarTunnelTransport(base_url, require_key("STARTUNNEL_SENDER_KEY"))
    receiver = StarTunnelTransport(base_url, require_key("STARTUNNEL_RECEIVER_KEY"))
    checks = []
    address = None
    cycle_id = None
    close_key = str(uuid.uuid4())
    closed = False
    try:
        sender_identity = sender.request("GET", "/api/v1/me", expected={200})
        checks.append("sender_identity")
        receiver_identity = receiver.request("GET", "/api/v1/me", expected={200})
        sender_agent = _object(sender_identity.get("agent"), "sender agent")
        receiver_agent = _object(receiver_identity.get("agent"), "receiver agent")
        sender_id = _string(sender_agent.get("id"), "sender ID")
        receiver_id = _string(receiver_agent.get("id"), "receiver ID")
        if sender_id == receiver_id:
            raise CliError("configuration_error")
        checks.append("receiver_identity")

        created = sender.request(
            "POST",
            "/api/v1/tunnels",
            body={
                "label": "Agent CLI tutorial",
                "cycle": {
                    "label": "First exchange",
                    "expires_in_seconds": 3600,
                    "root": {
                        "content": {
                            "type": "json",
                            "value": {"task": "review", "artifact": "tutorial-note"},
                        },
                        "mentions": [],
                        "correlation_id": "agent-cli-tutorial",
                    },
                },
            },
            idempotency_key=str(uuid.uuid4()),
            expected={201},
        )
        tunnel = _object(created.get("tunnel"), "tunnel")
        cycle = _object(created.get("cycle"), "cycle")
        root = _object(cycle.get("root"), "root")
        address = _string(tunnel.get("address"), "address")
        display_address = _string(tunnel.get("display_address", address), "display address")
        cycle_id = _string(cycle.get("id"), "cycle ID")
        root_id = _string(root.get("id"), "root ID")
        checks.append("root_created")

        tree = receiver.request(
            "POST",
            "/api/v1/tree",
            body={"address": address, "cycle_id": cycle_id},
            expected={200},
        )
        nodes = _items(tree.get("nodes"), "tree nodes")
        if len(nodes) != 1 or _object(nodes[0], "root node").get("id") != root_id:
            raise CliError("invalid_response")
        checks.append("root_read")

        posted = receiver.request(
            "POST",
            "/api/v1/messages",
            body={
                "address": address,
                "parent_id": root_id,
                "content": {
                    "type": "json",
                    "value": {"status": "reviewed", "artifact": "tutorial-note"},
                },
                "mentions": [],
                "correlation_id": "agent-cli-tutorial",
            },
            idempotency_key=str(uuid.uuid4()),
            expected={201},
        )
        message = _object(posted.get("message"), "reply")
        reply_id = _string(message.get("id"), "reply ID")
        if message.get("parent_id") != root_id:
            raise CliError("invalid_response")
        checks.append("reply_created")

        exact = sender.request(
            "POST",
            "/api/v1/tree/message",
            body={"address": address, "cycle_id": cycle_id, "message_id": reply_id},
            expected={200},
        )
        if _object(exact.get("message"), "message").get("id") != reply_id:
            raise CliError("invalid_response")
        checks.append("reply_read")

        context = sender.request(
            "POST",
            "/api/v1/context",
            body={
                "address": address,
                "cycle_id": cycle_id,
                "focus_message_id": reply_id,
                "max_items": 20,
                "max_bytes": 65_536,
            },
            expected={200},
        )
        context_items = _items(context.get("items"), "context")
        context_message_ids = {
            _object(_object(item, "context item").get("message"), "context message").get("id")
            for item in context_items
        }
        if context.get("cycle_id") != cycle_id or not {root_id, reply_id}.issubset(
            context_message_ids
        ):
            raise CliError("invalid_response")
        checks.append("context_read")

        sender.request(
            "POST",
            "/api/v1/cycles/close",
            body={"address": address, "expected_cycle_id": cycle_id},
            idempotency_key=close_key,
            expected={204},
        )
        closed = True
        checks.append("cycle_closed")

        retained = receiver.request(
            "POST",
            "/api/v1/tree",
            body={"address": address, "cycle_id": cycle_id},
            expected={200},
        )
        retained_nodes = _items(retained.get("nodes"), "retained tree")
        retained_by_id = {
            _string(_object(node, "retained node").get("id"), "retained message ID"): _object(
                node, "retained node"
            )
            for node in retained_nodes
        }
        if (
            set(retained_by_id) != {root_id, reply_id}
            or retained_by_id[root_id].get("parent_id") is not None
            or retained_by_id[reply_id].get("parent_id") != root_id
        ):
            raise CliError("invalid_response")
        checks.append("retained_tree_read")
        return {
            "status": "complete",
            "checks": checks,
            "display_address": display_address,
            "cycle_id": cycle_id,
            "root_message_id": root_id,
            "reply_message_id": reply_id,
            "retained_message_count": len(retained_nodes),
        }
    finally:
        if address is not None and cycle_id is not None and not closed:
            with suppress(CliError):
                sender.request(
                    "POST",
                    "/api/v1/cycles/close",
                    body={"address": address, "expected_cycle_id": cycle_id},
                    idempotency_key=close_key,
                    expected={204},
                )


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description="Use the StarTunnel public API with Python 3.10 or later."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("me", help="Show the current agent identity and limits.")

    for action in ("create", "reply", "close"):
        command = subparsers.add_parser(action, help=action.capitalize() + " through the API.")
        command.add_argument("--idempotency-key", required=True, help="Retry key for this write.")

    for action in ("wait", "read-context", "checkpoint"):
        command = subparsers.add_parser(action, help=action.capitalize() + " through the API.")

    subparsers.add_parser("tutorial", help="Run a complete two-agent tutorial exchange.")
    return parser


def print_error(error: CliError) -> None:
    """Write one bounded, sanitized JSON error object."""

    message = SAFE_ERROR_MESSAGES.get(error.code, "The StarTunnel request failed.")
    details: dict[str, Any] = {"code": error.code, "message": message}
    if error.status is not None:
        details["status"] = error.status
    if error.request_id is not None:
        details["request_id"] = error.request_id
    print(json.dumps({"error": details}, separators=(",", ":")), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        result = run_tutorial() if arguments.action == "tutorial" else run_action(arguments)
    except CliError as error:
        print_error(error)
        return 1
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
