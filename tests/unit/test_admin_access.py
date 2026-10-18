"""Administrator credentials and account controls share one domain service."""

from __future__ import annotations

import hmac
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pydantic import ValidationError as CommandValidationError

from accounts.admin_access import authenticate_admin_key, issue_admin_key
from accounts.admin_commands import (
    AccountCreate,
    AccountPassword,
    AccountState,
    AdminCommand,
    AgentCreate,
    AgentRevoke,
    KeyCreate,
    KeyRevoke,
    TunnelClose,
    TunnelRetire,
    TunnelRollover,
    TunnelRotate,
    TunnelStart,
)
from accounts.admin_data import AdminOperationResult
from accounts.admin_services import execute_admin_operation, list_admin_resources
from accounts.models import AdminCredential, AdminOperation, User
from tunnels.errors import IdempotencyConflict, InvalidCredential, InvalidRequest, LifecycleConflict
from tunnels.models import AuditEvent

pytestmark = pytest.mark.django_db


def run(
    actor: User, command_type: type[AdminCommand], data: dict[str, Any], key: str | None = None
) -> AdminOperationResult:
    return execute_admin_operation(
        actor=actor,
        command=command_type.model_validate(data),
        key=key or str(uuid4()),
        request_data=data,
    )


def test_admin_unlabeled_retry_keeps_old_digest_and_label_conflicts(
    user_factory: Any, credential_factory: Any
) -> None:
    from tunnels.cycles import create_tunnel

    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    created = create_tunnel(
        credential=credential,
        idempotency_key="admin-session-base",
        root_content={"type": "text", "text": "Initial root"},
    )
    run(
        actor,
        TunnelClose,
        {"tunnel_id": str(created.tunnel.id), "expected_cycle_id": str(created.cycle.id)},
    )
    data = {
        "tunnel_id": str(created.tunnel.id),
        "expected_address_generation": 1,
        "root_content": {"type": "text", "text": "New root"},
        "cycle_label": "",
        "expires_in_seconds": None,
    }
    key = "admin-session-retry"
    first = run(actor, TunnelStart, data, key)
    old_body = json.dumps(
        {"operation": "tunnels.start", "data": data},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode()
    expected = hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), old_body, "sha256")
    receipt = AdminOperation.objects.get(pk=first["operation"]["id"])
    assert bytes(receipt.request_digest) == expected
    assert run(actor, TunnelStart, {**data, "root_session": None}, key) == first
    with pytest.raises(IdempotencyConflict):
        run(actor, TunnelStart, {**data, "root_session": "chat-a"}, key)

    from tunnels.models import Cycle

    cycle = Cycle.objects.get(pk=first["resource"]["active_cycle_id"])
    assert cycle.root_message is not None and cycle.root_message.session is None
    labeled = run(
        actor,
        TunnelRollover,
        {
            **data,
            "expected_cycle_id": str(cycle.id),
            "root_content": {"type": "text", "text": "Labeled root"},
            "root_session": "operator-chat",
        },
        "admin-session-labeled",
    )
    labeled_cycle = Cycle.objects.get(pk=labeled["resource"]["active_cycle_id"])
    assert labeled_cycle.root_message is not None
    assert labeled_cycle.root_message.session == "operator-chat"


def test_passwordless_account_replay_and_audit(user_factory: Any) -> None:
    actor = user_factory()
    key = str(uuid4())
    data = {"username": "automation", "key_name": "runner"}
    created = run(actor, AccountCreate, data, key)
    assert run(actor, AccountCreate, data, key) == created
    target = User.objects.get(pk=created["resource"]["id"])
    assert not target.has_usable_password()
    credential = authenticate_admin_key(created["secret"])
    assert credential.owner == target
    stored = AdminOperation.objects.get(pk=created["operation"]["id"])
    assert created["secret"] not in str(stored.response)
    event = AuditEvent.objects.get(pk=created["operation"]["audit_event_id"])
    assert event.actor_user == actor
    assert event.channel == "browser"
    with pytest.raises(IdempotencyConflict):
        run(actor, AccountCreate, {"username": "different"}, key)
    with pytest.raises(LifecycleConflict):
        run(
            actor,
            AccountState,
            {"admin_id": str(target.id), "active": False, "expected_active": False},
        )
    run(
        actor,
        AccountState,
        {"admin_id": str(target.id), "active": False, "expected_active": True},
    )
    with pytest.raises(InvalidCredential):
        authenticate_admin_key(created["secret"])
    with pytest.raises(LifecycleConflict):
        run(actor, AccountCreate, data, key)
    run(
        actor,
        AccountState,
        {"admin_id": str(target.id), "active": True, "expected_active": False},
    )
    assert AdminCredential.objects.get(pk=credential.id).revoked_at is not None


def test_admin_key_isolation_expiry_and_stale_actor(
    user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    credential, secret = issue_admin_key(owner=actor, name="runner")
    assert authenticate_admin_key(secret).id == credential.id
    assert authenticate_admin_key(secret).last_used_at is not None
    _, agent_key = credential_factory(user=actor)
    for value in [agent_key, "sta_" + "!" * 43, "sta_" + "A" * 42 + "="]:
        with pytest.raises(InvalidCredential):
            authenticate_admin_key(value)
    credential.expires_at = timezone.now() - timedelta(seconds=1)
    credential.save(update_fields=["expires_at"])
    with pytest.raises(InvalidCredential):
        authenticate_admin_key(secret)
    with pytest.raises(InvalidCredential):
        request_data = {"name": "unused"}
        execute_admin_operation(
            actor=actor,
            credential=credential,
            command=AgentCreate.model_validate(request_data),
            key=str(uuid4()),
            request_data=request_data,
        )


def test_creation_validation_and_cursor(user_factory: Any) -> None:
    actor = user_factory()
    for data in [
        {"username": ""},
        {"username": "invalid space"},
        {"username": "short", "password": "x"},
    ]:
        with pytest.raises(InvalidRequest):
            run(actor, AccountCreate, data)
    created = run(
        actor,
        AccountCreate,
        {"username": "password-user", "password": "proper-long-password-8295"},
    )
    assert "secret" not in created
    with pytest.raises(InvalidRequest):
        run(actor, KeyCreate, {"admin_id": str(actor.id), "name": ""})
    for data in [
        {"admin_id": str(actor.id), "name": "expired", "expires_at": "yesterday"},
        {"admin_id": "bad", "name": "invalid"},
    ]:
        with pytest.raises(CommandValidationError):
            run(actor, KeyCreate, data)
    page = list_admin_resources("accounts", actor=actor, limit=1)
    assert page["next_cursor"]
    rest = list_admin_resources("accounts", actor=actor, limit=1, cursor=page["next_cursor"])
    assert rest["items"][0]["id"] != page["items"][0]["id"]
    with pytest.raises(InvalidRequest):
        list_admin_resources("accounts", actor=actor, cursor=page["next_cursor"], state="active")
    with pytest.raises(InvalidRequest):
        list_admin_resources("accounts", actor=actor, limit=0)
    assert list_admin_resources("accounts", actor=actor, state="inactive")["items"] == []


def test_receipts_are_owner_scoped_and_keys_replay_only_when_available(user_factory: Any) -> None:
    actor = user_factory()
    replacement, _ = issue_admin_key(owner=actor, name="replacement")
    key = str(uuid4())
    result = run(actor, AgentCreate, {"name": "worker"}, key)
    request_data = {"name": "worker"}
    assert (
        execute_admin_operation(
            actor=actor,
            credential=replacement,
            command=AgentCreate.model_validate(request_data),
            key=key,
            request_data=request_data,
        )["secret"]
        == result["secret"]
    )
    run(actor, AgentRevoke, {"credential_id": result["resource"]["id"]})
    with pytest.raises(LifecycleConflict):
        run(actor, AgentCreate, {"name": "worker"}, key)
    other = user_factory()
    assert (
        run(other, AgentCreate, {"name": "worker"}, key)["operation"]["id"]
        != result["operation"]["id"]
    )
    receipt = AdminOperation.objects.get(pk=result["operation"]["id"])
    receipt.expires_at = timezone.now() - timedelta(seconds=1)
    receipt.save(update_fields=["expires_at"])
    assert (
        run(actor, AgentCreate, {"name": "worker"}, key)["operation"]["id"]
        != result["operation"]["id"]
    )


def test_bootstrap_and_recovery_private_files(tmp_path: Any) -> None:
    path = tmp_path / "bootstrap.key"
    call_command("create_instance_admin", "runner", key_file=str(path))
    assert path.stat().st_mode & 0o777 == 0o600
    secret = path.read_text().strip()
    credential = authenticate_admin_key(secret)
    assert not credential.owner.has_usable_password()
    with pytest.raises(CommandError):
        call_command("recover_instance_admin", "runner", key_file=str(path))
    assert path.read_text().strip() == secret
    recovered = tmp_path / "recovery.key"
    call_command("recover_instance_admin", "runner", key_file=str(recovered))
    assert authenticate_admin_key(recovered.read_text().strip()).owner == credential.owner
    missing = tmp_path / "missing.key"
    with pytest.raises(CommandError):
        call_command("recover_instance_admin", "missing", key_file=str(missing))
    assert not missing.exists()


def test_tunnel_operations_replay_and_revoked_addresses(
    user_factory: Any, credential_factory: Any
) -> None:
    from accounts.admin_data import tunnel_resource
    from tunnels.cycles import create_tunnel

    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    created = create_tunnel(
        credential=credential,
        idempotency_key=str(uuid4()),
        root_content={"type": "text", "text": "Initial root"},
        label="admin-cycle",
    )
    identifier = str(created.tunnel.id)
    inspected = tunnel_resource(created.tunnel)
    assert inspected["address"] == created.address
    run(
        actor,
        TunnelClose,
        {"tunnel_id": identifier, "expected_cycle_id": inspected["active_cycle_id"]},
    )
    run(
        actor,
        TunnelStart,
        {
            "tunnel_id": identifier,
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Next root"},
        },
    )
    inspected = tunnel_resource(created.tunnel)
    rolled = run(
        actor,
        TunnelRollover,
        {
            "tunnel_id": identifier,
            "expected_cycle_id": inspected["active_cycle_id"],
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Rollover root"},
        },
    )
    assert rolled["resource"]["active_cycle_id"] != inspected["active_cycle_id"]
    key = str(uuid4())
    data = {"tunnel_id": identifier, "expected_address_generation": 1}
    rotated = run(actor, TunnelRotate, data, key)
    assert rotated["resource"]["address_generation"] == 2
    assert rotated["resource"]["address"]
    assert "secret" not in rotated
    assert run(actor, TunnelRotate, data, key) == rotated
    run(
        actor,
        TunnelRotate,
        {"tunnel_id": identifier, "expected_address_generation": 2},
        str(uuid4()),
    )
    with pytest.raises(LifecycleConflict):
        run(actor, TunnelRotate, data, key)
    assert list_admin_resources("cycles", actor=actor, tunnel_id=created.tunnel.id)["items"]
    current = tunnel_resource(created.tunnel)
    run(
        actor,
        TunnelRetire,
        {
            "tunnel_id": identifier,
            "confirmation": "admin-cycle",
            "expected_address_generation": current["address_generation"],
        },
    )
    with pytest.raises(LifecycleConflict):
        run(actor, TunnelRotate, data, key)
    assert (
        list_admin_resources("tunnels", actor=actor, state="retired")["items"][0]["id"]
        == identifier
    )
    assert tunnel_resource(created.tunnel)["address"] is None


@pytest.mark.parametrize("inactive_reason", ["revoked", "expired"])
def test_admin_can_start_and_roll_over_after_tunnel_creator_is_inactive(
    user_factory: Any, credential_factory: Any, inactive_reason: str
) -> None:
    from tunnels.cycles import create_tunnel

    creator_user = user_factory()
    actor = user_factory()
    creator, _ = credential_factory(user=creator_user)
    created = create_tunnel(
        credential=creator,
        idempotency_key=f"inactive-creator-{inactive_reason}",
        root_content={"type": "text", "text": "Initial root"},
        label="inactive-creator",
    )
    if inactive_reason == "revoked":
        creator.revoked_at = timezone.now()
        creator.save(update_fields=["revoked_at"])
    else:
        creator.expires_at = timezone.now() - timedelta(seconds=1)
        creator.save(update_fields=["expires_at"])

    run(
        actor,
        TunnelClose,
        {"tunnel_id": str(created.tunnel.id), "expected_cycle_id": str(created.cycle.id)},
    )
    started = run(
        actor,
        TunnelStart,
        {
            "tunnel_id": str(created.tunnel.id),
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Admin start"},
        },
    )
    started_cycle_id = started["resource"]["active_cycle_id"]
    rolled = run(
        actor,
        TunnelRollover,
        {
            "tunnel_id": str(created.tunnel.id),
            "expected_cycle_id": started_cycle_id,
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Admin rollover"},
        },
    )

    from tunnels.models import Cycle

    rolled_cycle_id = rolled["resource"]["active_cycle_id"]
    admin_cycles = Cycle.objects.filter(pk__in=[started_cycle_id, rolled_cycle_id])
    assert admin_cycles.count() == 2
    for cycle in admin_cycles.select_related("root_message"):
        assert cycle.creator_id == creator.id
        assert cycle.root_message is not None
        assert cycle.root_message.sender_id == creator.id
    events = AuditEvent.objects.filter(
        action="cycle.started", target_id__in=admin_cycles.values("id")
    )
    assert events.count() == 2
    assert set(events.values_list("actor_user_id", flat=True)) == {actor.id}


def test_validation_failure_is_atomic_and_safe(user_factory: Any) -> None:
    actor = user_factory()
    initial = User.objects.count()
    with pytest.raises(InvalidRequest):
        run(actor, AccountCreate, {"username": "invalid-key-owner", "key_name": ""})
    assert User.objects.count() == initial
    assert AdminOperation.objects.count() == 0
    cases: list[tuple[type[AdminCommand], dict[str, Any]]] = [
        (AccountPassword, {"admin_id": str(actor.id), "password": "x"}),
        (
            KeyCreate,
            {
                "admin_id": str(actor.id),
                "name": "past",
                "expires_at": (timezone.now() - timedelta(seconds=1)).isoformat(),
            },
        ),
        (KeyRevoke, {"key_id": str(uuid4())}),
    ]
    for command_type, data in cases:
        with pytest.raises(InvalidRequest):
            run(actor, command_type, data)
    with pytest.raises(CommandValidationError):
        AccountState.model_validate({"admin_id": actor.id, "active": True})
    with pytest.raises(InvalidRequest):
        execute_admin_operation(
            actor=actor,
            command=AgentCreate(name="nonfinite"),
            key=str(uuid4()),
            request_data={"name": "nonfinite", "bad": float("nan")},
        )
    assert AdminOperation.objects.count() == 0


def test_audit_pages_are_chronological_and_filtered(user_factory: Any) -> None:
    actor = user_factory()
    first = run(actor, AgentCreate, {"name": "first"})
    second = run(actor, AgentCreate, {"name": "second"})
    page = list_admin_resources("audit", actor=actor, limit=1, action="agents.create")
    assert page["items"][0]["id"] == second["operation"]["audit_event_id"]
    other = list_admin_resources(
        "audit", actor=actor, limit=1, action="agents.create", cursor=page["next_cursor"]
    )
    assert other["items"][0]["id"] == first["operation"]["audit_event_id"]
    assert list_admin_resources("agents", actor=actor, state="available")["items"]
    assert list_admin_resources("keys", actor=actor, state="revoked")["items"] == []
    with pytest.raises(InvalidRequest):
        list_admin_resources("keys", actor=actor, state="bad")
    with pytest.raises(InvalidRequest):
        list_admin_resources("accounts", actor=actor, state="bad")


def test_optional_browser_password_form() -> None:
    from accounts.forms import InstanceAdminCreationForm

    assert InstanceAdminCreationForm(data={"username": "key-only"}).is_valid()
    assert not InstanceAdminCreationForm(
        data={"username": "mismatch", "password1": "somepassword"}
    ).is_valid()


def test_safe_failure_audit_and_filters(user_factory: Any) -> None:
    actor = user_factory()
    credential, _ = issue_admin_key(owner=actor, name="automation")
    with pytest.raises(LifecycleConflict):
        request_data = {"admin_id": str(actor.id), "active": False, "expected_active": False}
        execute_admin_operation(
            actor=actor,
            credential=credential,
            command=AccountState.model_validate(request_data),
            key=str(uuid4()),
            request_data=request_data,
        )
    event = AuditEvent.objects.get(action="admin.operation_failed")
    assert event.actor_admin_credential == credential
    assert event.channel == "admin_api"
    assert event.metadata == {
        "outcome": "failure",
        "code": "lifecycle_conflict",
        "operation": "accounts.state",
    }
    assert event.target_id == actor.id
    page = list_admin_resources(
        "audit",
        actor=actor,
        actor_id=actor.id,
        credential_id=credential.id,
        target_type="user",
    )
    assert page["items"][0]["id"] == str(event.id)
    assert not list_admin_resources("audit", actor=actor, target_id=uuid4())["items"]


def test_secret_file_failure_rolls_back_bootstrap(tmp_path: Any, monkeypatch: Any) -> None:
    import os

    def fail_fsync(descriptor: int) -> None:
        raise OSError("Test output failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    path = tmp_path / "failed.key"
    with pytest.raises(OSError):
        call_command("create_instance_admin", "failed-output", key_file=str(path))
    assert not User.objects.filter(username="failed-output").exists()
    assert not path.exists()
    owner = User.objects.create_user(username="recover-failed")
    with pytest.raises(OSError):
        call_command("recover_instance_admin", owner.username, key_file=str(path))
    assert not AdminCredential.objects.filter(owner=owner).exists()
    assert not path.exists()


def test_recovery_key_can_use_a_symlinked_output_parent(tmp_path: Any) -> None:
    owner = User.objects.create_user(username="linked-recovery")
    directory = tmp_path / "actual"
    directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(directory, target_is_directory=True)
    key_file = linked_directory / "recovery.key"

    call_command("recover_instance_admin", owner.username, key_file=str(key_file))

    assert (directory / "recovery.key").is_file()
    assert key_file.stat().st_mode & 0o777 == 0o600
