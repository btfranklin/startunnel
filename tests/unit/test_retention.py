"""Closed history follows one simple deadline and deletion policy."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from tunnels.access import cycle_is_readable
from tunnels.models import Cycle, IdempotencyRecord, Message, TunnelEvent
from tunnels.retention import delete_due_cycle_content
from tunnels.services import close_cycle, create_tunnel, post_reply, read_tree

pytestmark = pytest.mark.django_db


def _conversation(credential_factory: Any, suffix: str = "retention") -> tuple[Any, Any]:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key=f"create-{suffix}",
        label="Temporary label",
        cycle_label="Temporary cycle",
        root_content={"type": "text", "text": "Root content."},
    )
    reply = post_reply(
        credential=credential,
        idempotency_key=f"reply-{suffix}",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Reply content."},
    )
    return created, reply


def test_explicit_close_snapshots_current_retention(credential_factory: Any, settings: Any) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 300
    created, _ = _conversation(credential_factory, "explicit")
    close_cycle(
        credential=created.tunnel.creator,
        idempotency_key="close-explicit",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.CLOSED
    assert created.cycle.retention_seconds == 300
    assert created.cycle.delete_after == created.cycle.closed_at + timedelta(seconds=300)
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 600
    created.cycle.refresh_from_db()
    assert created.cycle.delete_after == created.cycle.closed_at + timedelta(seconds=300)


def test_forever_close_keeps_history(credential_factory: Any, settings: Any) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = None
    created, _ = _conversation(credential_factory, "forever")
    close_cycle(
        credential=created.tunnel.creator,
        idempotency_key="close-forever",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    assert created.cycle.delete_after is None
    assert cycle_is_readable(created.cycle, now=timezone.now() + timedelta(days=3650))


def test_active_cycle_deadline_is_exact_before_worker_closure(credential_factory: Any) -> None:
    created, _ = _conversation(credential_factory, "logical")
    expiry = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=expiry, retention_seconds=60)
    created.cycle.refresh_from_db()
    assert cycle_is_readable(created.cycle, now=expiry + timedelta(seconds=59))
    assert not cycle_is_readable(created.cycle, now=expiry + timedelta(seconds=60))
    Cycle.objects.filter(pk=created.cycle.pk).update(retention_seconds=None)
    created.cycle.refresh_from_db()
    assert cycle_is_readable(created.cycle, now=expiry + timedelta(days=365))


def test_closed_cycle_becomes_unreadable_at_exact_delete_deadline(
    credential_factory: Any,
) -> None:
    created, _ = _conversation(credential_factory, "boundary")
    deadline = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=deadline - timedelta(seconds=60),
        delete_after=deadline,
        final_sequence=2,
    )
    created.cycle.refresh_from_db()
    assert cycle_is_readable(created.cycle, now=deadline - timedelta(microseconds=1))
    assert not cycle_is_readable(created.cycle, now=deadline)


def test_due_deletion_keeps_minimal_tombstone_and_is_idempotent(
    credential_factory: Any,
) -> None:
    created, reply = _conversation(credential_factory, "delete")
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now - timedelta(seconds=1),
        final_sequence=2,
    )
    IdempotencyRecord.objects.create(
        credential=created.tunnel.creator,
        operation="test-delete",
        key_digest=b"k" * 32,
        request_digest=b"r" * 32,
        resource_id=reply.message.id,
        expires_at=now + timedelta(hours=1),
    )
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=now)
    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.DELETED
    assert created.cycle.root_message is None
    assert created.cycle.label == ""
    assert created.cycle.message_count == 0 and created.cycle.content_bytes == 0
    assert not Message.objects.filter(cycle=created.cycle).exists()
    retained_events = TunnelEvent.objects.filter(cycle=created.cycle)
    assert retained_events.exists()
    assert not retained_events.exclude(message__isnull=True).exists()
    assert not IdempotencyRecord.objects.filter(resource_id=reply.message.id).exists()
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)
    assert not delete_due_cycle_content(cycle_id=uuid4(), now=now)


def test_deletion_rejects_not_due_or_active_cycle(credential_factory: Any) -> None:
    created, _ = _conversation(credential_factory, "not-due")
    now = timezone.now()
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now,
        delete_after=now + timedelta(seconds=1),
        final_sequence=2,
    )
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)


def test_expired_history_is_not_materialized_by_reads(credential_factory: Any) -> None:
    created, _ = _conversation(credential_factory, "read")
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now,
        final_sequence=2,
    )
    from tunnels.errors import CycleUnavailable

    with pytest.raises(CycleUnavailable):
        read_tree(
            credential=created.tunnel.creator,
            address=created.address,
            cycle_id=created.cycle.id,
        )
