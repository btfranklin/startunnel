"""Durable stable-tunnel, cycle-tree, idempotency, and audit state."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models import Q

from agents.models import AgentCredential


class Tunnel(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        DORMANT = "dormant", "Dormant"
        RETIRED = "retired", "Retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    creator = models.ForeignKey(
        AgentCredential, on_delete=models.PROTECT, related_name="created_tunnels"
    )
    label = models.CharField(max_length=160, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.ACTIVE)
    next_cycle_number = models.PositiveIntegerField(default=1)
    next_event_position = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    dormant_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retired_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(state__in=["active", "dormant"], retired_at__isnull=True)
                    | Q(state="retired", retired_at__isnull=False)
                ),
                name="tunnel_retirement_state_matches",
            ),
        ]
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"Tunnel {self.id}"


class TunnelAddress(models.Model):
    class State(models.TextChoices):
        CURRENT = "current", "Current"
        RETIRED = "retired", "Retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(Tunnel, on_delete=models.CASCADE, related_name="addresses")
    address_digest = models.BinaryField(max_length=32)
    generation = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=12, choices=State.choices, default=State.CURRENT)
    issued_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retirement_reason = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(state="current", retired_at__isnull=True)
                    | Q(state="retired", retired_at__isnull=False)
                ),
                name="tunnel_address_retirement_matches",
            ),
            models.UniqueConstraint(
                fields=["address_digest"],
                name="unique_tunnel_address",
            ),
            models.UniqueConstraint(
                fields=["tunnel"],
                condition=Q(state="current"),
                name="one_current_address_per_tunnel",
            ),
            models.UniqueConstraint(
                fields=["tunnel", "generation"],
                name="unique_address_generation_per_tunnel",
            ),
        ]
        ordering = ["-issued_at", "id"]

    def __str__(self) -> str:
        return f"{self.state} address for {self.tunnel_id}"


class Cycle(models.Model):
    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(Tunnel, on_delete=models.CASCADE, related_name="cycles")
    creator = models.ForeignKey(
        AgentCredential, on_delete=models.PROTECT, related_name="created_cycles"
    )
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=160, blank=True)
    state = models.CharField(max_length=24, choices=State.choices, default=State.ACTIVE)
    root_message = models.OneToOneField(
        "Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="root_of_cycle",
    )
    next_sequence = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    retention_seconds = models.PositiveIntegerField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delete_after = models.DateTimeField(null=True, blank=True, db_index=True)
    close_reason = models.CharField(max_length=32, blank=True)
    final_sequence = models.PositiveBigIntegerField(null=True, blank=True)
    message_count = models.PositiveIntegerField(default=0)
    content_bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tunnel", "number"], name="unique_cycle_number_per_tunnel"
            ),
            models.UniqueConstraint(
                fields=["tunnel"], condition=Q(state="active"), name="one_active_cycle_per_tunnel"
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="active", closed_at__isnull=True, final_sequence__isnull=True)
                    | Q(
                        state__in=["closed", "deleted"],
                        closed_at__isnull=False,
                        final_sequence__isnull=False,
                    )
                ),
                name="cycle_active_close_state_matches",
            ),
        ]
        ordering = ["tunnel_id", "number"]

    def __str__(self) -> str:
        return f"Cycle {self.number} for {self.tunnel_id}"


class Message(models.Model):
    class PayloadType(models.TextChoices):
        TEXT = "text", "Text"
        JSON = "json", "JSON"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(Tunnel, on_delete=models.CASCADE, related_name="messages")
    cycle = models.ForeignKey(Cycle, on_delete=models.CASCADE, related_name="messages")
    parent = models.ForeignKey(
        "self", on_delete=models.RESTRICT, null=True, blank=True, related_name="replies"
    )
    sender = models.ForeignKey(
        AgentCredential, on_delete=models.PROTECT, related_name="sent_messages"
    )
    sender_name = models.CharField(max_length=120)
    sequence = models.PositiveBigIntegerField()
    depth = models.PositiveIntegerField()
    payload_type = models.CharField(max_length=8, choices=PayloadType.choices)
    text_payload = models.TextField(null=True, blank=True)  # noqa: DJ001
    json_payload = models.TextField(null=True, blank=True)  # noqa: DJ001
    byte_count = models.PositiveIntegerField()
    content_digest = models.BinaryField(max_length=32)
    correlation_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(payload_type="text", text_payload__isnull=False, json_payload__isnull=True)
                    | Q(
                        payload_type="json",
                        text_payload__isnull=True,
                        json_payload__isnull=False,
                    )
                ),
                name="message_has_one_payload_type",
            ),
            models.CheckConstraint(
                condition=Q(byte_count__lte=65_536), name="message_byte_count_at_most_64k"
            ),
            models.UniqueConstraint(
                fields=["cycle", "sequence"], name="unique_message_sequence_per_cycle"
            ),
            models.UniqueConstraint(
                fields=["cycle"], condition=Q(parent__isnull=True), name="one_root_per_cycle"
            ),
            models.CheckConstraint(
                condition=(Q(parent__isnull=True, depth=0) | Q(parent__isnull=False, depth__gt=0)),
                name="message_root_depth_matches",
            ),
            models.CheckConstraint(condition=Q(depth__lte=128), name="message_depth_at_most_128"),
        ]
        indexes = [
            models.Index(fields=["cycle", "parent", "sequence"], name="message_reply_order"),
            models.Index(fields=["tunnel", "created_at"], name="message_tunnel_activity"),
            GinIndex(
                SearchVector("text_payload", "json_payload", config="simple"),
                name="message_content_search",
            ),
        ]
        ordering = ["sequence", "id"]

    def __str__(self) -> str:
        return f"Message {self.sequence} in cycle {self.cycle_id}"


class MessageMention(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="mentions")
    credential = models.ForeignKey(
        AgentCredential, on_delete=models.PROTECT, related_name="message_mentions"
    )
    display_name = models.CharField(max_length=120)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "credential"], name="unique_mention_per_message"
            )
        ]

    def __str__(self) -> str:
        return f"Mention of {self.credential_id} in {self.message_id}"


class TunnelEvent(models.Model):
    class Type(models.TextChoices):
        MESSAGE_POSTED = "message_posted", "Message posted"
        CYCLE_CLOSED = "cycle_closed", "Cycle closed"
        TUNNEL_DORMANT = "tunnel_dormant", "Tunnel dormant"
        CYCLE_STARTED = "cycle_started", "Cycle started"
        ADDRESS_ROTATED = "address_rotated", "Address rotated"
        TUNNEL_RETIRED = "tunnel_retired", "Tunnel retired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(Tunnel, on_delete=models.CASCADE, related_name="events")
    position = models.PositiveBigIntegerField()
    event_type = models.CharField(max_length=24, choices=Type.choices)
    cycle = models.ForeignKey(
        Cycle, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    message = models.ForeignKey(
        Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tunnel", "position"], name="unique_event_position_per_tunnel"
            )
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"Event {self.position} for {self.tunnel_id}"


class ActivityCheckpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tunnel = models.ForeignKey(Tunnel, on_delete=models.CASCADE, related_name="checkpoints")
    credential = models.ForeignKey(
        AgentCredential, on_delete=models.CASCADE, related_name="activity_checkpoints"
    )
    last_event_position = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tunnel", "credential"], name="one_checkpoint_per_credential_tunnel"
            )
        ]

    def __str__(self) -> str:
        return f"Checkpoint {self.last_event_position} for {self.credential_id}"


class IdempotencyRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    credential = models.ForeignKey(
        AgentCredential, on_delete=models.CASCADE, related_name="idempotency_records"
    )
    operation = models.CharField(max_length=120)
    key_digest = models.BinaryField(max_length=32)
    request_digest = models.BinaryField(max_length=32)
    response = models.JSONField(default=dict)
    resource_id = models.UUIDField(null=True, blank=True)
    derivation_nonce = models.BinaryField(max_length=32, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
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
        AgentCredential, on_delete=models.SET_NULL, null=True, blank=True
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
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:
        return f"{self.action}:{self.id}"
