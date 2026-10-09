"""Administrator credentials protect the final usable access path."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.admin_access import authenticate_admin_key, issue_admin_key
from accounts.admin_services import execute_admin_operation, list_admin_resources
from accounts.models import AdminCredential, AdminOperation, User
from tunnels.errors import IdempotencyConflict, InvalidCredential, InvalidRequest, LifecycleConflict
from tunnels.models import AuditEvent

pytestmark = pytest.mark.django_db


def run(
    actor: User, operation: str, data: dict[str, Any], key: str | None = None
) -> dict[str, Any]:
    return execute_admin_operation(
        actor=actor, operation=operation, key=key or str(uuid4()), data=data
    )


def test_passwordless_account_replay_and_audit(user_factory: Any) -> None:
    actor = user_factory()
    key = str(uuid4())
    data = {"username": "automation", "key_name": "runner"}
    created = run(actor, "accounts.create", data, key)
    assert run(actor, "accounts.create", data, key) == created
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
        run(actor, "accounts.create", {"username": "different"}, key)
    with pytest.raises(LifecycleConflict):
        run(
            actor,
            "accounts.state",
            {"admin_id": str(target.id), "active": False, "expected_active": False},
        )
    run(
        actor,
        "accounts.state",
        {"admin_id": str(target.id), "active": False, "expected_active": True},
    )
    with pytest.raises(InvalidCredential):
        authenticate_admin_key(created["secret"])
    with pytest.raises(LifecycleConflict):
        run(actor, "accounts.create", data, key)
    run(
        actor,
        "accounts.state",
        {"admin_id": str(target.id), "active": True, "expected_active": False},
    )
    assert AdminCredential.objects.get(pk=credential.id).revoked_at is not None


def test_last_access_and_expiring_keys(user_factory: Any) -> None:
    actor = user_factory()
    with pytest.raises(LifecycleConflict):
        run(actor, "accounts.password", {"admin_id": str(actor.id), "password": None})
    assert User.objects.get(pk=actor.pk).has_usable_password()
    expiring = run(
        actor,
        "keys.create",
        {
            "admin_id": str(actor.id),
            "name": "temporary",
            "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
        },
    )
    with pytest.raises(LifecycleConflict):
        run(actor, "accounts.password", {"admin_id": str(actor.id), "password": None})
    permanent = run(actor, "keys.create", {"admin_id": str(actor.id), "name": "recovery"})
    run(actor, "accounts.password", {"admin_id": str(actor.id), "password": None})
    with pytest.raises(LifecycleConflict):
        run(actor, "keys.revoke", {"key_id": permanent["resource"]["id"]})
    with pytest.raises(LifecycleConflict):
        run(
            actor,
            "accounts.state",
            {"admin_id": str(actor.id), "active": False, "expected_active": True},
        )
    run(actor, "keys.revoke", {"key_id": expiring["resource"]["id"]})
    with pytest.raises(InvalidCredential):
        authenticate_admin_key(expiring["secret"])


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
        execute_admin_operation(
            actor=actor,
            credential=credential,
            operation="agents.create",
            key=str(uuid4()),
            data={"name": "unused"},
        )


def test_creation_validation_and_cursor(user_factory: Any) -> None:
    actor = user_factory()
    for data in [
        {"username": ""},
        {"username": "invalid space"},
        {"username": "short", "password": "x"},
    ]:
        with pytest.raises(InvalidRequest):
            run(actor, "accounts.create", data)
    created = run(
        actor,
        "accounts.create",
        {"username": "password-user", "password": "proper-long-password-8295"},
    )
    assert "secret" not in created
    for data in [
        {"admin_id": str(actor.id), "name": ""},
        {"admin_id": str(actor.id), "name": "expired", "expires_at": "yesterday"},
        {"admin_id": "bad", "name": "invalid"},
    ]:
        with pytest.raises(InvalidRequest):
            run(actor, "keys.create", data)
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
    result = run(actor, "agents.create", {"name": "worker"}, key)
    assert (
        execute_admin_operation(
            actor=actor,
            credential=replacement,
            operation="agents.create",
            key=key,
            data={"name": "worker"},
        )["secret"]
        == result["secret"]
    )
    run(actor, "agents.revoke", {"credential_id": result["resource"]["id"]})
    with pytest.raises(LifecycleConflict):
        run(actor, "agents.create", {"name": "worker"}, key)
    other = user_factory()
    assert (
        run(other, "agents.create", {"name": "worker"}, key)["operation"]["id"]
        != result["operation"]["id"]
    )
    receipt = AdminOperation.objects.get(pk=result["operation"]["id"])
    receipt.expires_at = timezone.now() - timedelta(seconds=1)
    receipt.save(update_fields=["expires_at"])
    assert (
        run(actor, "agents.create", {"name": "worker"}, key)["operation"]["id"]
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
    from accounts.admin_services import tunnel_resource
    from tunnels.services import create_tunnel

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
    run(
        actor,
        "tunnels.close",
        {"tunnel_id": identifier, "expected_cycle_id": inspected["active_cycle_id"]},
    )
    run(
        actor,
        "tunnels.start",
        {
            "tunnel_id": identifier,
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Next root"},
        },
    )
    inspected = tunnel_resource(created.tunnel)
    rolled = run(
        actor,
        "tunnels.rollover",
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
    rotated = run(actor, "tunnels.rotate", data, key)
    assert rotated["resource"]["address_generation"] == 2
    assert run(actor, "tunnels.rotate", data, key) == rotated
    assert list_admin_resources("cycles", actor=actor, tunnel_id=created.tunnel.id)["items"]
    run(
        actor,
        "tunnels.retire",
        {"tunnel_id": identifier, "confirmation": "admin-cycle", "expected_address_generation": 2},
    )
    with pytest.raises(LifecycleConflict):
        run(actor, "tunnels.rotate", data, key)
    assert (
        list_admin_resources("tunnels", actor=actor, state="retired")["items"][0]["id"]
        == identifier
    )


def test_validation_failure_is_atomic_and_safe(user_factory: Any) -> None:
    actor = user_factory()
    initial = User.objects.count()
    with pytest.raises(InvalidRequest):
        run(actor, "accounts.create", {"username": "invalid-key-owner", "key_name": ""})
    assert User.objects.count() == initial
    assert AdminOperation.objects.count() == 0
    cases: list[tuple[str, dict[str, Any]]] = [
        ("accounts.state", {"admin_id": str(actor.id), "active": True}),
        ("accounts.password", {"admin_id": str(actor.id), "password": "x"}),
        (
            "keys.create",
            {
                "admin_id": str(actor.id),
                "name": "past",
                "expires_at": (timezone.now() - timedelta(seconds=1)).isoformat(),
            },
        ),
        ("keys.revoke", {"key_id": str(uuid4())}),
        ("unknown", {"tunnel_id": str(uuid4())}),
    ]
    for operation, data in cases:
        with pytest.raises(InvalidRequest):
            run(actor, operation, data)
    with pytest.raises(InvalidRequest):
        run(actor, "agents.create", {"name": "nonfinite", "bad": float("nan")})
    assert AdminOperation.objects.count() == 0


def test_audit_pages_are_chronological_and_filtered(user_factory: Any) -> None:
    actor = user_factory()
    first = run(actor, "agents.create", {"name": "first"})
    second = run(actor, "agents.create", {"name": "second"})
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
        execute_admin_operation(
            actor=actor,
            credential=credential,
            operation="accounts.state",
            key=str(uuid4()),
            data={"admin_id": str(actor.id), "active": False, "expected_active": True},
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
    unsafe = tmp_path / "link"
    unsafe.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CommandError):
        call_command("recover_instance_admin", owner.username, key_file=str(unsafe / "nested.key"))
