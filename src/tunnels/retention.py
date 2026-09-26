"""Physical deletion for closed cycle content."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from django.db import transaction

from .models import Cycle, IdempotencyRecord, Message, Tunnel


@transaction.atomic
def delete_due_cycle_content(*, cycle_id: UUID, now: datetime) -> bool:
    """Delete one cycle's content and keep a minimal lifecycle tombstone."""

    candidate = Cycle.objects.only("tunnel_id").filter(pk=cycle_id).first()
    if candidate is None:
        return False
    Tunnel.objects.select_for_update().get(pk=candidate.tunnel_id)
    cycle = Cycle.objects.select_for_update().get(pk=cycle_id)
    if cycle.state == Cycle.State.DELETED:
        return False
    if cycle.state != Cycle.State.CLOSED or cycle.delete_after is None or cycle.delete_after > now:
        return False

    message_ids = list(Message.objects.filter(cycle=cycle).values_list("id", flat=True))
    cycle.state = Cycle.State.DELETED
    cycle.root_message = None
    cycle.label = ""
    cycle.message_count = 0
    cycle.content_bytes = 0
    cycle.save(
        update_fields=[
            "state",
            "root_message",
            "label",
            "message_count",
            "content_bytes",
        ]
    )
    IdempotencyRecord.objects.filter(resource_id__in=[cycle.id, *message_ids]).delete()
    Message.objects.filter(cycle=cycle).delete()
    return True
