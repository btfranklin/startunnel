"""Production database credential rotation is coordinated and non-echoing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import rotate_postgres_passwords as rotation
from scripts.rotate_postgres_passwords import PasswordPair, RotationError
from scripts.start_production import PRODUCTION_SECRET_NAMES


def _write_secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _production_secrets(directory: Path, pair: PasswordPair) -> None:
    for name in PRODUCTION_SECRET_NAMES:
        value = f"safe-{name}"
        if name == rotation.ADMIN_SECRET_NAME:
            value = pair.admin
        elif name == rotation.RUNTIME_SECRET_NAME:
            value = pair.runtime
        _write_secret(directory / name, value)


def _install_database_simulation(
    monkeypatch: pytest.MonkeyPatch,
    initial: PasswordPair,
    *,
    fail_first_restart: bool = False,
) -> tuple[dict[str, PasswordPair], list[str], list[tuple[str, bool]]]:
    state = {"pair": initial}
    events: list[str] = []
    checks: list[tuple[str, bool]] = []

    def apply(
        command: list[str],
        *,
        authenticate_with: str,
        replacement: PasswordPair,
    ) -> None:
        del command
        assert authenticate_with == state["pair"].admin
        state["pair"] = replacement
        events.append("database")

    def works(command: list[str], *, role: str, password: str) -> bool:
        del command
        expected = state["pair"].admin if role == "startunnel_admin" else state["pair"].runtime
        accepted = password == expected
        checks.append((role, accepted))
        events.append("check")
        return accepted

    def restart(command: list[str]) -> None:
        del command
        events.append("restart")
        if fail_first_restart and events.count("restart") == 1:
            raise RotationError("safe simulated restart failure")

    def inspect(command: list[str]) -> None:
        del command
        events.append("inspect")

    monkeypatch.setattr(rotation, "apply_database_passwords", apply)
    monkeypatch.setattr(rotation, "credential_works", works)
    monkeypatch.setattr(rotation, "restart_database_clients", restart)
    monkeypatch.setattr(rotation, "inspect_runtime", inspect)
    return state, events, checks


def test_rotation_rejects_malformed_equal_and_reused_passwords() -> None:
    with pytest.raises(ValueError, match="32 to 128"):
        rotation.validate_password("too-short", label="Test password")
    with pytest.raises(ValueError, match="URL-safe"):
        rotation.validate_password("x" * 31 + "!", label="Test password")
    with pytest.raises(ValueError, match="must be different"):
        rotation.validate_password_pair(
            PasswordPair(admin="A" * 40, runtime="A" * 40),
            label="New",
        )
    with pytest.raises(ValueError, match="differ from both current"):
        rotation._require_fresh_replacement(
            PasswordPair(admin="A" * 40, runtime="B" * 40),
            PasswordPair(admin="B" * 40, runtime="C" * 40),
        )


def test_rotation_and_explicit_rollback_change_both_roles_and_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = PasswordPair(admin="A" * 40, runtime="B" * 40)
    new = PasswordPair(admin="C" * 40, runtime="D" * 40)
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir(mode=0o700)
    _production_secrets(secret_directory, old)
    new_admin_file = tmp_path / "new-admin"
    new_runtime_file = tmp_path / "new-runtime"
    _write_secret(new_admin_file, new.admin)
    _write_secret(new_runtime_file, new.runtime)
    rollback_directory = tmp_path / "rollback"
    rollback_directory.mkdir(mode=0o700)
    rollback_file = rollback_directory / "postgres.json"
    state, events, checks = _install_database_simulation(monkeypatch, old)

    rotation.rotate(
        directory=secret_directory,
        new_admin_file=new_admin_file,
        new_runtime_file=new_runtime_file,
        rollback_file=rollback_file,
        command=["safe-compose"],
    )

    assert state["pair"] == new
    assert rotation.read_password_pair(secret_directory) == new
    assert rollback_file.stat().st_mode & 0o777 == 0o600
    assert rotation.read_rollback_bundle(rollback_file).previous == old
    assert events == [
        "database",
        *("check" for _index in range(4)),
        "restart",
        *("check" for _index in range(4)),
        "inspect",
    ]
    assert [accepted for _role, accepted in checks[-4:]] == [True, True, False, False]

    checks.clear()
    rotation.rollback(
        directory=secret_directory,
        rollback_file=rollback_file,
        command=["safe-compose"],
    )

    assert state["pair"] == old
    assert rotation.read_password_pair(secret_directory) == old
    assert events[-11:] == [
        "database",
        *("check" for _index in range(4)),
        "restart",
        *("check" for _index in range(4)),
        "inspect",
    ]
    assert [accepted for _role, accepted in checks[-4:]] == [True, True, False, False]


def test_failed_restart_restores_the_previous_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = PasswordPair(admin="A" * 40, runtime="B" * 40)
    new = PasswordPair(admin="C" * 40, runtime="D" * 40)
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir(mode=0o700)
    _production_secrets(secret_directory, old)
    new_admin_file = tmp_path / "new-admin"
    new_runtime_file = tmp_path / "new-runtime"
    _write_secret(new_admin_file, new.admin)
    _write_secret(new_runtime_file, new.runtime)
    rollback_directory = tmp_path / "rollback"
    rollback_directory.mkdir(mode=0o700)
    rollback_file = rollback_directory / "postgres.json"
    state, events, checks = _install_database_simulation(
        monkeypatch,
        old,
        fail_first_restart=True,
    )

    with pytest.raises(RotationError, match="previous credentials are active"):
        rotation.rotate(
            directory=secret_directory,
            new_admin_file=new_admin_file,
            new_runtime_file=new_runtime_file,
            rollback_file=rollback_file,
            command=["safe-compose"],
        )

    assert state["pair"] == old
    assert rotation.read_password_pair(secret_directory) == old
    assert events == [
        "database",
        *("check" for _index in range(4)),
        "restart",
        "database",
        "restart",
        *("check" for _index in range(4)),
        "inspect",
    ]
    assert [accepted for _role, accepted in checks[-4:]] == [True, True, False, False]
    assert rollback_file.exists()


def test_rollback_repairs_a_partial_secret_file_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = PasswordPair(admin="A" * 40, runtime="B" * 40)
    new = PasswordPair(admin="C" * 40, runtime="D" * 40)
    mixed_files = PasswordPair(admin=new.admin, runtime=old.runtime)
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir(mode=0o700)
    _production_secrets(secret_directory, mixed_files)
    rollback_directory = tmp_path / "rollback"
    rollback_directory.mkdir(mode=0o700)
    rollback_file = rollback_directory / "postgres.json"
    rotation.write_rollback_bundle(rollback_file, old, new)
    state, _events, _checks = _install_database_simulation(monkeypatch, new)

    rotation.rollback(
        directory=secret_directory,
        rollback_file=rollback_file,
        command=["safe-compose"],
    )

    assert state["pair"] == old
    assert rotation.read_password_pair(secret_directory) == old


def test_database_passwords_are_sent_only_through_subprocess_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_password = "admin_private_input_marker_123456789"
    runtime_password = "runtime_private_input_marker_1234567"
    authentication_password = "current_private_input_marker_123456"
    observed: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.rotate_postgres_passwords.subprocess.run", run)
    rotation.apply_database_passwords(
        ["safe-compose"],
        authenticate_with=authentication_password,
        replacement=PasswordPair(admin=admin_password, runtime=runtime_password),
    )

    rendered_command = " ".join(observed["command"])
    assert admin_password not in rendered_command
    assert runtime_password not in rendered_command
    assert authentication_password not in rendered_command
    assert all(marker in observed["input"] for marker in (admin_password, runtime_password))
    assert authentication_password in observed["input"]
    assert "env" not in observed
    assert observed["stdout"] is subprocess.DEVNULL
    assert observed["stderr"] is subprocess.DEVNULL


def test_rollback_bundle_must_be_new_and_in_a_private_directory(tmp_path: Path) -> None:
    current = PasswordPair(admin="A" * 40, runtime="B" * 40)
    replacement = PasswordPair(admin="C" * 40, runtime="D" * 40)
    public_directory = tmp_path / "public"
    public_directory.mkdir(mode=0o755)
    public_directory.chmod(0o755)
    with pytest.raises(ValueError, match="group or other"):
        rotation.write_rollback_bundle(
            public_directory / "rollback.json",
            current,
            replacement,
        )

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    rollback_file = private_directory / "rollback.json"
    rotation.write_rollback_bundle(rollback_file, current, replacement)
    with pytest.raises(ValueError, match="must be a new regular file"):
        rotation.write_rollback_bundle(rollback_file, current, replacement)


def test_prepare_creates_two_independent_non_echoing_inputs(tmp_path: Path) -> None:
    directory = tmp_path / "rotation"
    directory.mkdir(mode=0o700)

    admin_file, runtime_file = rotation.prepare_replacement_files(directory)

    pair = rotation.read_replacement_pair(admin_file, runtime_file)
    assert pair.admin != pair.runtime
    assert admin_file.stat().st_mode & 0o777 == 0o600
    assert runtime_file.stat().st_mode & 0o777 == 0o600


def test_rollback_rejects_a_bundle_for_another_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = PasswordPair(admin="A" * 40, runtime="B" * 40)
    expected_replacement = PasswordPair(admin="C" * 40, runtime="D" * 40)
    active = PasswordPair(admin="E" * 40, runtime="F" * 40)
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir(mode=0o700)
    _production_secrets(secret_directory, active)
    rollback_directory = tmp_path / "rollback"
    rollback_directory.mkdir(mode=0o700)
    rollback_file = rollback_directory / "postgres.json"
    rotation.write_rollback_bundle(rollback_file, old, expected_replacement)
    monkeypatch.setattr(
        rotation,
        "apply_database_passwords",
        lambda *args, **kwargs: pytest.fail("Database update must not run."),
    )
    monkeypatch.setattr(rotation, "credential_works", lambda *args, **kwargs: False)

    with pytest.raises(ValueError, match="does not match the active PostgreSQL roles"):
        rotation.rollback(
            directory=secret_directory,
            rollback_file=rollback_file,
            command=["safe-compose"],
        )


def test_replacement_secret_files_require_mode_0600(tmp_path: Path) -> None:
    admin = tmp_path / "admin"
    runtime = tmp_path / "runtime"
    _write_secret(admin, "A" * 40)
    _write_secret(runtime, "B" * 40)
    runtime.chmod(0o640)

    with pytest.raises(ValueError, match="mode 0600"):
        rotation.read_replacement_pair(admin, runtime)
