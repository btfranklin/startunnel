"""PostgreSQL keeps admin receipts and target-state races consistent."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from accounts.admin_access import (
    authenticate_admin_key,
    change_admin_state,
    issue_admin_key,
)
from accounts.admin_commands import (
    AccountCreate,
    AccountState,
    AdminCommand,
    AgentCreate,
    KeyCreate,
    TunnelClose,
)
from accounts.admin_data import AdminOperationResult
from accounts.admin_services import execute_admin_operation
from accounts.models import AdminCredential, AdminOperation, User
from tunnels.admin_actions import close_cycle_as_admin
from tunnels.cycles import create_tunnel
from tunnels.errors import IdempotencyConflict, InvalidCredential
from tunnels.models import AuditEvent, Cycle

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_independent_self_deactivations_can_leave_no_active_admins() -> None:
    assert connection.vendor == "postgresql"
    first = User.objects.create_user(username="first", password="test-long-password-7291")
    second = User.objects.create_user(username="second", password="test-long-password-7282")
    barrier = Barrier(2)

    def deactivate(account_id: UUID) -> None:
        close_old_connections()
        try:
            actor = User.objects.get(pk=account_id)
            barrier.wait(timeout=10)
            change_admin_state(actor=actor, admin_id=actor.id, active=False, expected_active=True)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(deactivate, [first.id, second.id]))
    assert User.objects.filter(is_active=True).count() == 0


def test_unrelated_tunnel_admin_writes_reach_domain_work_together(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    credential, _ = credential_factory(user=actor)
    first = create_tunnel(
        credential=credential,
        idempotency_key="independent-admin-first-tunnel",
        root_content={"type": "text", "text": "First"},
    )
    second = create_tunnel(
        credential=credential,
        idempotency_key="independent-admin-second-tunnel",
        root_content={"type": "text", "text": "Second"},
    )
    both_in_domain = Barrier(2)

    def close_after_both_arrive(*args: Any, **kwargs: Any) -> None:
        both_in_domain.wait(timeout=15)
        close_cycle_as_admin(*args, **kwargs)

    monkeypatch.setattr("accounts.admin_commands.close_cycle_as_admin", close_after_both_arrive)

    def close(tunnel_id: UUID, cycle_id: UUID, key: str) -> AdminOperationResult:
        close_old_connections()
        try:
            request_data = {"tunnel_id": str(tunnel_id), "expected_cycle_id": str(cycle_id)}
            return execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=TunnelClose.model_validate(request_data),
                key=key,
                request_data=request_data,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: close(*args),
                [
                    (first.tunnel.id, first.cycle.id, "independent-admin-first-close"),
                    (second.tunnel.id, second.cycle.id, "independent-admin-second-close"),
                ],
            )
        )
    assert len({item["operation"]["id"] for item in results}) == 2
    assert (
        Cycle.objects.filter(pk__in=[first.cycle.pk, second.cycle.pk], state="closed").count() == 2
    )


def test_identical_concurrent_admin_keys_create_one_result_and_audit() -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    both_started = Barrier(2)
    data = {"username": "created-once", "password": "test-long-password-7292"}

    def create() -> AdminOperationResult:
        close_old_connections()
        try:
            both_started.wait(timeout=15)
            return execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=AccountCreate.model_validate(data),
                key="same-admin-create-key",
                request_data=data,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: create(), range(2)))
    assert first == second
    assert User.objects.filter(username="created-once").count() == 1
    assert AdminOperation.objects.filter(owner=actor).count() == 1
    assert AuditEvent.objects.filter(action="accounts.create").count() == 1


def test_same_owner_agent_key_creations_both_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    from agents.services import create_credential

    both_in_domain = Barrier(2)

    def create_after_both_arrive(*args: Any, **kwargs: Any) -> Any:
        both_in_domain.wait(timeout=15)
        return create_credential(*args, **kwargs)

    monkeypatch.setattr("accounts.admin_commands.create_credential", create_after_both_arrive)

    def create(number: int) -> AdminOperationResult:
        close_old_connections()
        try:
            request_data = {"name": f"worker-{number}"}
            return execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=AgentCreate.model_validate(request_data),
                key=f"same-owner-agent-key-{number}",
                request_data=request_data,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert len({item["resource"]["id"] for item in results}) == 2
    assert AdminOperation.objects.filter(owner=actor).count() == 2
    assert AuditEvent.objects.filter(action="agents.create").count() == 2


@pytest.mark.parametrize("different_operation", [False, True])
def test_concurrent_changed_admin_requests_with_one_key_conflict(
    different_operation: bool,
) -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    both_started = Barrier(2)
    requests: list[tuple[type[AdminCommand], dict[str, Any]]] = [
        (AccountCreate, {"username": "first-request", "password": "test-long-password-7292"}),
        (
            AgentCreate if different_operation else AccountCreate,
            {"name": "second-request"}
            if different_operation
            else {"username": "second-request", "password": "test-long-password-7293"},
        ),
    ]

    def execute(request: tuple[type[AdminCommand], dict[str, Any]]) -> str:
        close_old_connections()
        try:
            both_started.wait(timeout=15)
            try:
                execute_admin_operation(
                    actor=User.objects.get(pk=actor.pk),
                    command=request[0].model_validate(request[1]),
                    key="changed-admin-request-key",
                    request_data=request[1],
                )
            except IdempotencyConflict:
                return "conflict"
            return "success"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, requests))
    assert sorted(outcomes) == ["conflict", "success"]
    assert AdminOperation.objects.filter(owner=actor).count() == 1
    assert AuditEvent.objects.filter(action__in=["accounts.create", "agents.create"]).count() == 1


def test_concurrent_retry_replaces_one_expired_receipt() -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    data = {"name": "replace-expired"}
    first = execute_admin_operation(
        actor=actor,
        command=AgentCreate.model_validate(data),
        key="expired-admin-key",
        request_data=data,
    )
    receipt = AdminOperation.objects.get(pk=first["operation"]["id"])
    receipt.expires_at = timezone.now() - timedelta(seconds=1)
    receipt.save(update_fields=["expires_at"])
    both_started = Barrier(2)

    def retry() -> AdminOperationResult:
        close_old_connections()
        try:
            both_started.wait(timeout=15)
            return execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=AgentCreate.model_validate(data),
                key="expired-admin-key",
                request_data=data,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry(), range(2)))
    assert results[0] == results[1]
    assert results[0]["operation"]["id"] != str(receipt.id)
    assert AdminOperation.objects.filter(owner=actor).count() == 1
    assert AuditEvent.objects.filter(action="agents.create").count() == 2


def test_key_issuance_finishes_before_concurrent_owner_deactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert connection.vendor == "postgresql"
    actor = User.objects.create_user(username="operator", password="test-long-password-7291")
    owner = User.objects.create_user(username="key-owner", password="test-long-password-7292")
    issued = Event()
    release_issuance = Event()
    deactivation_entered_domain = Event()
    deactivation_finished = Event()

    def pause_after_issue(*args: Any, **kwargs: Any) -> Any:
        result = issue_admin_key(*args, **kwargs)
        issued.set()
        assert release_issuance.wait(timeout=15)
        return result

    def observe_state_change(*args: Any, **kwargs: Any) -> User:
        deactivation_entered_domain.set()
        return change_admin_state(*args, **kwargs)

    monkeypatch.setattr("accounts.admin_commands.issue_admin_key", pause_after_issue)
    monkeypatch.setattr("accounts.admin_commands.change_admin_state", observe_state_change)

    def create_key() -> AdminOperationResult:
        close_old_connections()
        try:
            request_data = {"admin_id": str(owner.pk), "name": "new key"}
            return execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=KeyCreate.model_validate(request_data),
                key="race-owner-issue-key",
                request_data=request_data,
            )
        finally:
            close_old_connections()

    def deactivate_owner() -> None:
        close_old_connections()
        try:
            request_data = {"admin_id": str(owner.pk), "active": False, "expected_active": True}
            execute_admin_operation(
                actor=User.objects.get(pk=actor.pk),
                command=AccountState.model_validate(request_data),
                key="race-owner-deactivate-key",
                request_data=request_data,
            )
        finally:
            deactivation_finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        key_future = pool.submit(create_key)
        assert issued.wait(timeout=15)
        deactivate_future = pool.submit(deactivate_owner)
        assert deactivation_entered_domain.wait(timeout=15)
        try:
            assert not deactivation_finished.wait(timeout=0.3)
        finally:
            release_issuance.set()
        result = key_future.result(timeout=15)
        deactivate_future.result(timeout=15)
    assert not User.objects.get(pk=owner.pk).is_active
    assert AdminCredential.objects.get(pk=result["resource"]["id"]).revoked_at is not None
    change_admin_state(actor=actor, admin_id=owner.pk, active=True, expected_active=False)
    with pytest.raises(InvalidCredential):
        authenticate_admin_key(result["secret"])
