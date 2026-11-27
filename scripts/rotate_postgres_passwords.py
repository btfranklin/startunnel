"""Rotate both production PostgreSQL roles without showing password values."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .json_data import (
        json_object,
        json_string,
    )
else:
    from json_data import (
        json_object,
        json_string,
    )


if TYPE_CHECKING or __package__:
    from .production_lock import production_lock
    from .start_production import (
        ROOT,
        compose_command,
        production_secret_directory,
        read_validated_secret_file,
        validate_production_secret_files,
    )
else:
    from production_lock import production_lock
    from start_production import (
        ROOT,
        compose_command,
        production_secret_directory,
        read_validated_secret_file,
        validate_production_secret_files,
    )

ADMIN_SECRET_NAME = "postgres_admin_password"
RUNTIME_SECRET_NAME = "postgres_runtime_password"

_UPDATE_ROLES_PROGRAM = """
import json
import sys

import psycopg
from psycopg import sql

payload = json.load(sys.stdin)
try:
    with psycopg.connect(
        host=payload["host"],
        port=payload["port"],
        dbname=payload["database"],
        user=payload["admin_role"],
        password=payload["authenticate_with"],
        connect_timeout=5,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(payload["admin_role"]),
                    sql.Literal(payload["admin_password"]),
                )
            )
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(payload["runtime_role"]),
                    sql.Literal(payload["runtime_password"]),
                )
            )
except psycopg.Error as error:
    print(
        f"PostgreSQL {type(error).__name__} (SQLSTATE {error.sqlstate or 'none'})",
        file=sys.stderr,
    )
    raise SystemExit(11) from None
""".strip()

_CHECK_CREDENTIAL_PROGRAM = """
import json
import sys

import psycopg

payload = json.load(sys.stdin)
try:
    with psycopg.connect(
        host=payload["host"],
        port=payload["port"],
        dbname=payload["database"],
        user=payload["role"],
        password=payload["password"],
        connect_timeout=5,
    ) as connection:
        connection.execute("SELECT 1")
except psycopg.Error as error:
    if error.sqlstate == "28P01" or (
        isinstance(error, psycopg.OperationalError)
        and "password authentication failed for user" in str(error)
    ):
        raise SystemExit(10) from None
    print(
        f"PostgreSQL {type(error).__name__} (SQLSTATE {error.sqlstate or 'none'})",
        file=sys.stderr,
    )
    raise SystemExit(11) from None
""".strip()


class RotationError(RuntimeError):
    """Report a safe operator error without database or secret text."""


@dataclass(frozen=True, repr=False)
class PasswordPair:
    admin: str
    runtime: str


@dataclass(frozen=True, repr=False)
class RollbackBundle:
    previous: PasswordPair
    replacement: PasswordPair


def validate_password(value: str, *, label: str) -> str:
    """Require one UTF-8 password value that PostgreSQL can receive."""

    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{label} must contain one nonempty text value.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must contain UTF-8 text.") from error
    return value


def validate_password_pair(pair: PasswordPair, *, label: str) -> PasswordPair:
    validate_password(pair.admin, label=f"{label} admin password")
    validate_password(pair.runtime, label=f"{label} runtime password")
    if pair.admin == pair.runtime:
        raise ValueError(f"{label} admin and runtime passwords must be different.")
    return pair


def read_password_pair(directory: Path) -> PasswordPair:
    return validate_password_pair(
        PasswordPair(
            admin=read_validated_secret_file(
                directory / ADMIN_SECRET_NAME,
                name=ADMIN_SECRET_NAME,
            ),
            runtime=read_validated_secret_file(
                directory / RUNTIME_SECRET_NAME,
                name=RUNTIME_SECRET_NAME,
            ),
        ),
        label="Current",
    )


def read_replacement_pair(admin_file: Path, runtime_file: Path) -> PasswordPair:
    return validate_password_pair(
        PasswordPair(
            admin=read_validated_secret_file(admin_file, name="new admin password"),
            runtime=read_validated_secret_file(runtime_file, name="new runtime password"),
        ),
        label="New",
    )


def _require_fresh_replacement(current: PasswordPair, replacement: PasswordPair) -> None:
    current_values = (current.admin, current.runtime)
    for new_value in (replacement.admin, replacement.runtime):
        if new_value in current_values:
            raise ValueError(
                "Each new PostgreSQL password must differ from both current passwords."
            )


def _require_absolute(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path.")
    return path


def _write_exclusive(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ValueError("The credential output must be a new regular file.") from error
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_rollback_bundle(path: Path, current: PasswordPair, replacement: PasswordPair) -> None:
    payload = {
        "previous_admin_password": current.admin,
        "previous_runtime_password": current.runtime,
        "replacement_admin_password": replacement.admin,
        "replacement_runtime_password": replacement.runtime,
    }
    _write_exclusive(path, json.dumps(payload, sort_keys=True, separators=(",", ":")))


def prepare_replacement_files(directory: Path) -> tuple[Path, Path]:
    """Create two new independent password files without displaying values."""

    admin_file = directory / "postgres_admin_password.new"
    runtime_file = directory / "postgres_runtime_password.new"
    created: list[Path] = []
    try:
        _write_exclusive(admin_file, secrets.token_urlsafe(32))
        created.append(admin_file)
        _write_exclusive(runtime_file, secrets.token_urlsafe(32))
        created.append(runtime_file)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return admin_file, runtime_file


def read_rollback_bundle(path: Path) -> RollbackBundle:
    try:
        with path.open(encoding="utf-8") as source:
            payload = json_object(json.load(source))
    except ValueError as error:
        raise ValueError("The rollback file is malformed.") from error
    required = {
        "previous_admin_password",
        "previous_runtime_password",
        "replacement_admin_password",
        "replacement_runtime_password",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("The rollback file is malformed.")
    if not all(isinstance(payload[key], str) for key in required):
        raise ValueError("The rollback file is malformed.")
    previous = validate_password_pair(
        PasswordPair(
            admin=json_string(payload["previous_admin_password"]),
            runtime=json_string(payload["previous_runtime_password"]),
        ),
        label="Rollback",
    )
    replacement = validate_password_pair(
        PasswordPair(
            admin=json_string(payload["replacement_admin_password"]),
            runtime=json_string(payload["replacement_runtime_password"]),
        ),
        label="Replacement",
    )
    _require_fresh_replacement(previous, replacement)
    return RollbackBundle(
        previous=previous,
        replacement=replacement,
    )


def _run_private_input(command: list[str], *, program: str, payload: dict[str, str]) -> int:
    result = subprocess.run(
        [
            *command,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "/app/.venv/bin/python",
            "migrate",
            "-c",
            program,
        ],
        cwd=ROOT,
        input=json.dumps(payload, separators=(",", ":")),
        text=True,
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode


def apply_database_passwords(
    command: list[str],
    *,
    authenticate_with: str,
    replacement: PasswordPair,
) -> None:
    """Change both roles in one PostgreSQL transaction."""

    return_code = _run_private_input(
        command,
        program=_UPDATE_ROLES_PROGRAM,
        payload={
            "host": "postgres",
            "port": "5432",
            "database": "startunnel",
            "admin_role": "startunnel_admin",
            "runtime_role": "startunnel",
            "authenticate_with": authenticate_with,
            "admin_password": replacement.admin,
            "runtime_password": replacement.runtime,
        },
    )
    if return_code != 0:
        raise RotationError(f"Role update failed (exit status {return_code}). See stderr.")


def credential_works(command: list[str], *, role: str, password: str) -> bool:
    return_code = _run_private_input(
        command,
        program=_CHECK_CREDENTIAL_PROGRAM,
        payload={
            "host": "postgres",
            "port": "5432",
            "database": "startunnel",
            "role": role,
            "password": password,
        },
    )
    if return_code == 0:
        return True
    if return_code == 10:
        return False
    raise RotationError(f"Credential check failed (exit status {return_code}). See stderr.")


def verify_transition(command: list[str], *, previous: PasswordPair, current: PasswordPair) -> None:
    checks = (
        credential_works(command, role="startunnel_admin", password=current.admin),
        credential_works(command, role="startunnel", password=current.runtime),
        not credential_works(command, role="startunnel_admin", password=previous.admin),
        not credential_works(command, role="startunnel", password=previous.runtime),
    )
    if not all(checks):
        raise RotationError("PostgreSQL credential verification did not reach the required state.")


def _atomic_replace_secret(path: Path, value: str) -> None:
    path = path.resolve()
    current = path.stat()
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            replacement = os.fstat(output.fileno())
            if (replacement.st_uid, replacement.st_gid) != (current.st_uid, current.st_gid):
                os.fchown(output.fileno(), current.st_uid, current.st_gid)
            os.fchmod(output.fileno(), stat.S_IMODE(current.st_mode))
            output.write(value)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def replace_secret_pair(directory: Path, pair: PasswordPair) -> None:
    _atomic_replace_secret(directory / ADMIN_SECRET_NAME, pair.admin)
    _atomic_replace_secret(directory / RUNTIME_SECRET_NAME, pair.runtime)


def restart_database_clients(command: list[str]) -> None:
    result = subprocess.run(
        [
            *command,
            "up",
            "-d",
            "--no-build",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
            "migrate",
            "maintenance",
            "web",
        ],
        cwd=ROOT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RotationError("The production database clients did not restart cleanly.")


def inspect_runtime(command: list[str]) -> None:
    deadline = time.monotonic() + 180
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = subprocess.run(
                [
                    *command,
                    "exec",
                    "-T",
                    "web",
                    "env",
                    "-u",
                    "PROMETHEUS_MULTIPROC_DIR",
                    "/usr/local/bin/startunnel-entrypoint",
                    "/app/.venv/bin/python",
                    "manage.py",
                    "inspect_runtime",
                    "--require-ready",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(15, remaining),
                check=False,
            )
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RotationError("The restarted web service did not become ready within 180 seconds.")


def active_pair(command: list[str], *, first: PasswordPair, second: PasswordPair) -> PasswordPair:
    def check(pair: PasswordPair) -> tuple[bool, bool]:
        return (
            credential_works(command, role="startunnel_admin", password=pair.admin),
            credential_works(command, role="startunnel", password=pair.runtime),
        )

    first_checks = check(first)
    second_checks = check(second)
    if first_checks == (True, True) and second_checks == (False, False):
        return first
    if second_checks == (True, True) and first_checks == (False, False):
        return second
    raise RotationError("The rollback file does not match the active PostgreSQL roles.")


def install_pair(
    command: list[str], *, directory: Path, current: PasswordPair, rejected: PasswordPair
) -> None:
    replace_secret_pair(directory, current)
    restart_database_clients(command)
    verify_transition(command, previous=rejected, current=current)
    if read_password_pair(directory) != current:
        raise RotationError("The mounted password sources do not match the database credentials.")
    inspect_runtime(command)


def _automatic_restore(
    command: list[str], *, restore: PasswordPair, rejected: PasswordPair, directory: Path
) -> bool:
    try:
        current = active_pair(command, first=restore, second=rejected)
        if current != restore:
            try:
                apply_database_passwords(
                    command, authenticate_with=current.admin, replacement=restore
                )
            except OSError, RotationError:
                if active_pair(command, first=restore, second=rejected) != restore:
                    return False
            verify_transition(command, previous=rejected, current=restore)
        install_pair(command, directory=directory, current=restore, rejected=rejected)
    except OSError, RotationError, ValueError:
        return False
    return True


def rotate(
    *,
    directory: Path,
    new_admin_file: Path,
    new_runtime_file: Path,
    rollback_file: Path,
    command: list[str],
) -> None:
    validate_production_secret_files(directory)
    current = read_password_pair(directory)
    replacement = read_replacement_pair(new_admin_file, new_runtime_file)
    _require_fresh_replacement(current, replacement)
    write_rollback_bundle(rollback_file, current, replacement)

    try:
        apply_database_passwords(
            command,
            authenticate_with=current.admin,
            replacement=replacement,
        )
        verify_transition(command, previous=current, current=replacement)
        install_pair(command, directory=directory, current=replacement, rejected=current)
    except (OSError, RotationError, ValueError) as error:
        detail = f"{error} "
        restored = _automatic_restore(
            command,
            restore=current,
            rejected=replacement,
            directory=directory,
        )
        if restored:
            raise RotationError(
                "PostgreSQL credential rotation failed. "
                f"{detail}The previous credentials are active."
            ) from error
        raise RotationError(
            "PostgreSQL credential rotation failed. "
            f"{detail}Automatic rollback did not complete. "
            "Keep the rollback file and use the documented recovery procedure."
        ) from error


def rollback(
    *,
    directory: Path,
    rollback_file: Path,
    command: list[str],
) -> None:
    validate_production_secret_files(directory)
    bundle = read_rollback_bundle(rollback_file)
    try:
        initial = active_pair(command, first=bundle.replacement, second=bundle.previous)
    except RotationError as error:
        raise ValueError(str(error)) from error

    rejected = bundle.previous if initial == bundle.replacement else bundle.replacement
    try:
        if initial == bundle.replacement:
            apply_database_passwords(
                command,
                authenticate_with=initial.admin,
                replacement=bundle.previous,
            )
            verify_transition(command, previous=bundle.replacement, current=bundle.previous)
        install_pair(
            command, directory=directory, current=bundle.previous, rejected=bundle.replacement
        )
    except (OSError, RotationError, ValueError) as error:
        detail = f"{error} "
        restored = _automatic_restore(
            command, restore=initial, rejected=rejected, directory=directory
        )
        if restored:
            raise RotationError(
                "PostgreSQL rollback failed. "
                f"{detail}The pre-rollback credential state was restored."
            ) from error
        raise RotationError(
            "PostgreSQL rollback failed. "
            f"{detail}Automatic recovery did not complete. "
            "Keep the rollback file and recover the database before another attempt."
        ) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = subparsers.add_parser(
        "prepare", help="Create two new mode-0600 password input files."
    )
    prepare_parser.add_argument("--directory", required=True, type=Path)
    rotate_parser = subparsers.add_parser("rotate", help="Install two new role passwords.")
    rotate_parser.add_argument("--new-admin-file", required=True, type=Path)
    rotate_parser.add_argument("--new-runtime-file", required=True, type=Path)
    rotate_parser.add_argument("--rollback-file", required=True, type=Path)
    rollback_parser = subparsers.add_parser(
        "rollback", help="Restore the pair saved by an earlier rotation."
    )
    rollback_parser.add_argument("--rollback-file", required=True, type=Path)
    args = parser.parse_args(argv)

    directory = production_secret_directory()
    command = compose_command(edge=False)
    try:
        if args.operation == "prepare":
            output_directory = _require_absolute(args.directory, label="Output directory")
            admin_file, runtime_file = prepare_replacement_files(output_directory)
            print("Created two mode-0600 PostgreSQL password files. Values were not displayed.")
            print(f"Admin input: {admin_file}")
            print(f"Runtime input: {runtime_file}")
        elif args.operation == "rotate":
            rollback_file = _require_absolute(args.rollback_file, label="Rollback file")
            with production_lock(directory):
                rotate(
                    directory=directory,
                    new_admin_file=_require_absolute(args.new_admin_file, label="New admin file"),
                    new_runtime_file=_require_absolute(
                        args.new_runtime_file,
                        label="New runtime file",
                    ),
                    rollback_file=rollback_file,
                    command=command,
                )
            print("PostgreSQL credentials rotated. The old credentials no longer authenticate.")
            print(f"Keep the mode-0600 rollback file secure until acceptance: {rollback_file}")
        else:
            rollback_file = _require_absolute(args.rollback_file, label="Rollback file")
            with production_lock(directory):
                rollback(directory=directory, rollback_file=rollback_file, command=command)
            print("PostgreSQL credentials rolled back. The replacement credentials are inactive.")
    except (OSError, RotationError, ValueError) as error:
        print(str(error))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
