"""Small PostgreSQL-owned runtime coordination records."""

from __future__ import annotations

import uuid

from django.db import models


class RateLimitBucket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key_digest = models.BinaryField(max_length=32, unique=True)
    accepted_at_ms = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return str(self.id)


class MaintenanceState(models.Model):
    key = models.CharField(primary_key=True, max_length=32, default="worker", editable=False)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    oldest_overdue_seconds = models.FloatField(default=0)
    last_error = models.CharField(max_length=240, blank=True)
    reconciliation_count = models.PositiveBigIntegerField(default=0)
    due_work_count = models.PositiveBigIntegerField(default=0)
    notification_wake_count = models.PositiveBigIntegerField(default=0)

    def __str__(self) -> str:
        return self.key
