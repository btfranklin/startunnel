"""Prove the downloadable agent CLI against the disposable example service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest
from examples.python.startunnel_client import (
    ActivityResponse,
    CheckpointResponse,
    ContextResponse,
    CreateTunnelResponse,
    CycleListResponse,
    JsonObject,
    JsonValue,
    MeResponse,
    MessageReadResponse,
    PostReplyResponse,
    SearchResponse,
    StartedCycleResponse,
    TreeResponse,
    TunnelStatusResponse,
)
from pydantic import TypeAdapter
from scripts.json_data import json_array, json_object, json_string


class ResourceIdentity(TypedDict):
    id: str


class IssuedAgent(TypedDict):
    resource: ResourceIdentity


class RecoveryTunnel(TypedDict):
    id: str
    state: str
    address_generation: int | None
    retirement_confirmation: str


class RecoveryTunnelPage(TypedDict):
    items: list[RecoveryTunnel]
    next_cursor: str | None


class RetiredTunnels(TypedDict):
    credential_id: str
    retired_tunnel_ids: list[str]


class RetirementResult(TypedDict):
    resource: RetiredTunnels
    operation: ResourceIdentity


class ReceiptResult(TypedDict):
    resource: RetiredTunnels


class RetirementReceipt(TypedDict):
    result: ReceiptResult


class RetainedCycle(TypedDict):
    state: str
    label: str


class RetainedCyclePage(TypedDict):
    items: list[RetainedCycle]


ENABLED = os.getenv("STARTUNNEL_STANDALONE_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="Set STARTUNNEL_STANDALONE_E2E=1 in the isolated examples lane.",
)


def _command(
    cli_path: Path,
    *,
    base_url: str,
    key: str | None,
    arguments: list[str],
    body: JsonObject | None = None,
    tutorial_keys: tuple[str, str] | None = None,
    admin_key_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["STARTUNNEL_BASE_URL"] = base_url
    environment.pop("STARTUNNEL_ADMIN_KEY", None)
    environment.pop("STARTUNNEL_ADMIN_KEY_FILE", None)
    if admin_key_file is not None:
        environment["STARTUNNEL_ADMIN_KEY_FILE"] = str(admin_key_file)
    if key is None:
        environment.pop("STARTUNNEL_AGENT_KEY", None)
    else:
        environment["STARTUNNEL_AGENT_KEY"] = key
    if tutorial_keys is not None:
        environment["STARTUNNEL_SENDER_KEY"] = tutorial_keys[0]
        environment["STARTUNNEL_RECEIVER_KEY"] = tutorial_keys[1]
    return subprocess.run(
        (
            [str(cli_path), *arguments]
            if cli_path.name == "startunnel"
            else [sys.executable, "-I", str(cli_path), *arguments]
        ),
        cwd=cli_path.parent,
        env=environment,
        input=json.dumps(body) if body is not None else None,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )


def _successful_json(completed: subprocess.CompletedProcess[str]) -> JsonObject:
    assert completed.returncode == 0, (
        f"The downloaded CLI exited with {completed.returncode}: {completed.stderr.strip()}"
    )
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return json_object(parsed)


def _run_action[T](
    cli_path: Path,
    *,
    base_url: str,
    key: str,
    action: str,
    body: JsonObject,
    response_type: type[T],
    idempotency_key: str | None = None,
) -> T:
    arguments = [action]
    if idempotency_key is not None:
        arguments.extend(("--idempotency-key", idempotency_key))
    return TypeAdapter(response_type).validate_python(
        _successful_json(
            _command(
                cli_path,
                base_url=base_url,
                key=key,
                arguments=arguments,
                body=body,
            )
        ),
        strict=True,
    )


def _prove_tunnel_commands(
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
        response_type=CreateTunnelResponse,
    )
    address = str(created["tunnel"]["address"])
    cycle_id = str(created["cycle"]["id"])
    root_id = str(created["cycle"]["root"]["id"])
    initial_cursor = str(created["activity_cursor"])

    reply_key = f"standalone-reply-{uuid4().hex}"
    reply_body: JsonObject = {
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
        response_type=PostReplyResponse,
    )
    repeated = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="reply",
        idempotency_key=reply_key,
        body=reply_body,
        response_type=PostReplyResponse,
    )
    reply_id = str(replied["message"]["id"])
    assert repeated["message"]["id"] == reply_id

    activity = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="wait",
        body={"address": address, "after_cursor": initial_cursor},
        response_type=ActivityResponse,
    )
    assert [event["type"] for event in activity["events"]] == ["message_posted"]
    activity_cursor = str(activity["next_cursor"])

    context_body: JsonObject = {
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
        response_type=ContextResponse,
    )
    assert [item["message"]["id"] for item in context["items"]] == [root_id, reply_id]

    checkpoint = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="checkpoint",
        body={"address": address, "cursor": activity_cursor},
        response_type=CheckpointResponse,
    )
    assert checkpoint["advanced"] is True

    def read[T](action: str, body: JsonObject, response_type: type[T]) -> T:
        return _run_action(
            cli_path,
            base_url=base_url,
            key=receiver_key,
            action=action,
            body=body,
            response_type=response_type,
        )

    scope = {"address": address, "cycle_id": cycle_id}
    for action, fields, collection, expected_ids in (
        ("read-tree", {}, "nodes", [root_id, reply_id]),
        ("read-branch", {"leaf_id": reply_id}, "messages", [root_id, reply_id]),
        ("read-replies", {"message_id": root_id}, "messages", [reply_id]),
        ("read-subtree", {"message_id": root_id}, "nodes", [root_id, reply_id]),
        ("read-leaves", {}, "leaves", [reply_id]),
    ):
        page = read(action, {**scope, **fields}, response_type=dict[str, JsonValue])
        assert [json_object(item)["id"] for item in json_array(page[collection])] == expected_ids
    exact = read(
        "read-message", {**scope, "message_id": reply_id}, response_type=MessageReadResponse
    )
    assert exact["message"]["id"] == reply_id
    assert exact["message"]["parent_id"] == root_id
    found = read("search", {**scope, "query": "complete"}, response_type=SearchResponse)
    assert [item["message_id"] for item in found["results"]] == [reply_id]
    status = read("status", {"address": address}, response_type=TunnelStatusResponse)
    assert status["state"] == "active"
    assert status["current_cycle"] is not None
    assert status["current_cycle"]["id"] == cycle_id
    directory = read("cycles", {"address": address}, response_type=CycleListResponse)
    assert [item["id"] for item in directory["items"]] == [cycle_id]

    closed = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="close",
        idempotency_key=f"standalone-close-{uuid4().hex}",
        body={"address": address, "expected_cycle_id": cycle_id},
        response_type=dict[str, JsonValue],
    )
    assert closed == {}
    retained = _run_action(
        cli_path,
        base_url=base_url,
        key=receiver_key,
        action="read-context",
        body=context_body,
        response_type=ContextResponse,
    )
    assert [item["message"]["id"] for item in retained["items"]] == [root_id, reply_id]

    next_cycle = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="start",
        idempotency_key=f"standalone-start-{uuid4().hex}",
        body={
            "address": address,
            "expected_address_generation": status["address_generation"],
            "cycle": {
                "root": {
                    "content": {"type": "text", "text": "Start the next task."},
                    "session": "next-work",
                }
            },
        },
        response_type=StartedCycleResponse,
    )["cycle"]
    assert next_cycle["number"] == 2
    next_root = read(
        "read-message",
        {"address": address, "cycle_id": next_cycle["id"], "message_id": next_cycle["root"]["id"]},
        response_type=MessageReadResponse,
    )
    assert next_root["message"]["sender"]["session"] == "next-work"
    rolled = _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="rollover",
        idempotency_key=f"standalone-rollover-{uuid4().hex}",
        body={
            "address": address,
            "expected_address_generation": status["address_generation"],
            "expected_cycle_id": next_cycle["id"],
            "cycle": {
                "root": {
                    "content": {"type": "text", "text": "Continue in a new cycle."},
                    "session": "continued-work",
                }
            },
        },
        response_type=StartedCycleResponse,
    )["cycle"]
    assert rolled["number"] == 3
    rolled_root = read(
        "read-message",
        {"address": address, "cycle_id": rolled["id"], "message_id": rolled["root"]["id"]},
        response_type=MessageReadResponse,
    )
    assert rolled_root["message"]["sender"]["session"] == "continued-work"
    directory = read("cycles", {"address": address}, response_type=CycleListResponse)
    states = {item["id"]: item["state"] for item in directory["items"]}
    assert states == {cycle_id: "closed", next_cycle["id"]: "closed", rolled["id"]: "active"}
    _run_action(
        cli_path,
        base_url=base_url,
        key=sender_key,
        action="close",
        idempotency_key=f"standalone-final-close-{uuid4().hex}",
        body={"address": address, "expected_cycle_id": rolled["id"]},
        response_type=dict[str, JsonValue],
    )
    return address, cycle_id, reply_id


def _prove_admin_tunnel_recovery(
    cli_path: Path, *, base_url: str, receiver_key: str, tmp_path: Path
) -> None:
    username = "cli_recovery_" + uuid4().hex[:20]
    admin_file = tmp_path / "recovery-admin.key"
    agent_file = tmp_path / "recovery-agent.key"
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"

    def manage(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "manage.py", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )

    def admin[T](*arguments: str, response_type: type[T], body: JsonObject | None = None) -> T:
        return TypeAdapter(response_type).validate_python(
            _successful_json(
                _command(
                    cli_path,
                    base_url=base_url,
                    key=None,
                    arguments=["admin", *arguments],
                    body=body,
                    admin_key_file=admin_file,
                )
            ),
            strict=True,
        )

    try:
        bootstrap = manage("create_instance_admin", username, "--key-file", str(admin_file))
        assert bootstrap.returncode == 0, bootstrap.stderr
        assert admin_file.stat().st_mode & 0o777 == 0o600
        issued = admin(
            "agents",
            "create",
            "--idempotency-key",
            uuid4().hex,
            "--secret-output",
            str(agent_file),
            body={"name": "CLI recovery worker"},
            response_type=IssuedAgent,
        )
        assert agent_file.stat().st_mode & 0o777 == 0o600
        credential_id = issued["resource"]["id"]
        agent_key = agent_file.read_text().strip()
        created = [
            _run_action(
                cli_path,
                base_url=base_url,
                key=agent_key,
                action="create",
                idempotency_key=uuid4().hex,
                body={
                    "label": f"Recovery tunnel {index}",
                    "cycle": {
                        "label": "Retained recovery cycle",
                        "root": {"content": {"type": "text", "text": "Retain recovery history."}},
                    },
                },
                response_type=CreateTunnelResponse,
            )
            for index in range(3)
        ]
        admin(
            "agents",
            "revoke",
            "--idempotency-key",
            uuid4().hex,
            body={"credential_id": credential_id},
            response_type=dict[str, JsonValue],
        )
        page = admin(
            "tunnels",
            "list",
            "--creator-id",
            credential_id,
            "--limit",
            "2",
            response_type=RecoveryTunnelPage,
        )
        listed = page["items"]
        assert len(listed) == 2
        assert page["next_cursor"]
        next_cursor = page["next_cursor"]
        page = admin(
            "tunnels",
            "list",
            "--creator-id",
            credential_id,
            "--cursor",
            next_cursor,
            "--limit",
            "2",
            response_type=RecoveryTunnelPage,
        )
        assert len(page["items"]) == 1
        assert page["next_cursor"] is None
        listed.extend(page["items"])
        assert {item["id"] for item in listed} == {item["tunnel"]["id"] for item in created}
        selected_ids = {row["tunnel"]["id"] for row in created[:2]}
        targets: list[JsonValue] = []
        for item in listed:
            if item["id"] not in selected_ids:
                continue
            generation = item["address_generation"]
            assert generation is not None
            targets.append(
                {
                    "tunnel_id": item["id"],
                    "expected_address_generation": generation,
                    "confirmation": item["retirement_confirmation"],
                }
            )
        body: JsonObject = {"credential_id": credential_id, "tunnels": targets}
        retry_key = uuid4().hex
        result = admin(
            "tunnels",
            "retire-for-credential",
            "--idempotency-key",
            retry_key,
            body=body,
            response_type=RetirementResult,
        )
        assert result["resource"] == {
            "credential_id": credential_id,
            "retired_tunnel_ids": sorted(selected_ids),
        }
        assert (
            admin(
                "tunnels",
                "retire-for-credential",
                "--idempotency-key",
                retry_key,
                body=body,
                response_type=RetirementResult,
            )
            == result
        )
        receipt = admin(
            "operations", "get", result["operation"]["id"], response_type=RetirementReceipt
        )
        assert receipt["result"]["resource"] == result["resource"]
        for created_tunnel in created[:2]:
            tunnel_id = created_tunnel["tunnel"]["id"]
            assert (
                admin("tunnels", "get", tunnel_id, response_type=RecoveryTunnel)["state"]
                == "retired"
            )
            cycles = admin("tunnels", "cycles", tunnel_id, response_type=RetainedCyclePage)["items"]
            assert cycles[0]["state"] == "closed"
            assert cycles[0]["label"] == "Retained recovery cycle"
        denied = _command(
            cli_path,
            base_url=base_url,
            key=receiver_key,
            arguments=["read-tree"],
            body={"address": created[0]["tunnel"]["address"]},
        )
        assert denied.returncode == 1
        assert json.loads(denied.stderr)["error"]["code"] == "tunnel_unavailable"
        unselected = created[2]
        assert (
            admin("tunnels", "get", unselected["tunnel"]["id"], response_type=RecoveryTunnel)[
                "state"
            ]
            == "active"
        )
        readable = _run_action(
            cli_path,
            base_url=base_url,
            key=receiver_key,
            action="read-tree",
            body={"address": unselected["tunnel"]["address"]},
            response_type=TreeResponse,
        )
        assert readable["nodes"][0]["id"] == unselected["cycle"]["root"]["id"]
        roots = [row["cycle"]["root"]["id"] for row in created]
        retained = manage(
            "shell",
            "-c",
            "from tunnels.models import Message; "
            f"assert Message.objects.filter(pk__in={roots!r}, "
            "text_payload='Retain recovery history.').count() == 3",
        )
        assert retained.returncode == 0, retained.stderr
    finally:
        try:
            cleanup = manage(
                "shell",
                "-c",
                "from accounts.models import User, AdminCredential; "
                "from agents.models import AgentCredential; "
                "from tunnels.admin_actions import retire_tunnels_for_credentials; "
                "from django.utils import timezone; "
                f"users=User.objects.filter(username={username!r}); "
                "ids=list(AgentCredential.objects.filter(created_by__in=users)"
                ".values_list('id',flat=True)); "
                "retire_tunnels_for_credentials(credential_ids=ids,reason='cli_test_cleanup'); "
                "AgentCredential.objects.filter(pk__in=ids,revoked_at__isnull=True)"
                ".update(revoked_at=timezone.now()); "
                "AdminCredential.objects.filter(owner__in=users,revoked_at__isnull=True)"
                ".update(revoked_at=timezone.now()); users.update(is_active=False)",
            )
        finally:
            admin_file.unlink(missing_ok=True)
            agent_file.unlink(missing_ok=True)
        assert cleanup.returncode == 0, cleanup.stderr


def test_downloaded_agent_cli_runs_without_repository_imports(
    tmp_path: Path, executable_dir: Path
) -> None:
    required = {
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
    }
    missing = sorted(name for name in required if not os.getenv(name))
    assert not missing, "Missing standalone CLI setting names: " + ", ".join(missing)

    target_base_url = os.environ["STARTUNNEL_BASE_URL"]
    base_url = target_base_url.rstrip("/")
    download = httpx.get(f"{base_url}/downloads/star_tunnel.py", timeout=30)
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    cli_path = tmp_path / "star_tunnel.py"
    cli_path.write_bytes(download.content)
    installed = _successful_json(
        _command(
            cli_path,
            base_url=base_url,
            key=None,
            arguments=["install", "--bin-dir", str(executable_dir / "bin")],
        )
    )
    cli_path = Path(json_string(installed["command_path"]))

    sender_key = os.environ["STARTUNNEL_SENDER_KEY"]
    receiver_key = os.environ["STARTUNNEL_RECEIVER_KEY"]
    tutorial = _successful_json(
        _command(
            cli_path,
            base_url=base_url,
            key=None,
            arguments=["tutorial"],
            tutorial_keys=(sender_key, receiver_key),
        )
    )
    assert tutorial["status"] == "complete"

    sender = _successful_json(
        _command(
            cli_path,
            base_url=base_url,
            key=sender_key,
            arguments=["me"],
        )
    )
    assert TypeAdapter(MeResponse).validate_python(sender, strict=True)["agent"]["id"]

    _prove_tunnel_commands(
        cli_path,
        base_url=base_url,
        sender_key=sender_key,
        receiver_key=receiver_key,
    )
    _prove_admin_tunnel_recovery(
        cli_path, base_url=base_url, receiver_key=receiver_key, tmp_path=tmp_path
    )
