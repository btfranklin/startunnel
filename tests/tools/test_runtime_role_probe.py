"""Execute the installed PostgreSQL role probe against controlled psql results."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docker/postgres/check-runtime-role.sh"
PSQL = """#!/usr/bin/env python3
import json, os, sys
sql = sys.stdin.read()
allowed = '--set=ON_ERROR_STOP=on' in sys.argv
with open(os.environ['PROBE_COMMANDS'], 'a') as output:
    output.write(json.dumps({'args': sys.argv[1:], 'sql': sql,
        'password_received': os.environ.get('PGPASSWORD') == os.environ['PROBE_PASSWORD']}) + '\\n')
if allowed:
    raise SystemExit(int(os.environ.get('PROBE_ALLOWED_STATUS', '0')))
sys.stdout.write(os.environ['PROBE_SQLSTATE'])
raise SystemExit(int(os.environ['PROBE_STATUS']))
"""


@pytest.mark.parametrize(
    ("state", "status", "allowed_status", "expected"),
    [
        ("42501\n", 0, 0, 0),
        ("00000\n", 0, 0, 1),
        ("23503\n", 0, 0, 1),
        ("28P01\n", 0, 0, 1),
        ("", 2, 0, 1),
        ("42501\n", 2, 0, 1),
        ("extra\n42501\n", 0, 0, 1),
        ("42501\n", 0, 1, 1),
    ],
)
def test_runtime_role_probe_requires_permission_denial_and_normal_writes(
    executable_dir: Path, state: str, status: int, allowed_status: int, expected: int
) -> None:
    executable = executable_dir / "psql"
    executable.write_text(PSQL, encoding="utf-8")
    executable.chmod(0o755)
    commands = executable_dir / "commands.jsonl"
    password = "synthetic-probe-password"
    result = subprocess.run(
        [str(PROBE), "restored_database"],
        env={
            **os.environ,
            "PATH": str(executable_dir) + os.pathsep + os.environ["PATH"],
            "STARTUNNEL_DB_RUNTIME_PASSWORD": password,
            "PROBE_PASSWORD": password,
            "PROBE_COMMANDS": str(commands),
            "PROBE_SQLSTATE": state,
            "PROBE_STATUS": str(status),
            "PROBE_ALLOWED_STATUS": str(allowed_status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected
    assert result.stdout == ""
    assert password not in result.stderr
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    assert all(call["password_received"] for call in calls)
    assert all(call["args"][-2:] == ["--dbname", "restored_database"] for call in calls)
    assert "--set=ON_ERROR_STOP=on" in calls[0]["args"]
    assert "INSERT INTO django_session" in calls[0]["sql"]
    assert "UPDATE django_session" in calls[0]["sql"]
    assert "DELETE FROM django_session" in calls[0]["sql"]
    if expected == 0:
        assert len(calls) == 6
        assert all("--set=ON_ERROR_STOP=off" in call["args"] for call in calls[1:])
        assert calls[-2]["sql"].splitlines()[0] == "DELETE FROM tunnels_message WHERE FALSE;"
        assert calls[-1]["sql"].splitlines()[0] == "DELETE FROM tunnels_auditevent WHERE FALSE;"
    else:
        assert len(calls) == (1 if allowed_status else 2)
