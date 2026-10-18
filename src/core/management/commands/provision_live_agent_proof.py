"""Provision short-lived identities for the opt-in live-agent proof."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from agents.services import create_credential
from tunnels.services import retire_tunnels_for_credentials

_PROOF_USERNAME = "live-agent-proof"
_CREDENTIAL_NAMES = (
    "Live proof sender",
    "Live proof receiver",
    "Live proof concurrent receiver",
)


def _validate_output_path(raw_path: str) -> Path:
    output_path = Path(raw_path)
    if not output_path.is_absolute():
        raise CommandError("--output must be an absolute path.")
    try:
        parent = output_path.parent.resolve(strict=True)
    except OSError as error:
        raise CommandError("The output directory does not exist.") from error
    output_path = parent / output_path.name
    if output_path.exists() or output_path.is_symlink():
        raise CommandError("The output file already exists. Use a new path.")
    return output_path


def _clear_previous_proof_data(user: User) -> None:
    """Close data owned by prior fixture credentials and revoke those credentials."""

    now = timezone.now()
    prior_credentials = AgentCredential.objects.filter(
        name__in=_CREDENTIAL_NAMES,
        created_by=user,
    )
    credential_ids = list(prior_credentials.values_list("id", flat=True))
    if not credential_ids:
        return
    retire_tunnels_for_credentials(
        credential_ids=credential_ids,
        reason="live_proof_reset",
    )
    AgentCredential.objects.filter(id__in=credential_ids, revoked_at__isnull=True).update(
        revoked_at=now
    )


def _write_environment(path: Path, values: dict[str, str]) -> None:
    """Write one new mode-0600 dotenv file without following a link."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        content = "".join(f"{name}={value}\n" for name, value in values.items()).encode()
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    if path.stat().st_mode & 0o777 != 0o600:
        path.unlink(missing_ok=True)
        raise CommandError("The credential file does not have mode 0600.")


class Command(BaseCommand):
    help = "Provision credentials for the opt-in live OpenAI agent proof."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--output", required=True)

    def handle(self, *args: object, **options: object) -> None:
        if not settings.DEBUG or os.getenv("STARTUNNEL_LIVE_AGENT_TESTS") != "1":
            raise CommandError(
                "Use a local DEBUG environment and set STARTUNNEL_LIVE_AGENT_TESTS=1 "
                "to provision the live-agent proof."
            )
        output_path = _validate_output_path(str(options["output"]))
        issued_ids: list[UUID] = []
        try:
            with transaction.atomic():
                user, _ = User.objects.get_or_create(
                    username=_PROOF_USERNAME,
                    defaults={"is_active": True},
                )
                user.is_active = True
                user.set_unusable_password()
                user.save(update_fields=["is_active", "password"])
                _clear_previous_proof_data(user)

                sender = create_credential(actor=user, name=_CREDENTIAL_NAMES[0])
                receiver = create_credential(actor=user, name=_CREDENTIAL_NAMES[1])
                concurrent_receiver = create_credential(actor=user, name=_CREDENTIAL_NAMES[2])
                issued_ids = [
                    sender.credential.id,
                    receiver.credential.id,
                    concurrent_receiver.credential.id,
                ]
                values = {
                    "STARTUNNEL_SENDER_KEY": sender.key,
                    "STARTUNNEL_RECEIVER_KEY": receiver.key,
                    "STARTUNNEL_RECEIVER_KEYS": (f"{receiver.key},{concurrent_receiver.key}"),
                    "STARTUNNEL_SENDER_ID": str(sender.credential.id),
                    "STARTUNNEL_RECEIVER_ID": str(receiver.credential.id),
                }
                _write_environment(output_path, values)
        except Exception:
            output_path.unlink(missing_ok=True)
            if issued_ids:
                AgentCredential.objects.filter(id__in=issued_ids).update(revoked_at=timezone.now())
            raise
        self.stdout.write(
            self.style.SUCCESS("Live-agent credentials are ready in a mode-0600 file.")
        )
