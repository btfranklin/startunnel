"""Production SQL and migration guards grant the same runtime permissions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest
from django.db import ProgrammingError, connection, transaction

from core.models import AgentReceipt, AuditEvent
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

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


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


def test_production_grants_match_migration_and_deny_only_history_deletes(tmp_path: Path) -> None:
    psql = shutil.which("psql")
    assert psql is not None, "Install the PostgreSQL client to run the production grant proof."
    broad_grant = (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO startunnel"
    )
    with connection.cursor() as cursor:
        cursor.execute(broad_grant)
    with connection.schema_editor() as schema_editor:
        revoke_runtime_history_delete(None, schema_editor)
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
        )
    }
    mutable = {model._meta.db_table for model in (Beacon, BoardState, AgentReceipt)}
    assert {table for table in expected if table.startswith("tunnels_")} == protected | mutable
    assert all(expected[table] == (True, True, True, True) for table in mutable)
    with connection.cursor() as cursor:
        cursor.execute(broad_grant)
    for source in (
        ROOT / "docker/postgres/runtime-grants.sql",
        ROOT / "src/tunnels/migrations/sql/history-grants.sql",
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
