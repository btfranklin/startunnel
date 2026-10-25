"""Production database credential rotation is coordinated and non-echoing."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
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


def test_rotation_accepts_text_passwords_but_rejects_invalid_and_reused_values() -> None:
    assert rotation.validate_password("short ' ü $", label="Test password") == "short ' ü $"
    assert rotation.validate_password("x" * 130, label="Test password") == "x" * 130
    for invalid in ("", "first\nsecond", "first\rsecond", "nul\x00byte"):
        with pytest.raises(ValueError, match="one nonempty text value"):
            rotation.validate_password(invalid, label="Test password")
    with pytest.raises(ValueError, match="UTF-8 text"):
        rotation.validate_password("unpaired \ud800", label="Test password")
    with pytest.raises(ValueError, match="must be different"):
        rotation.validate_password_pair(
            PasswordPair(admin="same ü", runtime="same ü"),
            label="New",
        )
    with pytest.raises(ValueError, match="differ from both current"):
        rotation._require_fresh_replacement(
            PasswordPair(admin="old ' ü", runtime="old $ é"),
            PasswordPair(admin="old $ é", runtime="new \\ ♥"),
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

    monkeypatch.setenv("STARTUNNEL_SECRETS_DIR", str(secret_directory))
    assert (
        rotation.main(
            [
                "rotate",
                "--new-admin-file",
                str(new_admin_file),
                "--new-runtime-file",
                str(new_runtime_file),
                "--rollback-file",
                str(rollback_file),
            ]
        )
        == 0
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

    with pytest.raises(RotationError, match="previous credentials are active") as failure:
        rotation.rotate(
            directory=secret_directory,
            new_admin_file=new_admin_file,
            new_runtime_file=new_runtime_file,
            rollback_file=rollback_file,
            command=["safe-compose"],
        )

    assert "safe simulated restart failure" in str(failure.value)
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
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.rotate_postgres_passwords.subprocess.run", run)
    rotation.apply_database_passwords(
        ["safe-compose"],
        authenticate_with=authentication_password,
        replacement=PasswordPair(admin=admin_password, runtime=runtime_password),
    )

    command = observed["command"]
    assert isinstance(command, list)
    rendered_command = " ".join(command)
    assert admin_password not in rendered_command
    assert runtime_password not in rendered_command
    assert authentication_password not in rendered_command
    input_text = observed["input"]
    assert isinstance(input_text, str)
    assert all(marker in input_text for marker in (admin_password, runtime_password))
    assert authentication_password in input_text
    assert "env" not in observed
    assert observed["stdout"] is subprocess.DEVNULL
    assert "stderr" not in observed


def test_credential_check_distinguishes_rejection_from_database_and_compose_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = 10
    monkeypatch.setattr(rotation, "_run_private_input", lambda *args, **kwargs: result)
    assert not rotation.credential_works(["safe-compose"], role="startunnel", password="test")

    result = 11
    with pytest.raises(RotationError, match="exit status 11"):
        rotation.credential_works(["safe-compose"], role="startunnel", password="test")

    result = 125
    with pytest.raises(RotationError, match="exit status 125"):
        rotation.credential_works(["safe-compose"], role="startunnel", password="test")


def test_database_child_reports_safe_failure_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "host": "postgres",
        "port": "5432",
        "database": "startunnel",
        "admin_role": "startunnel_admin",
        "runtime_role": "startunnel",
        "authenticate_with": "synthetic-old",
        "admin_password": "synthetic-new-admin",
        "runtime_password": "synthetic-new-runtime",
        "role": "startunnel",
        "password": "synthetic-password",
    }

    def execute(program: str, error: psycopg.Error) -> tuple[int, str]:
        def fail_connect(**kwargs: object) -> None:
            raise error

        monkeypatch.setattr(psycopg, "connect", fail_connect)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        with pytest.raises(SystemExit) as exit_status:
            exec(program, {})
        code = exit_status.value.code
        assert isinstance(code, int)
        return code, capsys.readouterr().err

    code, diagnostic = execute(
        rotation._UPDATE_ROLES_PROGRAM,
        psycopg.errors.InsufficientPrivilege("sensitive synthetic exception text"),
    )
    assert code == 11
    assert "InsufficientPrivilege" in diagnostic
    assert "42501" in diagnostic
    assert "sensitive synthetic exception text" not in diagnostic

    code, diagnostic = execute(
        rotation._CHECK_CREDENTIAL_PROGRAM,
        psycopg.OperationalError("FATAL: password authentication failed for user synthetic"),
    )
    assert code == 10
    assert diagnostic == ""

    code, diagnostic = execute(
        rotation._CHECK_CREDENTIAL_PROGRAM,
        psycopg.OperationalError("connection refused; sensitive synthetic exception text"),
    )
    assert code == 11
    assert "OperationalError" in diagnostic
    assert "SQLSTATE none" in diagnostic
    assert "sensitive synthetic exception text" not in diagnostic


def test_compose_restart_and_inspection_show_normal_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs
        print("safe Compose diagnostic", file=sys.stderr)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RotationError, match="did not restart cleanly"):
        rotation.restart_database_clients(["safe-compose"])
    with pytest.raises(RotationError, match="did not pass runtime inspection"):
        rotation.inspect_runtime(["safe-compose"])

    assert "safe Compose diagnostic" in capsys.readouterr().err
    assert commands[0][1] == "up"
    assert commands[1][1] == "exec"


def test_rollback_bundle_is_new_and_private_with_accessible_output_parent(tmp_path: Path) -> None:
    current = PasswordPair(admin="old ' ü " + "A" * 2100, runtime="old $ é " + "B" * 2100)
    replacement = PasswordPair(admin="new \\ ♥ " + "C" * 2100, runtime="new % ☀ " + "D" * 2100)
    public_directory = tmp_path / "public"
    public_directory.mkdir(mode=0o755)
    public_directory.chmod(0o755)
    rotation.write_rollback_bundle(public_directory / "rollback.json", current, replacement)
    assert (public_directory / "rollback.json").stat().st_mode & 0o777 == 0o600

    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    rollback_file = private_directory / "rollback.json"
    rotation.write_rollback_bundle(rollback_file, current, replacement)
    assert rotation.read_rollback_bundle(rollback_file) == rotation.RollbackBundle(
        previous=current, replacement=replacement
    )
    assert rollback_file.stat().st_size > 8192
    rollback_file.write_text(json.dumps(json.loads(rollback_file.read_text()), indent=2))
    assert rotation.read_rollback_bundle(rollback_file) == rotation.RollbackBundle(
        previous=current, replacement=replacement
    )
    with pytest.raises(ValueError, match="must be a new regular file"):
        rotation.write_rollback_bundle(rollback_file, current, replacement)

    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(private_directory, target_is_directory=True)
    rotation.write_rollback_bundle(linked_directory / "linked-rollback.json", current, replacement)
    assert (private_directory / "linked-rollback.json").stat().st_mode & 0o777 == 0o600


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


def test_replacement_secret_files_accept_readonly_and_symlinked_inputs(tmp_path: Path) -> None:
    admin = tmp_path / "admin"
    runtime = tmp_path / "runtime"
    _write_secret(admin, "admin ' ü $")
    _write_secret(runtime, "runtime \\ ♥ " + "B" * 130)
    runtime.chmod(0o400)
    linked_runtime = tmp_path / "runtime-link"
    linked_runtime.symlink_to(runtime)

    assert rotation.read_replacement_pair(admin, linked_runtime) == rotation.PasswordPair(
        admin="admin ' ü $", runtime="runtime \\ ♥ " + "B" * 130
    )


def test_replacement_keeps_managed_leaf_symlinks_and_target_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir()
    target_directory = tmp_path / "managed"
    target_directory.mkdir()
    admin_target = target_directory / rotation.ADMIN_SECRET_NAME
    runtime_target = target_directory / rotation.RUNTIME_SECRET_NAME
    _write_secret(admin_target, "old-admin")
    _write_secret(runtime_target, "old-runtime")
    runtime_target.chmod(0o640)
    old_admin = admin_target.stat()
    old_runtime = runtime_target.stat()
    (secret_directory / rotation.ADMIN_SECRET_NAME).symlink_to(admin_target)
    (secret_directory / rotation.RUNTIME_SECRET_NAME).symlink_to(runtime_target)

    def unexpected_chown(*args: object) -> None:
        pytest.fail("Matching owner and group must not require chown.")

    monkeypatch.setattr(os, "fchown", unexpected_chown)

    pair = PasswordPair(admin="A" * 40, runtime="B" * 40)
    rotation.replace_secret_pair(secret_directory, pair)

    assert (secret_directory / rotation.ADMIN_SECRET_NAME).is_symlink()
    assert (secret_directory / rotation.RUNTIME_SECRET_NAME).is_symlink()
    assert rotation.read_password_pair(secret_directory) == pair
    assert admin_target.stat().st_mode & 0o777 == 0o600
    assert runtime_target.stat().st_mode & 0o777 == 0o640
    assert (admin_target.stat().st_uid, admin_target.stat().st_gid) == (
        old_admin.st_uid,
        old_admin.st_gid,
    )
    assert (runtime_target.stat().st_uid, runtime_target.stat().st_gid) == (
        old_runtime.st_uid,
        old_runtime.st_gid,
    )


def test_replacement_metadata_failure_keeps_old_file_and_removes_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / rotation.ADMIN_SECRET_NAME
    _write_secret(target, "old-admin")

    def fail_chmod(*args: object) -> None:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(os, "fchmod", fail_chmod)
    with pytest.raises(OSError, match="simulated metadata failure"):
        rotation._atomic_replace_secret(target, "new-admin")

    assert target.read_text(encoding="utf-8") == "old-admin\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "document",
    ["{", "[]", "{}", '{"previous_admin_password": 1}', "\ud800"],
)
def test_rollback_rejects_malformed_json_bundles(tmp_path: Path, document: str) -> None:
    path = tmp_path / "rollback.json"
    path.write_bytes(document.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(ValueError, match="malformed"):
        rotation.read_rollback_bundle(path)
