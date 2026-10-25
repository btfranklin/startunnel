"""Create the first local admin account for one StarTunnel instance."""

from __future__ import annotations

import os
from getpass import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from accounts.admin_access import issue_admin_key
from accounts.models import User
from core.audit import record_event

from ._admin_key_file import reserve_key_file


class Command(BaseCommand):
    help = "Create one active local StarTunnel administrator."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")
        parser.add_argument("--key-file")
        parser.add_argument("--key-name", default="bootstrap")

    def handle(self, *args: object, **options: object) -> None:
        username = User.normalize_username(options["username"]).strip()
        if not username:
            raise CommandError("Supply a username.")
        user = User(username=username, is_active=True, is_staff=False, is_superuser=False)
        try:
            user.full_clean(exclude={"password"})
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error

        if options["key_file"]:
            path, descriptor = reserve_key_file(str(options["key_file"]))
            try:
                with os.fdopen(descriptor, "w") as output, transaction.atomic():
                    user.set_unusable_password()
                    user.save()
                    credential, secret = issue_admin_key(owner=user, name=str(options["key_name"]))
                    output.write(secret + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                    record_event(
                        actor_user=user,
                        action="admin.bootstrap",
                        target_type="admin_credential",
                        target_id=credential.id,
                        channel="server",
                    )
            except Exception:
                path.unlink(missing_ok=True)
                raise
            self.stdout.write(f"Created administrator {username}. Key file: {path}")
            return

        password = getpass("Password: ")
        confirmation = getpass("Password (again): ")
        if password != confirmation:
            raise CommandError("The passwords do not match.")

        try:
            validate_password(password, user=user)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error

        with transaction.atomic():
            user.set_password(password)
            user.save()
            record_event(
                actor_user=user,
                action="admin.bootstrap",
                target_type="user",
                target_id=user.id,
                channel="server",
            )
        self.stdout.write(self.style.SUCCESS(f"Created local administrator {username}."))
