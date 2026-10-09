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
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPMessage
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "startunnel-agent-cli"

SAFE_ERROR_MESSAGES = {
    "secret_output_failed": "The private secret file could not be created or written.",
    "health_check_failed": "A required instance health check failed.",
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
    "admin_unavailable",
    "credential_unavailable",
    "last_admin_access",
    "state_conflict",
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
    retry_after: float | None = None


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


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse both server delay forms without shortening the requested wait."""

    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


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
    retry_after = _retry_after_seconds(error.headers.get("Retry-After"))
    return CliError(code=code, status=error.code, request_id=request_id, retry_after=retry_after)


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
        retry: bool = False,
    ) -> dict[str, Any]:
        """Retry transient admin failures with the same request and retry key."""

        for attempt in range(3 if retry else 1):
            try:
                return self._request_once(
                    method,
                    path,
                    body=body,
                    idempotency_key=idempotency_key,
                    expected=expected,
                    timeout=timeout,
                )
            except CliError as error:
                transient = error.code == "connection_failed" or error.status in {
                    429,
                    502,
                    503,
                    504,
                }
                if not retry or not transient or attempt == 2:
                    raise
                delay = error.retry_after if error.retry_after is not None else 2**attempt
                if delay > 10:
                    raise
                time.sleep(delay)
        raise CliError("connection_failed")

    def _request_once(
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
        except (URLError, OSError, TimeoutError, HTTPException) as error:
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


# This table drives both argument parsing and the offline command schema.
ADMIN_COMMANDS = {
    "me": ("GET", "me", False, (), ()),
    "capabilities": ("GET", "capabilities", False, (), ()),
    "status": ("GET", "status", False, (), ()),
    "doctor": ("GET", "status", False, (), ()),
    "accounts list": ("GET", "accounts", False, (), ("state",)),
    "accounts get": ("GET", "accounts/{id}", False, (), ()),
    "accounts create": (
        "POST",
        "accounts/create",
        True,
        ("username",),
        ("password", "key_name"),
    ),
    "accounts set-state": (
        "POST",
        "accounts/state",
        False,
        ("admin_id", "active", "expected_active"),
        (),
    ),
    "accounts set-password": ("POST", "accounts/password", False, ("admin_id",), ("password",)),
    "keys list": ("GET", "keys", False, (), ("admin_id", "state")),
    "keys create": ("POST", "keys/create", True, ("admin_id", "name"), ("expires_at",)),
    "keys revoke": ("POST", "keys/revoke", False, ("key_id",), ()),
    "agents list": ("GET", "agents", False, (), ("state",)),
    "agents create": ("POST", "agents/create", True, ("name",), ("expires_at",)),
    "agents revoke": ("POST", "agents/revoke", False, ("credential_id",), ()),
    "tunnels list": ("GET", "tunnels", False, (), ("state",)),
    "tunnels get": ("GET", "tunnels/{id}", False, (), ()),
    "tunnels cycles": ("GET", "tunnels/{id}/cycles", False, (), ("state",)),
    "tunnels start": (
        "POST",
        "tunnels/start",
        False,
        ("tunnel_id", "expected_address_generation", "root_content"),
        ("cycle_label", "expires_in_seconds"),
    ),
    "tunnels close": ("POST", "tunnels/close", False, ("tunnel_id", "expected_cycle_id"), ()),
    "tunnels rollover": (
        "POST",
        "tunnels/rollover",
        False,
        ("tunnel_id", "expected_cycle_id", "expected_address_generation", "root_content"),
        ("cycle_label", "expires_in_seconds"),
    ),
    "tunnels rotate": (
        "POST",
        "tunnels/rotate",
        True,
        ("tunnel_id", "expected_address_generation"),
        (),
    ),
    "tunnels retire": (
        "POST",
        "tunnels/retire",
        False,
        ("tunnel_id", "confirmation", "expected_address_generation"),
        (),
    ),
    "audit list": (
        "GET",
        "audit",
        False,
        (),
        ("action", "actor_id", "target_type", "target_id", "credential_id"),
    ),
    "operations get": ("GET", "operations/{id}", False, (), ()),
}


def require_admin_key() -> str:
    """Read one admin key from an environment value or a private file."""

    direct = os.environ.get("STARTUNNEL_ADMIN_KEY")
    filename = os.environ.get("STARTUNNEL_ADMIN_KEY_FILE")
    if (direct is None) == (filename is None):
        raise CliError("configuration_error")
    if direct is not None:
        return require_key("STARTUNNEL_ADMIN_KEY")
    descriptor = None
    try:
        descriptor = os.open(filename or "", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise CliError("configuration_error")
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            raise CliError("configuration_error")
        key = value.decode("utf-8").strip()
        if not key or any(ord(character) < 33 or ord(character) > 126 for character in key):
            raise CliError("configuration_error")
        return key
    except (OSError, UnicodeError) as error:
        raise CliError("configuration_error") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def admin_schema() -> dict[str, Any]:
    """Describe admin commands without a network connection or credential."""

    return {
        "authentication": ["STARTUNNEL_ADMIN_KEY", "STARTUNNEL_ADMIN_KEY_FILE"],
        "openapi_path": "/api/v1/openapi.json",
        "commands": [
            {
                "command": "admin " + name,
                "method": spec[0],
                "path": "/api/v1/admin/" + spec[1],
                "stdin_json": spec[0] == "POST",
                "idempotency_key_required": spec[0] == "POST",
                "secret_output_required": spec[2],
                "required_fields": list(spec[3]),
                "optional_fields": list(spec[4]),
            }
            for name, spec in ADMIN_COMMANDS.items()
        ],
    }


def _reserve_secret_file(filename: str) -> int:
    """Create a private regular file without replacing an existing target."""

    parent = os.path.dirname(os.path.abspath(filename))
    if os.path.realpath(parent) != parent:
        raise CliError("secret_output_failed")
    try:
        return os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise CliError("secret_output_failed") from error


def run_admin(arguments: argparse.Namespace) -> dict[str, Any]:
    """Run one admin operation and save returned secrets to a private file."""

    name = arguments.admin_command
    if name == "schema":
        return admin_schema()
    method, suffix, secret_required, _required, optional = ADMIN_COMMANDS[name]
    if "{id}" in suffix:
        try:
            resource_id = str(uuid.UUID(arguments.resource_id))
        except ValueError as error:
            raise CliError("usage_error") from error
        suffix = suffix.replace("{id}", resource_id)
    if method == "GET" and name.endswith((" list", " cycles")):
        query = {
            field: getattr(arguments, "filter_" + field if field in optional else field, None)
            for field in ("limit", "cursor", *optional)
        }
        query = {field: value for field, value in query.items() if value is not None}
        if query:
            suffix += "?" + urlencode(query)
    body = read_input_object() if method == "POST" else None
    retry_key = require_idempotency_key(arguments.idempotency_key) if method == "POST" else None
    transport = StarTunnelTransport(os.environ.get("STARTUNNEL_BASE_URL", ""), require_admin_key())
    descriptor = None
    complete = False
    reserved = False
    try:
        if secret_required:
            descriptor = _reserve_secret_file(arguments.secret_output)
            reserved = True
        result = transport.request(
            method,
            "/api/v1/admin/" + suffix,
            body=body,
            idempotency_key=retry_key,
            expected={200, 201},
            retry=True,
        )
        secret = result.pop("secret", None)
        if secret is not None:
            if descriptor is None or not isinstance(secret, str) or not secret:
                raise CliError("invalid_response")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    descriptor = None
                    output.write(secret + "\n")
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as error:
                raise CliError("secret_output_failed") from error
            result["secret_output"] = arguments.secret_output
            complete = True
        elif secret_required:
            # Password-backed account creation can return no API key.
            if name != "accounts create":
                raise CliError("invalid_response")
        if name == "doctor" and not result.get("healthy", False):
            raise CliError("health_check_failed")
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if reserved and not complete:
            with suppress(OSError):
                os.unlink(arguments.secret_output)


def add_admin_parser(subparsers: Any) -> None:
    """Build the admin parser from the command schema."""

    admin = subparsers.add_parser("admin", help="Administer the instance through the admin API.")
    commands = admin.add_subparsers(required=True)
    groups: dict[str, Any] = {}
    schema = commands.add_parser("schema", help="Show the offline admin command schema.")
    schema.set_defaults(admin_command="schema")
    for name, spec in ADMIN_COMMANDS.items():
        parts = name.split()
        if len(parts) == 2:
            group, action = parts
            if group not in groups:
                groups[group] = commands.add_parser(group).add_subparsers(required=True)
            command = groups[group].add_parser(action)
        else:
            command = commands.add_parser(name)
        if spec[0] == "POST":
            command.description = (
                "Read one JSON object from stdin. Required fields: "
                + ", ".join(spec[3])
                + ". Optional fields: "
                + (", ".join(spec[4]) or "none")
                + ". "
                "Write resource metadata and an operation receipt as JSON to stdout. "
                "Save returned secrets in the required private output file."
            )
            command.epilog = (
                "Inspect before acting. Reuse the same body and idempotency key for a retry "
                "within 24 hours. Inspect current state and the receipt after an uncertain "
                "result. Use the instance OpenAPI document for field types and limits."
            )
        else:
            command.description = (
                "Write the requested resource or page as JSON to stdout. "
                "Use admin schema for command definitions and the instance OpenAPI "
                "document for response types."
            )
        command.set_defaults(admin_command=name)
        if "{id}" in spec[1]:
            command.add_argument("resource_id", help="Stable resource UUID.")
        if spec[0] == "POST":
            command.add_argument(
                "--idempotency-key", required=True, help="Reuse for exact retries."
            )
        if spec[2]:
            command.add_argument("--secret-output", required=True, help="New private secret file.")
        if name.endswith((" list", " cycles")):
            command.add_argument("--limit", type=int)
            command.add_argument("--cursor")
            for field in spec[4]:
                command.add_argument("--" + field.replace("_", "-"), dest="filter_" + field)


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
    add_admin_parser(subparsers)
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
    if error.retry_after is not None:
        details["retry_after"] = error.retry_after
    print(json.dumps({"error": details}, separators=(",", ":")), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.action == "admin":
            result = run_admin(arguments)
        else:
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
