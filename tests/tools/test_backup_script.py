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
    if [ -n "${BACKUP_TEST_COMPETITOR:-}" ]; then
        mkdir "$BACKUP_TEST_COMPETITOR"
        printf 'another archive\\n' > "$BACKUP_TEST_COMPETITOR/owner"
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


def test_destination_created_during_dump_is_preserved(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    backup = tmp_path / "instance.backup"
    result = subprocess.run(
        ["sh", "-c", FAKE_DATABASE, str(ROOT / "scripts/backup-startunnel.sh"), str(backup)],
        env={
            **os.environ,
            "BACKUP_TEST_COMMANDS": str(commands),
            "BACKUP_TEST_COMPETITOR": str(backup),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (backup / "owner").read_text() == "another archive\n"
    assert list(backup.iterdir()) == [backup / "owner"]
    assert not list(tmp_path.glob("*.partial.*"))


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [("", 0), ("createdb", 1), ("pg_restore", 1), ("probe", 1), ("dropdb", 1)],
)
def test_restore_runs_production_grants_and_shared_role_probe(
    tmp_path: Path, failure: str, expected_status: int
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
        *createdb*) [ "$RESTORE_FAILURE" != createdb ] ;;
        *dropdb*) [ "$RESTORE_FAILURE" != dropdb ] ;;
        *startunnel-check-runtime-role*) [ "$RESTORE_FAILURE" != probe ] ;;
        *information_schema.tables*) printf '5\n' ;;
        *'SELECT count(*) FROM django_migrations;'*) printf '8\n' ;;
        *pg_restore*) cat >/dev/null; [ "$RESTORE_FAILURE" != pg_restore ] ;;
    esac
}
. "$0"
"""
    result = subprocess.run(
        ["sh", "-c", fake_docker, str(ROOT / "scripts/verify-startunnel-restore.sh"), str(backup)],
        env={
            **os.environ,
            "RESTORE_COMMANDS": str(commands),
            "RESTORE_FAILURE": failure,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_status
    calls = commands.read_text().splitlines()
    assert "--template=template0" in calls[0]
    if failure == "createdb":
        assert len(calls) == 1
        assert "did not confirm ownership" in result.stderr
        return
    assert calls[-1].split()[4:7] == ["dropdb", "--if-exists", "--force"]
    if failure == "dropdb":
        assert "Could not remove the restore database" in result.stderr
    restore = next(call.split() for call in calls if "pg_restore" in call.split())
    assert "--no-owner" in restore and "--no-acl" in restore
    assert "--exit-on-error" in restore
    if failure == "pg_restore":
        assert len(calls) == 3
        return
    grants = next(call.split() for call in calls if "--file" in call.split())
    assert grants[-2:] == ["--file", "/usr/local/share/startunnel/runtime-grants.sql"]
    probe = next(
        call.split()
        for call in calls
        if "/usr/local/bin/startunnel-check-runtime-role" in call.split()
    )
    assert probe[-1].startswith("startunnel_restore_check_")
    assert "PGHOST=127.0.0.1" in probe


@pytest.mark.parametrize("failure", ["checksum", "writers", "bootstrap", "switch", ""])
def test_replacement_restore_guards_database_switch(tmp_path: Path, failure: str) -> None:
    backup = tmp_path / "instance.backup"
    backup.mkdir()
    dump = b"synthetic database dump"
    (backup / "postgres.dump").write_bytes(dump)
    digest = "bad" if failure == "checksum" else hashlib.sha256(dump).hexdigest()
    (backup / "manifest.sha256").write_text(digest + "  postgres.dump\n")
    commands = tmp_path / "commands"
    switch_sql = tmp_path / "switch.sql"
    fake_docker = r"""
docker() {
    printf '%s\n' "$*" >> "$RESTORE_COMMANDS"
    case "$*" in
        *"ps --status running"*)
            if [ "$RESTORE_FAILURE" = writers ]; then printf 'writer\n'; fi ;;
        *"pg_dump --schema-only"*"--dbname startunnel")
            printf 'bootstrap schema\n'
            if [ "$RESTORE_FAILURE" = bootstrap ]; then printf 'unexpected relation\n'; fi ;;
        *"pg_dump --schema-only"*) printf 'bootstrap schema\n' ;;
        *pg_largeobject_metadata*) printf '0\n' ;;
        *information_schema.tables*|*django_migrations*) printf '8\n' ;;
        *pg_restore*) cat >/dev/null ;;
        *"psql"*"--dbname postgres"*)
            cat > "$RESTORE_SWITCH_SQL"
            [ "$RESTORE_FAILURE" != switch ] ;;
    esac
}
. "$0"
"""
    result = subprocess.run(
        [
            "sh",
            "-c",
            fake_docker,
            str(ROOT / "scripts/restore-startunnel-replacement.sh"),
            str(backup),
        ],
        env={
            **os.environ,
            "RESTORE_COMMANDS": str(commands),
            "RESTORE_SWITCH_SQL": str(switch_sql),
            "RESTORE_FAILURE": failure,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode == 0) == (not failure)
    if failure == "checksum":
        assert "checksum does not match" in result.stderr
        assert not commands.exists()
        return
    calls = commands.read_text().splitlines()
    if failure == "writers":
        assert "must be stopped" in result.stderr
        assert len(calls) == 1
        return
    if failure == "bootstrap":
        assert "differs from a fresh bootstrap database" in result.stderr
        assert not any("pg_restore" in call for call in calls)
        assert not switch_sql.exists()
    else:
        assert switch_sql.read_text() == (
            'BEGIN;\nALTER DATABASE startunnel RENAME TO :"previous";\n'
            'ALTER DATABASE :"recovered" RENAME TO startunnel;\nCOMMIT;\n'
        )
        assert "startunnel_restore_" in result.stdout
        assert "startunnel_bootstrap_" in result.stdout
    if failure == "switch":
        assert "did not confirm completion" in result.stderr
    cleanup = [call for call in calls if "dropdb" in call]
    assert len(cleanup) == 1
    assert "startunnel_bootstrap_check_" in cleanup[0]
