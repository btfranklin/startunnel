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
