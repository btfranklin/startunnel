"""Shared audit, retry receipt, and runtime coordination records."""

from __future__ import annotations

import uuid

from django.conf import settings
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


class AgentReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    credential = models.ForeignKey(
        "agents.AgentCredential", on_delete=models.CASCADE, related_name="agent_receipts"
    )
    operation = models.CharField(max_length=120)
    key_digest = models.BinaryField(max_length=32)
    request_digest = models.BinaryField(max_length=32)
    response = models.JSONField(default=dict)
    resource_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "tunnels_idempotencyrecord"
        constraints = [
            models.UniqueConstraint(
                fields=["credential", "operation", "key_digest"],
                name="unique_idempotency_key_per_operation",
            )
        ]

    def __str__(self) -> str:
        return f"{self.operation}:{self.id}"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor_credential = models.ForeignKey(
        "agents.AgentCredential", on_delete=models.SET_NULL, null=True, blank=True
    )
    actor_admin_credential = models.ForeignKey(
        "accounts.AdminCredential", on_delete=models.SET_NULL, null=True, blank=True
    )
    channel = models.CharField(max_length=20, blank=True)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80)
    target_id = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "tunnels_auditevent"
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"{self.action}:{self.id}"
