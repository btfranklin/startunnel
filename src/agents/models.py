"""Instance-owned agent credentials."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class AgentCredentialQuerySet(models.QuerySet["AgentCredential"]):
    def available(self, *, at: datetime | None = None) -> AgentCredentialQuerySet:
        checked_at = at or timezone.now()
        return self.filter(
            revoked_at__isnull=True,
            suspended_at__isnull=True,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=checked_at))


class AgentCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_agent_credentials",
    )
    name = models.CharField(max_length=100)
    display_prefix = models.CharField(max_length=12, db_index=True)
    key_digest = models.BinaryField(max_length=32, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    objects = AgentCredentialQuerySet.as_manager()

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.display_prefix})"

    @property
    def is_available(self) -> bool:
        return (
            self.revoked_at is None
            and self.suspended_at is None
            and (self.expires_at is None or self.expires_at > timezone.now())
        )
