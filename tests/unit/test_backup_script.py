"""Check the host backup command with native file permissions and a fake database."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_DATABASE = """
docker() {
    printf '%s\\n' "$*" >> "$BACKUP_TEST_COMMANDS"
    if [ "${BACKUP_TEST_FAIL:-}" = 1 ]; then
        return 1
    fi
    printf 'test database dump\\n'
}
. "$0"
"""


@pytest.mark.parametrize("parent_mode", [0o700, 0o755])
def test_backup_accepts_accessible_parent_and_keeps_new_outputs_private(
    tmp_path: Path, parent_mode: int
) -> None:
    commands = tmp_path / "commands"
    private = tmp_path / "private"
    private.mkdir(mode=parent_mode)
    private.chmod(parent_mode)
    backup = private / "instance.backup"

    result = subprocess.run(
        ["sh", "-c", FAKE_DATABASE, str(ROOT / "scripts/backup-startunnel.sh"), str(backup)],
        env={
            **os.environ,
            "BACKUP_TEST_COMMANDS": str(commands),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    dump = backup / "postgres.dump"
    assert dump.read_bytes() == b"test database dump\n"
    assert dump.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o700
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert (backup / "manifest.sha256").read_text() == f"{digest}  postgres.dump\n"
    assert commands.read_text().splitlines() == [
        "compose exec -T postgres pg_dump --username startunnel_admin --dbname startunnel "
        "--format custom --compress 9 --no-owner --no-acl"
    ]


def test_failed_dump_removes_partial_backup(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    backup = private / "instance.backup"

    result = subprocess.run(
        ["sh", "-c", FAKE_DATABASE, str(ROOT / "scripts/backup-startunnel.sh"), str(backup)],
        env={**os.environ, "BACKUP_TEST_COMMANDS": str(commands), "BACKUP_TEST_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not backup.exists()
    assert not list(private.glob("*.partial.*"))
    assert len(commands.read_text().splitlines()) == 1


@pytest.mark.parametrize("probe_status", [0, 1])
def test_restore_runs_production_grants_and_shared_role_probe(
    tmp_path: Path, probe_status: int
) -> None:
    backup = tmp_path / "instance.backup"
    backup.mkdir()
    dump = b"synthetic database dump"
    (backup / "postgres.dump").write_bytes(dump)
    (backup / "manifest.sha256").write_text(hashlib.sha256(dump).hexdigest() + "  postgres.dump\n")
    commands = tmp_path / "commands"
    fake_docker = r"""
docker() {
    printf '%s\n' "$*" >> "$RESTORE_COMMANDS"
    case "$*" in
        *startunnel-check-runtime-role*) return "$RESTORE_PROBE_STATUS" ;;
        *information_schema.tables*) printf '5\n' ;;
        *'SELECT count(*) FROM django_migrations;'*) printf '8\n' ;;
        *pg_restore*) cat >/dev/null ;;
    esac
}
. "$0"
"""
    result = subprocess.run(
        ["sh", "-c", fake_docker, str(ROOT / "scripts/verify-startunnel-restore.sh"), str(backup)],
        env={
            **os.environ,
            "RESTORE_COMMANDS": str(commands),
            "RESTORE_PROBE_STATUS": str(probe_status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == probe_status
    calls = commands.read_text().splitlines()
    restore = next(call.split() for call in calls if "pg_restore" in call.split())
    assert "--no-owner" in restore and "--no-acl" in restore
    grants = next(call.split() for call in calls if "--file" in call.split())
    assert grants[-2:] == ["--file", "/usr/local/share/startunnel/runtime-grants.sql"]
    probe = next(
        call.split()
        for call in calls
        if "/usr/local/bin/startunnel-check-runtime-role" in call.split()
    )
    assert probe[-1].startswith("startunnel_restore_check_")
    assert calls[-1].split()[4:7] == ["dropdb", "--if-exists", "--force"]
