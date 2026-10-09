"""PostgreSQL serializes administrator lockout checks and preserves installed data."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor

from accounts.admin_access import change_admin_state
from accounts.models import User
from tunnels.errors import LifecycleConflict

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_concurrent_deactivation_preserves_access() -> None:
    assert connection.vendor == "postgresql"
    first = User.objects.create_user(username="first", password="test-long-password-7291")
    second = User.objects.create_user(username="second", password="test-long-password-7282")
    barrier = Barrier(2)

    def deactivate(account_id: UUID) -> bool:
        close_old_connections()
        try:
            actor = User.objects.get(pk=account_id)
            barrier.wait(timeout=10)
            try:
                change_admin_state(
                    actor=actor, admin_id=actor.id, active=False, expected_active=True
                )
            except LifecycleConflict:
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deactivate, [first.id, second.id]))
    assert sorted(results) == [False, True]
    assert User.objects.filter(is_active=True).count() == 1


def test_upgrade_preserves_existing_identity_and_audit() -> None:
    assert connection.vendor == "postgresql"
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    old = [
        ("accounts", "0001_initial"),
        ("tunnels", "0001_initial"),
        ("agents", "0001_initial"),
        ("core", "0001_initial"),
    ]
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        old_user = apps.get_model("accounts", "User")
        old_agent = apps.get_model("agents", "AgentCredential")
        old_audit = apps.get_model("tunnels", "AuditEvent")
        account_id, credential_id, event_id = uuid4(), uuid4(), uuid4()
        account = old_user.objects.create(
            id=account_id,
            username="existing-admin",
            password="stored-password-hash",
            is_active=True,
        )
        old_agent.objects.create(
            id=credential_id,
            created_by=account,
            name="existing-agent",
            display_prefix="st_existing",
            key_digest=b"a" * 32,
        )
        old_audit.objects.create(
            id=event_id,
            actor_user=account,
            action="admin.created",
            target_type="user",
            target_id=account.id,
        )
        MigrationExecutor(connection).migrate(current)
        from agents.models import AgentCredential
        from tunnels.models import AuditEvent

        assert User.objects.get(pk=account_id).password == "stored-password-hash"
        assert bytes(AgentCredential.objects.get(pk=credential_id).key_digest) == b"a" * 32
        event = AuditEvent.objects.get(pk=event_id)
        assert event.action == "admin.created"
        assert event.actor_admin_credential_id is None
        assert event.channel == ""
    finally:
        MigrationExecutor(connection).migrate(current)
