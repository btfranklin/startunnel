"""Production SQL and migration guards grant the same runtime permissions."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TypedDict
from uuid import UUID

import psycopg
import pytest
from django.apps import apps
from django.db import ProgrammingError, connection, transaction
from django.utils import timezone
from tests.factories import CredentialFactory, UserFactory

from accounts.admin_commands import AdminCommand, AgentCreate
from accounts.admin_services import execute_admin_operation
from accounts.models import AdminReceipt, User
from agents.models import AgentCredential
from core.audit import record_event
from core.models import AgentReceipt, AuditEvent
from tunnels.beacons import publish_beacon, withdraw_beacon
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import post_reply
from tunnels.migrations.sql.guards import revoke_runtime_history_delete
from tunnels.models import (
    ActivityCheckpoint,
    Beacon,
    BoardState,
    Cycle,
    Message,
    MessageMention,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)
from tunnels.retention import delete_due_cycle_content

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


class CreateTunnelArguments(TypedDict):
    credential: AgentCredential
    idempotency_key: str
    root_content: Mapping[str, object]


class AdminOperationArguments(TypedDict):
    actor: User
    command: AdminCommand
    key: str
    request_data: Mapping[str, object]


@contextmanager
def _runtime() -> Iterator[None]:
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE startunnel")
        yield
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _denied(sql: str, parameters: list[str | int | UUID]) -> None:
    with pytest.raises(ProgrammingError) as error, _runtime(), connection.cursor() as cursor:
        cursor.execute(sql, parameters)
    assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)


def _table_privileges() -> dict[str, tuple[bool, bool, bool, bool]]:
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT table_name,
                has_table_privilege('startunnel', format('public.%I', table_name), 'SELECT'),
                has_table_privilege('startunnel', format('public.%I', table_name), 'INSERT'),
                has_table_privilege('startunnel', format('public.%I', table_name), 'UPDATE'),
                has_table_privilege('startunnel', format('public.%I', table_name), 'DELETE')
            FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        return {row[0]: tuple(row[1:]) for row in cursor.fetchall()}


def test_production_grants_match_migrations_and_classify_every_table(tmp_path: Path) -> None:
    psql = shutil.which("psql")
    assert psql is not None, "Install the PostgreSQL client to run the production grant proof."
    broad_grant = (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO startunnel"
    )
    with connection.cursor() as cursor:
        cursor.execute(broad_grant)
    with connection.schema_editor() as schema_editor:
        revoke_runtime_history_delete(apps, schema_editor)
        migration = importlib.import_module("tunnels.migrations.0011_protect_runtime_history")
        migration.protect_runtime_history(None, schema_editor)
    expected = _table_privileges()
    protected = {table for table, privileges in expected.items() if not privileges[-1]}
    assert protected == {
        model._meta.db_table
        for model in (
            Tunnel,
            TunnelAddress,
            Cycle,
            Message,
            MessageMention,
            TunnelEvent,
            ActivityCheckpoint,
            AuditEvent,
            Beacon,
            BoardState,
        )
    }
    receipts = {AgentReceipt._meta.db_table, "accounts_adminoperation"}
    ordinary = {
        "accounts_admincredential",
        "accounts_user",
        "accounts_user_groups",
        "accounts_user_user_permissions",
        "agents_agentcredential",
        "auth_group",
        "auth_group_permissions",
        "auth_permission",
        "core_maintenancestate",
        "core_ratelimitbucket",
        "django_admin_log",
        "django_content_type",
        "django_migrations",
        "django_session",
    }
    assert set(expected) == protected | receipts | ordinary
    for table, privileges in expected.items():
        assert privileges == (
            True,
            True,
            table != AuditEvent._meta.db_table,
            table not in protected,
        )
    with connection.cursor() as cursor:
        cursor.execute(broad_grant)
    for source in (
        ROOT / "docker/postgres/runtime-grants.sql",
        ROOT / "src/tunnels/migrations/sql/history-grants.sql",
        ROOT / "src/tunnels/migrations/sql/runtime-history-guards.sql",
    ):
        shutil.copyfile(source, tmp_path / source.name)
    database = connection.settings_dict
    result = subprocess.run(
        [
            psql,
            "-X",
            "--set=ON_ERROR_STOP=on",
            "--set=database_name=" + database["NAME"],
            "--file",
            str(tmp_path / "runtime-grants.sql"),
        ],
        env={
            **os.environ,
            "PGDATABASE": database["NAME"],
            "PGHOST": database.get("HOST") or "",
            "PGPORT": str(database.get("PORT") or ""),
            "PGUSER": database.get("USER") or "",
            "PGPASSWORD": database.get("PASSWORD") or "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, "The production runtime grant SQL failed."
    assert _table_privileges() == expected
    for table in protected:
        with (
            pytest.raises(ProgrammingError) as error,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("SET LOCAL ROLE startunnel")
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(table)} WHERE FALSE")
        assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)
        assert error.value.__cause__.sqlstate == "42501"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege(current_user, %s, 'DELETE')", [Message._meta.db_table]
        )
        assert cursor.fetchone() == (True,)


def test_runtime_preserves_cycle_deadlines_beacons_and_audit(
    credential_factory: CredentialFactory,
    user_factory: UserFactory,
) -> None:
    credential, _ = credential_factory()
    actor = user_factory()
    with _runtime():
        created = create_tunnel(
            credential=credential,
            idempotency_key="runtime-history-create",
            root_content={"type": "text", "text": "Retained root"},
        )
        beacon, _ = publish_beacon(
            credential=credential,
            address=created.address,
            text="Retained Beacon",
            idempotency_key="runtime-history-publish",
        )
        post_reply(
            credential=credential,
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Retained reply"},
            idempotency_key="runtime-history-reply",
        )
    _denied(
        "UPDATE tunnels_cycle SET expires_at = expires_at - interval '1 day' WHERE id = %s",
        [created.cycle.id],
    )
    for field, value in (("text", "Changed"), ("publisher_id", actor.id)):
        _denied(f"UPDATE tunnels_beacon SET {field} = %s WHERE id = %s", [value, beacon["id"]])
    _denied("DELETE FROM tunnels_beacon WHERE id = %s", [beacon["id"]])
    _denied("DELETE FROM tunnels_boardstate WHERE id = %s", [1])
    with _runtime():
        withdraw_beacon(
            credential=credential,
            address=created.address,
            beacon_id=UUID(beacon["id"]),
            idempotency_key="runtime-history-withdraw",
        )
        close_cycle(
            credential=credential,
            address=created.address,
            expected_cycle_id=created.cycle.id,
            idempotency_key="runtime-history-close",
        )
    for sql in (
        "UPDATE tunnels_cycle SET delete_after = statement_timestamp() WHERE id = %s",
        "UPDATE tunnels_cycle SET delete_after = NULL WHERE id = %s",
        "UPDATE tunnels_cycle SET retention_seconds = 1 WHERE id = %s",
        "UPDATE tunnels_cycle SET state = 'deleted' WHERE id = %s",
        "UPDATE tunnels_cycle SET state = 'active', closed_at = NULL, "
        "final_sequence = NULL WHERE id = %s",
    ):
        _denied(sql, [created.cycle.id])
    event = record_event(
        action="test.history", target_type="test", target_id=None, actor_user=actor
    )
    _denied(
        "UPDATE tunnels_auditevent SET metadata = %s WHERE id = %s", ['{"changed": true}', event.id]
    )
    _denied("DELETE FROM tunnels_auditevent WHERE id = %s", [event.id])
    actor.delete()
    event.refresh_from_db()
    assert event.actor_user_id is None
    created.cycle.refresh_from_db()
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=timezone.now())
    assert created.cycle.delete_after is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=created.cycle.delete_after)
    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.DELETED
    assert not Message.objects.filter(cycle=created.cycle).exists()
    assert not Beacon.objects.filter(pk=beacon["id"]).exists()
    assert (
        AgentReceipt.objects.get(resource_id=beacon["id"], operation="publish_beacon").response
        == {}
    )


def test_runtime_receipts_finish_replay_and_reuse_only_after_expiry(
    credential_factory: CredentialFactory,
    user_factory: UserFactory,
) -> None:
    actor = user_factory()
    credential, _ = credential_factory()
    create_arguments: CreateTunnelArguments = {
        "credential": credential,
        "idempotency_key": "runtime-receipt-create",
        "root_content": {"type": "text", "text": "Retry root"},
    }
    admin_arguments: AdminOperationArguments = {
        "actor": actor,
        "command": AgentCreate(name="Retry agent"),
        "key": "runtime-receipt-admin",
        "request_data": {"name": "Retry agent"},
    }
    with _runtime():
        created = create_tunnel(**create_arguments)
        assert create_tunnel(**create_arguments).tunnel.id == created.tunnel.id
        admin_result = execute_admin_operation(**admin_arguments)
        assert execute_admin_operation(**admin_arguments) == admin_result
    receipts: list[AgentReceipt | AdminReceipt] = [
        AgentReceipt.objects.get(credential=credential, operation="create_tunnel"),
        AdminReceipt.objects.get(owner=actor),
    ]
    for receipt in receipts:
        table = connection.ops.quote_name(receipt._meta.db_table)
        for assignment in (
            "expires_at = statement_timestamp() - interval '1 second'",
            "request_digest = decode(repeat('00', 32), 'hex')",
            "response = '{}'::jsonb",
        ):
            _denied(f"UPDATE {table} SET {assignment} WHERE id = %s", [receipt.id])
        _denied(f"DELETE FROM {table} WHERE id = %s", [receipt.id])
        type(receipt).objects.filter(pk=receipt.id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    with _runtime():
        assert create_tunnel(**create_arguments).tunnel.id != created.tunnel.id
        assert execute_admin_operation(**admin_arguments)["resource"] != admin_result["resource"]
    assert not AgentReceipt.objects.filter(pk=receipts[0].id).exists()
    assert not AdminReceipt.objects.filter(pk=receipts[1].id).exists()
