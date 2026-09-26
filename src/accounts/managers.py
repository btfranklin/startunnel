"""Create local human accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import UserManager as DjangoUserManager

if TYPE_CHECKING:
    from .models import User


class UserManager(DjangoUserManager["User"]):
    use_in_migrations = True

    def create_user(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        del email
        normalized_username = self.model.normalize_username(username).strip()
        if not normalized_username:
            raise ValueError("A username is required.")
        user = self.model(username=normalized_username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        **extra_fields: Any,
    ) -> User:
        del email
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields.get("is_staff") or not extra_fields.get("is_superuser"):
            raise ValueError("A superuser must have staff and superuser access.")
        return self.create_user(username, password=password, **extra_fields)
