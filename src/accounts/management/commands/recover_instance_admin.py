"""Restore administrator access from the server."""

from __future__ import annotations

import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.admin_access import issue_admin_key, lock_admin_access
from accounts.models import User
from core.audit import record_event

from ._admin_key_file import reserve_key_file


class Command(BaseCommand):
    help = "Issue a recovery key for a named administrator from the server."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("username")
        parser.add_argument("--key-file", required=True)
        parser.add_argument("--key-name", default="recovery")

    def handle(self, *args: Any, **options: Any) -> None:
        path, descriptor = reserve_key_file(options["key_file"])
        try:
            with os.fdopen(descriptor, "w") as output, transaction.atomic():
                lock_admin_access()
                user = User.objects.filter(username=options["username"]).first()
                if user is None:
                    raise CommandError("This administrator does not exist.")
                user.is_active = True
                user.save(update_fields=["is_active"])
                credential, secret = issue_admin_key(owner=user, name=options["key_name"])
                output.write(secret + "\n")
                output.flush()
                os.fsync(output.fileno())
                record_event(
                    actor_user=user,
                    action="admin.recovered",
                    target_type="admin_credential",
                    target_id=credential.id,
                    channel="server",
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self.stdout.write(f"Recovered administrator {user.username}. Key file: {path}")
