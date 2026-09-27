"""Check the host backup command with native file permissions and a fake database."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("parent_mode", [0o700, 0o755])
def test_backup_checks_native_permissions_before_database_access(
    tmp_path: Path, parent_mode: int
) -> None:
    commands = tmp_path / "commands"
    # A shell function also works when the isolated test lane mounts /tmp as noexec.
    fake_database = """
docker() {
    printf '%s\\n' "$*" >> "$BACKUP_TEST_COMMANDS"
    case "$*" in
        *--services*) printf 'web\\nmaintenance\\n' ;;
        *pg_dump*) printf 'test database dump\\n' ;;
    esac
}
. "$0"
"""
    private = tmp_path / "private"
    private.mkdir(mode=parent_mode)
    private.chmod(parent_mode)
    backup = private / "instance.backup"

    result = subprocess.run(
        ["sh", "-c", fake_database, str(ROOT / "scripts/backup-startunnel.sh"), str(backup)],
        env={
            **os.environ,
            "BACKUP_TEST_COMMANDS": str(commands),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    if parent_mode != 0o700:
        assert result.returncode == 2
        assert "0700" in result.stderr
        assert not commands.exists()
        assert not backup.exists()
        return

    assert result.returncode == 0, result.stdout + result.stderr
    dump = backup / "postgres.dump"
    assert dump.read_bytes() == b"test database dump\n"
    assert dump.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o700
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert (backup / "manifest.sha256").read_text() == f"{digest}  postgres.dump\n"
    calls = commands.read_text().splitlines()
    assert "compose stop web maintenance" in calls
    assert calls[-1] == "compose up -d web maintenance"
