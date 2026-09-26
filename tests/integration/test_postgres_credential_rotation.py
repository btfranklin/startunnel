"""A real PostgreSQL server proves role rotation and rollback authentication."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from scripts.rotate_postgres_passwords import (
    _CHECK_CREDENTIAL_PROGRAM,
    _UPDATE_ROLES_PROGRAM,
)

pytestmark = pytest.mark.postgres


def _run_program(program: str, payload: dict[str, str]) -> int:
    result = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(payload, separators=(",", ":")),
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode


def _check_payload(connection: dict[str, str], *, role: str, password: str) -> dict[str, str]:
    return {
        "host": connection["host"],
        "port": connection["port"],
        "database": connection["dbname"],
        "role": role,
        "password": password,
    }


def _update_payload(
    connection: dict[str, str],
    *,
    admin_role: str,
    runtime_role: str,
    authenticate_with: str,
    admin_password: str,
    runtime_password: str,
) -> dict[str, str]:
    return {
        "host": connection["host"],
        "port": connection["port"],
        "database": connection["dbname"],
        "admin_role": admin_role,
        "runtime_role": runtime_role,
        "authenticate_with": authenticate_with,
        "admin_password": admin_password,
        "runtime_password": runtime_password,
    }


def test_real_postgres_rejects_old_credentials_and_accepts_rollback() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL credential tests.")
    parsed_connection = conninfo_to_dict(database_url)
    host = parsed_connection.get("host")
    if not isinstance(host, str) or not host or host.startswith("/"):
        pytest.skip("TEST_DATABASE_URL must use TCP so password authentication is tested.")
    connection = {
        "host": host,
        "port": str(parsed_connection.get("port") or "5432"),
        "dbname": str(parsed_connection.get("dbname") or "startunnel"),
    }

    suffix = uuid4().hex[:12]
    admin_role = f"st_rotate_admin_{suffix}"
    runtime_role = f"st_rotate_runtime_{suffix}"
    old_admin = "A" * 40
    old_runtime = "B" * 40
    new_admin = "C" * 40
    new_runtime = "D" * 40

    with psycopg.connect(database_url, autocommit=True) as database:
        database.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
                sql.Identifier(admin_role),
                sql.Literal(old_admin),
            )
        )
        database.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(runtime_role),
                sql.Literal(old_runtime),
            )
        )
        database.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                sql.Identifier(runtime_role),
                sql.Identifier(admin_role),
            )
        )

    try:
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=admin_role, password=old_admin),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=runtime_role, password=old_runtime),
            )
            == 0
        )

        assert (
            _run_program(
                _UPDATE_ROLES_PROGRAM,
                _update_payload(
                    connection,
                    admin_role=admin_role,
                    runtime_role=runtime_role,
                    authenticate_with=old_admin,
                    admin_password=new_admin,
                    runtime_password=new_runtime,
                ),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=admin_role, password=old_admin),
            )
            != 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=runtime_role, password=old_runtime),
            )
            != 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=admin_role, password=new_admin),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=runtime_role, password=new_runtime),
            )
            == 0
        )

        assert (
            _run_program(
                _UPDATE_ROLES_PROGRAM,
                _update_payload(
                    connection,
                    admin_role=admin_role,
                    runtime_role=runtime_role,
                    authenticate_with=new_admin,
                    admin_password=old_admin,
                    runtime_password=old_runtime,
                ),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=admin_role, password=old_admin),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=runtime_role, password=old_runtime),
            )
            == 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=admin_role, password=new_admin),
            )
            != 0
        )
        assert (
            _run_program(
                _CHECK_CREDENTIAL_PROGRAM,
                _check_payload(connection, role=runtime_role, password=new_runtime),
            )
            != 0
        )
    finally:
        with psycopg.connect(database_url, autocommit=True) as database:
            database.execute(
                sql.SQL("DROP ROLE IF EXISTS {}, {}").format(
                    sql.Identifier(admin_role),
                    sql.Identifier(runtime_role),
                )
            )
