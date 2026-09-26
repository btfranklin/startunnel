"""Create the first local human account for one StarTunnel instance."""

from __future__ import annotations

from getpass import getpass
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = "Create one active local StarTunnel administrator."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, **options: Any) -> None:
        username = User.normalize_username(options["username"]).strip()
        if not username:
            raise CommandError("Supply a username.")
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError("That username already exists.")

        password = getpass("Password: ")
        confirmation = getpass("Password (again): ")
        if password != confirmation:
            raise CommandError("The passwords do not match.")

        user = User(username=username, is_active=True, is_staff=False, is_superuser=False)
        try:
            user.full_clean(exclude={"password"})
            validate_password(password, user=user)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error

        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Created local administrator {username}."))
