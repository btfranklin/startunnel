"""Local admin accounts for one StarTunnel instance."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models

from .managers import UserManager


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email: None = None  # type: ignore[assignment]
    REQUIRED_FIELDS: ClassVar[list[str]] = []
    objects: ClassVar[UserManager] = UserManager()

    class Meta:
        ordering = ["username", "id"]

    def __str__(self) -> str:
        return self.username


class AdminCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="admin_credentials")
    name = models.CharField(max_length=100)
    display_prefix = models.CharField(max_length=12)
    key_digest = models.BinaryField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.display_prefix})"


class AdminOperation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="admin_operations")
    operation = models.CharField(max_length=120)
    key_digest = models.BinaryField(max_length=32)
    request_digest = models.BinaryField(max_length=32)
    response = models.JSONField(default=dict)
    derivation_nonce = models.BinaryField(max_length=32)
    secret_kind = models.CharField(max_length=20, blank=True)
    secret_resource_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "key_digest"], name="unique_admin_operation_key"
            )
        ]

    def __str__(self) -> str:
        return f"{self.operation}:{self.id}"
