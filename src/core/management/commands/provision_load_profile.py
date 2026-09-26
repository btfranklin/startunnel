"""Provision and remove short-lived identities for the opt-in load proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from agents.services import create_credential
from tunnels.services import retire_tunnels_for_credentials

_FIXTURE_PREFIX = "load-profile"
_MOUNT_ESCAPES = {
    r"\040": " ",
    r"\011": "\t",
    r"\012": "\n",
    r"\134": "\\",
}


def _decode_mount_path(value: str) -> str:
    for encoded, decoded in _MOUNT_ESCAPES.items():
        value = value.replace(encoded, decoded)
    return value


def _tmpfs_mount_for(path: Path) -> Path | None:
    """Return the containing tmpfs mount when Linux can prove it."""

    mount_info = Path("/proc/self/mountinfo")
    if not mount_info.is_file():
        return None
    matches: list[Path] = []
    for line in mount_info.read_text(encoding="utf-8").splitlines():
        try:
            left, right = line.split(" - ", 1)
            mount_path = Path(_decode_mount_path(left.split()[4])).resolve()
            filesystem_type = right.split()[0]
        except IndexError, ValueError, OSError:
            continue
        if filesystem_type == "tmpfs" and (path == mount_path or path.is_relative_to(mount_path)):
            matches.append(mount_path)
    return max(matches, key=lambda candidate: len(candidate.parts), default=None)


def _validated_new_tmpfs_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise CommandError("--output must be an absolute path on a tmpfs mount.")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise CommandError("The output directory does not exist.") from error
    path = parent / path.name
    if path.exists() or path.is_symlink():
        raise CommandError("The output file already exists. Use a new tmpfs path.")
    if _tmpfs_mount_for(parent) is None:
        raise CommandError("The output directory is not on a verified tmpfs mount.")
    return path


def _validated_existing_tmpfs_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise CommandError("--cleanup-manifest must be an absolute path on a tmpfs mount.")
    if path.is_symlink():
        raise CommandError("The load credential manifest must be a regular file.")
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise CommandError("The load credential manifest does not exist.") from error
    if not path.is_file() or path.is_symlink():
        raise CommandError("The load credential manifest must be a regular file.")
    if _tmpfs_mount_for(path.parent) is None:
        raise CommandError("The load credential manifest is not on a verified tmpfs mount.")
    status = path.stat()
    if status.st_uid != os.geteuid():
        raise CommandError("The load credential manifest must belong to the current user.")
    if status.st_mode & 0o777 != 0o600:
        raise CommandError("The load credential manifest must have mode 0600.")
    return path


def _write_manifest(path: Path, document: dict[str, object]) -> None:
    """Write one new mode-0600 JSON file without following a link."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        content = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    if path.stat().st_mode & 0o777 != 0o600:
        path.unlink(missing_ok=True)
        raise CommandError("The load credential manifest does not have mode 0600.")


def _load_run_id(path: Path) -> UUID:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {
            "credential_count",
            "credentials",
            "run_id",
            "user_count",
        }:
            raise ValueError
        raw_run_id = document["run_id"]
        if not isinstance(raw_run_id, str):
            raise ValueError
        run_id = UUID(raw_run_id)
        if raw_run_id != str(run_id):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise CommandError("The load credential manifest is not valid.") from error
    return run_id


def _fixture_prefix(run_id: UUID) -> str:
    return f"{_FIXTURE_PREFIX}-{run_id.hex}"


@transaction.atomic
def _remove_run(run_id: UUID) -> dict[str, int]:
    prefix = _fixture_prefix(run_id)
    users = User.objects.filter(username__startswith=f"l-{run_id.hex}")
    credentials = AgentCredential.objects.filter(
        created_by__in=users,
        name__startswith=prefix,
    )
    credential_ids = list(credentials.values_list("id", flat=True))
    now = timezone.now()
    revoked = credentials.filter(revoked_at__isnull=True).update(revoked_at=now)
    retired = retire_tunnels_for_credentials(
        credential_ids=credential_ids,
        reason="load_profile_cleanup",
    )
    users.update(is_active=False)
    return {
        "tunnels_retired": retired,
        "credentials_revoked": revoked,
    }


def _validate_counts(*, users: int, credentials: int) -> None:
    if not 1 <= users <= 5_000:
        raise CommandError("--users must be from 1 through 5000.")
    if not 1 <= credentials <= 5_000:
        raise CommandError("--credentials must be from 1 through 5000.")
    if credentials > settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT:
        raise CommandError("--credentials exceeds the instance credential limit.")


def _new_users(*, run_id: UUID, count: int) -> list[User]:
    unusable_password = make_password(None)
    users = [
        User(
            username=f"l-{run_id.hex}{index:04d}",
            is_active=True,
            password=unusable_password,
        )
        for index in range(count)
    ]
    User.objects.bulk_create(users)
    return users


def _provision(*, run_id: UUID, user_count: int, credential_count: int) -> dict[str, object]:
    users = _new_users(run_id=run_id, count=user_count)
    prefix = _fixture_prefix(run_id)
    entries: list[dict[str, str]] = []
    for index in range(credential_count):
        actor = users[index % len(users)]
        issued = create_credential(
            actor=actor,
            name=f"{prefix}-agent-{index:04d}",
        )
        entries.append(
            {
                "credential_id": str(issued.credential.id),
                "key": issued.key,
            }
        )

    return {
        "credential_count": credential_count,
        "credentials": entries,
        "run_id": str(run_id),
        "user_count": user_count,
    }


class Command(BaseCommand):
    help = "Provision or remove fixtures for the opt-in HTTP load proof."

    def add_arguments(self, parser: Any) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--output")
        group.add_argument("--cleanup-manifest")
        parser.add_argument("--users", type=int, default=500)
        parser.add_argument("--credentials", type=int, default=50)

    def handle(self, *args: object, **options: object) -> None:
        if not settings.DEBUG or os.getenv("STARTUNNEL_LOAD_TESTS") != "1":
            raise CommandError(
                "Use local DEBUG settings and set STARTUNNEL_LOAD_TESTS=1 for the load proof."
            )
        if cleanup_path := options.get("cleanup_manifest"):
            manifest = _validated_existing_tmpfs_path(str(cleanup_path))
            run_id = _load_run_id(manifest)
            counts = _remove_run(run_id)
            manifest.unlink()
            self.stdout.write(
                self.style.SUCCESS(
                    "Load fixtures were removed. "
                    f"Retired {counts['tunnels_retired']} tunnels and revoked "
                    f"{counts['credentials_revoked']} credentials."
                )
            )
            return

        users = cast(int, options["users"])
        credentials = cast(int, options["credentials"])
        _validate_counts(users=users, credentials=credentials)
        output_path = _validated_new_tmpfs_path(str(options["output"]))
        try:
            with transaction.atomic():
                document = _provision(
                    run_id=uuid4(),
                    user_count=users,
                    credential_count=credentials,
                )
                _write_manifest(output_path, document)
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        self.stdout.write(
            self.style.SUCCESS(
                f"Load fixtures are ready: {users} users and {credentials} credentials."
            )
        )
