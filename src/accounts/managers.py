"""Create local admin accounts."""

from __future__ import annotations

from sqlite3 import IntegrityError as SQLiteIntegrityError
from typing import TYPE_CHECKING

from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from psycopg.errors import UniqueViolation

if TYPE_CHECKING:
    from .models import User


CASE_INSENSITIVE_USERNAME_CONSTRAINT = "unique_admin_username_upper"
USERNAME_EXISTS = "That username already exists."


def save_new_user(user: User, *, using: str | None = None) -> None:
    database = using or router.db_for_write(type(user), instance=user)
    try:
        with transaction.atomic(using=database):
            user.save(force_insert=True, using=database)
    except IntegrityError as error:
        cause = error.__cause__
        if isinstance(cause, UniqueViolation):
            duplicate_username = cause.diag.constraint_name in {
                "accounts_user_username_key",
                CASE_INSENSITIVE_USERNAME_CONSTRAINT,
            }
        elif isinstance(cause, SQLiteIntegrityError):
            duplicate_username = str(error) in {
                "UNIQUE constraint failed: accounts_user.username",
                f"UNIQUE constraint failed: index '{CASE_INSENSITIVE_USERNAME_CONSTRAINT}'",
            }
        else:
            duplicate_username = False
        if duplicate_username:
            raise ValidationError({"username": USERNAME_EXISTS}) from error
        raise


class UserManager(DjangoUserManager["User"]):
    use_in_migrations = True

    def create_user(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: object,
    ) -> User:
        del email
        normalized_username = self.model.normalize_username(username).strip()
        if not normalized_username:
            raise ValueError("A username is required.")
        user = self.model(username=normalized_username, **extra_fields)
        user.full_clean(exclude={"password"})
        user.set_password(password)
        save_new_user(user, using=self._db)
        return user

    def create_superuser(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: object,
    ) -> User:
        del email
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields.get("is_staff") or not extra_fields.get("is_superuser"):
            raise ValueError("A superuser must have staff and superuser access.")
        return self.create_user(username, email=None, password=password, **extra_fields)
