"""Production backup fixtures verify installation identity and lock ownership."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from scripts import backup_production
from scripts.production_lock import ProductionLockBusy, production_lock
from scripts.start_production import PRODUCTION_SECRET_NAMES


def test_production_lock_serializes_independent_operations(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    command = [
        sys.executable,
        "scripts/production_lock.py",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(42)",
    ]
    environment = {"STARTUNNEL_SECRETS_DIR": str(directory)}
    with production_lock(directory):
        rejected = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert rejected.returncode == 2
    assert b"holds the instance lock" in rejected.stdout
    accepted = subprocess.run(command, env=environment, capture_output=True, check=False)
    assert accepted.returncode == 42


@pytest.mark.parametrize("changed", [False, True])
def test_backup_records_composition_or_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    for name in PRODUCTION_SECRET_NAMES:
        (directory / name).write_text(f"synthetic {name}\n")
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    recipients = tmp_path / "recipients"
    recipients.touch()
    env_file = tmp_path / "team.env"
    env_file.write_text("SYNTHETIC_CONFIGURATION=1\n")
    overlay = tmp_path / "override.yaml"
    overlay.write_text("services: {}\n")
    monkeypatch.setenv("STARTUNNEL_SECRETS_DIR", str(directory))
    monkeypatch.setenv("COMPOSE_FILE", str(overlay))
    monkeypatch.setattr(
        backup_production, "capture", lambda cmd: "" if "status" in cmd else "a" * 40
    )
    monkeypatch.setattr(backup_production, "render_model", lambda cmd: {})
    monkeypatch.setattr(
        backup_production,
        "validate_production_model",
        lambda *args, **kw: {"web": ("synthetic image", "")},
    )
    monkeypatch.setattr(backup_production, "running_identity", lambda *args, **kw: {"web": ["id"]})

    def run(args: list[str], **kwargs: object) -> None:
        if args[0] == "age":
            with tarfile.open(args[-1]) as archive:
                assert {
                    "config/00-override.yaml",
                    "production.env",
                    "installation.json",
                    "source-commit",
                    "secrets/postgres_runtime_password",
                    "database.backup",
                } <= set(archive.getnames())
            Path(args[-2]).write_bytes(b"synthetic encrypted archive")
        else:
            assert kwargs["env"] == {
                **os.environ,
                "STARTUNNEL_BACKUP_ENV_FILE": str(env_file),
            }
            Path(args[1]).mkdir()
            if changed:
                overlay.write_text("services: {other: {}}\n")

    monkeypatch.setattr(backup_production, "subprocess", type("Commands", (), {"run": run}))
    if changed:
        with pytest.raises(ValueError, match="changed during backup"):
            backup_production.backup(backup_directory, recipients, env_file)
        assert list(backup_directory.iterdir()) == []
    else:
        path = backup_production.backup(backup_directory, recipients, env_file)
        assert path.suffix == ".age"
        assert list(backup_directory.iterdir()) == [path]


def test_deferred_backup_reports_off_host_verification_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / ".off-host-verified"
    marker.touch()

    def locked(*args: object) -> Path:
        raise ProductionLockBusy("held")

    monkeypatch.setattr(backup_production, "backup", locked)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup_production.py",
            "--output-directory",
            str(tmp_path),
            "--recipients",
            str(marker),
            "--env-file",
            str(marker),
        ],
    )
    assert backup_production.main() == 2
    message = capsys.readouterr().out
    assert "Backup deferred" in message
    assert "The last off-host verification was 0.0 hours ago." in message
