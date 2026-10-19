"""PostgreSQL deletes branched history without restoring it through retries."""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from core.errors import CycleUnavailable, TunnelUnavailable
from core.models import AgentReceipt
from tunnels.activity import commit_checkpoint
from tunnels.beacons import beacon_data, beacon_queryset, publish_beacon
from tunnels.cycles import close_cycle, create_tunnel, start_cycle
from tunnels.messages import post_reply, read_tree
from tunnels.models import Beacon, Cycle, Message, MessageMention, TunnelEvent
from tunnels.retention import delete_due_cycle_content

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_retention_deletes_branched_content_and_preserves_retry_boundaries(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Retention publisher")
    responder, _ = credential_factory(name="Retention responder")
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-retention-create",
        root_content={"type": "text", "text": "Earlier cycle."},
    )
    close_cycle(
        credential=creator,
        idempotency_key="pg-retention-close-first",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    start_arguments = {
        "credential": creator,
        "idempotency_key": "pg-retention-start",
        "address": created.address,
        "expected_address_generation": 1,
        "cycle_label": "Temporary cycle",
        "root_content": {"type": "text", "text": "Temporary root."},
        "root_mentions": [responder.id],
    }
    started = start_cycle(**start_arguments)
    beacon_arguments = {
        "credential": creator,
        "idempotency_key": "pg-retention-publish",
        "address": created.address,
        "text": "Temporary discovery text.",
    }
    beacon, replay = publish_beacon(**beacon_arguments)
    assert not replay
    reply_arguments = {
        "credential": responder,
        "idempotency_key": "pg-retention-reply",
        "address": created.address,
        "parent_id": started.root.id,
        "content": {"type": "text", "text": "Temporary reply."},
        "mentions": [creator.id],
    }
    reply = post_reply(**reply_arguments)
    child = post_reply(
        credential=creator,
        idempotency_key="pg-retention-child",
        address=created.address,
        parent_id=reply.message.id,
        content={"type": "json", "value": {"temporary": True}},
    )
    sibling = post_reply(
        credential=responder,
        idempotency_key="pg-retention-sibling",
        address=created.address,
        parent_id=started.root.id,
        content={"type": "text", "text": "Temporary sibling."},
    )
    message_ids = [started.root.id, reply.message.id, child.message.id, sibling.message.id]
    assert list(Message.objects.filter(cycle=started.cycle).values_list("depth", flat=True)) == [
        0,
        1,
        2,
        1,
    ]
    assert MessageMention.objects.filter(message_id__in=message_ids).count() == 2
    assert beacon_data(beacon_queryset().get(pk=beacon["id"]))["response_count"] == 1
    checkpoint, advanced = commit_checkpoint(
        credential=responder,
        address=created.address,
        position=sibling.event_position,
    )
    assert advanced
    close_cycle(
        credential=creator,
        idempotency_key="pg-retention-close-second",
        address=created.address,
        expected_cycle_id=started.cycle.id,
    )
    now = timezone.now()
    Cycle.objects.filter(pk=started.cycle.pk).update(delete_after=now)
    started.cycle.refresh_from_db()
    lifecycle = (
        started.cycle.created_at,
        started.cycle.closed_at,
        started.cycle.delete_after,
        started.cycle.close_reason,
        started.cycle.final_sequence,
    )
    events = TunnelEvent.objects.filter(cycle=started.cycle)
    event_spine = list(events.values("id", "position", "event_type", "metadata"))
    receipts = AgentReceipt.objects.filter(credential__in=[creator, responder])
    receipt_ids = set(receipts.values_list("id", flat=True))
    assert receipts.filter(resource_id=started.cycle.id, expires_at__gt=now).exists()
    assert receipts.filter(resource_id=beacon["id"], expires_at__gt=now).exists()

    assert delete_due_cycle_content(cycle_id=started.cycle.id, now=now)
    started.cycle.refresh_from_db()
    assert started.cycle.state == Cycle.State.DELETED
    assert started.cycle.root_message_id is None
    assert started.cycle.label == ""
    assert started.cycle.message_count == started.cycle.content_bytes == 0
    assert (
        started.cycle.created_at,
        started.cycle.closed_at,
        started.cycle.delete_after,
        started.cycle.close_reason,
        started.cycle.final_sequence,
    ) == lifecycle
    assert not Message.objects.filter(cycle=started.cycle).exists()
    assert not MessageMention.objects.filter(message_id__in=message_ids).exists()
    assert not Beacon.objects.filter(cycle=started.cycle).exists()
    assert list(events.values("id", "position", "event_type", "metadata")) == event_spine
    assert not events.filter(message__isnull=False).exists()
    assert all(set(event["metadata"]) <= {"beacon_id", "reason"} for event in event_spine)
    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == sibling.event_position
    assert set(receipts.values_list("id", flat=True)) == receipt_ids
    assert receipts.get(resource_id=beacon["id"]).response == {}
    assert Message.objects.filter(pk=created.root.id).exists()

    cycle_count = Cycle.objects.filter(tunnel=created.tunnel).count()
    with pytest.raises(CycleUnavailable):
        start_cycle(**start_arguments)
    with pytest.raises(TunnelUnavailable):
        post_reply(**reply_arguments)
    with pytest.raises(TunnelUnavailable):
        publish_beacon(**beacon_arguments)
    with pytest.raises(CycleUnavailable):
        read_tree(credential=creator, address=created.address, cycle_id=started.cycle.id)
    assert Cycle.objects.filter(tunnel=created.tunnel).count() == cycle_count
    assert not delete_due_cycle_content(cycle_id=started.cycle.id, now=now)
