"""Closed history follows one simple deadline and deletion policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
import time_machine
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pydantic import TypeAdapter
from pytest_django.fixtures import Settings
from tests.factories import CredentialFactory

from accounts.admin_data import CycleResourceData
from accounts.admin_services import list_admin_resources
from agents.models import AgentCredential
from core.errors import CycleUnavailable, TunnelUnavailable
from core.models import AgentReceipt
from tunnels import admin_actions, cycles, messages
from tunnels.access import cycle_is_readable
from tunnels.cycles import CreatedTunnel, StartedCycle, close_cycle, create_tunnel
from tunnels.messages import PostedReply, post_reply, read_tree
from tunnels.models import Cycle, Message, TunnelEvent
from tunnels.retention import delete_due_cycle_content

pytestmark = pytest.mark.django_db


class RolloverCycleArguments(TypedDict):
    credential: AgentCredential
    idempotency_key: str
    address: str
    expected_address_generation: int
    root_content: Mapping[str, object]


def _conversation(
    credential_factory: CredentialFactory, suffix: str = "retention", *, closed: bool = False
) -> tuple[CreatedTunnel, PostedReply]:
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
    if closed:
        close_cycle(
            credential=credential,
            address=created.address,
            idempotency_key=f"close-{suffix}",
            expected_cycle_id=created.cycle.id,
        )
    return created, reply


def test_explicit_close_snapshots_current_retention(
    credential_factory: CredentialFactory, settings: Settings
) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 60
    created, _ = _conversation(credential_factory, "explicit")
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 300
    close_cycle(
        credential=created.tunnel.creator,
        idempotency_key="close-explicit",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.CLOSED
    assert created.cycle.retention_seconds == 300
    assert created.cycle.closed_at is not None
    assert created.cycle.delete_after == created.cycle.closed_at + timedelta(seconds=300)
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 600
    created.cycle.refresh_from_db()
    assert created.cycle.closed_at is not None
    assert created.cycle.delete_after == created.cycle.closed_at + timedelta(seconds=300)


def test_forever_close_keeps_history(
    credential_factory: CredentialFactory, settings: Settings
) -> None:
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


def test_active_cycle_deadline_is_exact_before_worker_closure(
    credential_factory: CredentialFactory,
) -> None:
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
    credential_factory: CredentialFactory,
) -> None:
    created, _ = _conversation(credential_factory, "boundary", closed=True)
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
    credential_factory: CredentialFactory,
) -> None:
    created, reply = _conversation(credential_factory, "delete", closed=True)
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now - timedelta(seconds=1),
        final_sequence=2,
    )
    AgentReceipt.objects.create(
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
    assert AgentReceipt.objects.filter(resource_id=reply.message.id).exists()
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)
    assert not delete_due_cycle_content(cycle_id=uuid4(), now=now)


def test_deletion_rejects_not_due_or_active_cycle(credential_factory: CredentialFactory) -> None:
    created, _ = _conversation(credential_factory, "not-due")
    now = timezone.now()
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)
    close_cycle(
        credential=created.tunnel.creator,
        address=created.address,
        idempotency_key="close-not-due",
        expected_cycle_id=created.cycle.id,
    )
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now,
        delete_after=now + timedelta(seconds=1),
        final_sequence=2,
    )
    assert not delete_due_cycle_content(cycle_id=created.cycle.id, now=now)


def test_expired_history_is_not_materialized_by_reads(
    credential_factory: CredentialFactory,
) -> None:
    created, _ = _conversation(credential_factory, "read", closed=True)
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now,
        final_sequence=2,
    )
    from core.errors import CycleUnavailable

    with pytest.raises(CycleUnavailable):
        read_tree(
            credential=created.tunnel.creator,
            address=created.address,
            cycle_id=created.cycle.id,
        )


@pytest.mark.parametrize("operation", ["create", "start", "rollover"])
def test_cycle_creation_replay_stops_at_history_deadline(
    credential_factory: CredentialFactory, settings: Settings, operation: str
) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 1
    credential, _ = credential_factory()

    def create() -> CreatedTunnel:
        return create_tunnel(
            credential=credential,
            idempotency_key="retention-retry-create",
            cycle_label="Temporary cycle",
            root_content={"type": "text", "text": "Root content."},
        )

    first = create()
    retry: Callable[[], CreatedTunnel | StartedCycle] = create
    if operation == "start":
        close_cycle(
            credential=credential,
            idempotency_key="retention-retry-close-first",
            address=first.address,
            expected_cycle_id=first.cycle.id,
        )

        def start() -> StartedCycle:
            return cycles.start_cycle(
                credential=credential,
                idempotency_key="retention-retry-start",
                address=first.address,
                expected_address_generation=1,
                cycle_label="Temporary cycle",
                root_content={"type": "text", "text": "Next root content."},
            )

        retry = start
    elif operation == "rollover":

        def rollover() -> StartedCycle:
            return cycles.rollover_cycle(
                credential=credential,
                idempotency_key="retention-retry-rollover",
                address=first.address,
                expected_cycle_id=first.cycle.id,
                expected_address_generation=1,
                cycle_label="Temporary cycle",
                root_content={"type": "text", "text": "Next root content."},
            )

        retry = rollover
    created = first if operation == "create" else retry()
    close_cycle(
        credential=credential,
        idempotency_key="retention-retry-close-result",
        address=first.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    deadline = created.cycle.delete_after
    assert deadline is not None
    count = Cycle.objects.count()
    with time_machine.travel(deadline - timedelta(microseconds=1), tick=False):
        replay = retry()
        assert replay.replay
        assert replay.cycle.id == created.cycle.id
        assert replay.cycle.label == "Temporary cycle"
        assert replay.message_count == 1
    with time_machine.travel(deadline, tick=False), pytest.raises(CycleUnavailable):
        retry()
    assert Cycle.objects.count() == count
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=deadline)
    with time_machine.travel(deadline, tick=False), pytest.raises(CycleUnavailable):
        retry()
    assert Cycle.objects.count() == count
    assert AgentReceipt.objects.filter(
        credential=credential,
        operation={"create": "create_tunnel", "start": "start_cycle", "rollover": "rollover_cycle"}[
            operation
        ],
    ).exists()


@pytest.mark.parametrize("action", ["close", "retire"])
@pytest.mark.parametrize("current_retention", [600, None])
def test_admin_actions_do_not_extend_expired_history(
    credential_factory: CredentialFactory,
    settings: Settings,
    action: str,
    current_retention: int | None,
) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 60
    created, _ = _conversation(credential_factory, f"expired-{action}")
    expiry = created.cycle.expires_at
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = current_retention
    actor = created.tunnel.creator.created_by
    assert actor is not None
    with time_machine.travel(expiry + timedelta(seconds=61), tick=False):
        if action == "close":
            admin_actions.close_cycle_as_admin(
                actor=actor, tunnel_id=created.tunnel.id, expected_cycle_id=created.cycle.id
            )
        else:
            admin_actions.retire_tunnel_as_admin(
                actor=actor,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
                confirmation=admin_actions.retirement_confirmation(created.tunnel),
            )
        created.cycle.refresh_from_db()
        assert created.cycle.close_reason == "expired"
        assert created.cycle.closed_at == expiry
        assert created.cycle.retention_seconds == 60
        assert created.cycle.delete_after == expiry + timedelta(seconds=60)
        assert not cycle_is_readable(created.cycle)


@pytest.mark.parametrize("action", ["rotate", "retire"])
def test_reply_replay_checks_current_tunnel_access(
    credential_factory: CredentialFactory, action: str
) -> None:
    created, _ = _conversation(credential_factory, f"reply-access-{action}")
    actor = created.tunnel.creator.created_by
    assert actor is not None
    if action == "rotate":
        admin_actions.rotate_address_as_admin(
            actor=actor, tunnel_id=created.tunnel.id, expected_address_generation=1
        )
    else:
        admin_actions.retire_tunnel_as_admin(
            actor=actor,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
            confirmation=admin_actions.retirement_confirmation(created.tunnel),
        )
    with pytest.raises(TunnelUnavailable):
        post_reply(
            credential=created.tunnel.creator,
            idempotency_key=f"reply-reply-access-{action}",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply content."},
        )


def test_reply_and_close_replay_stop_at_history_deadline(
    credential_factory: CredentialFactory, settings: Settings
) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 1
    created, _ = _conversation(credential_factory, "reply-history")

    def close() -> None:
        close_cycle(
            credential=created.tunnel.creator,
            idempotency_key="close-reply-history",
            address=created.address,
            expected_cycle_id=created.cycle.id,
        )

    close()
    created.cycle.refresh_from_db()
    with time_machine.travel(created.cycle.delete_after, tick=False):
        with pytest.raises(CycleUnavailable):
            post_reply(
                credential=created.tunnel.creator,
                idempotency_key="reply-reply-history",
                address=created.address,
                parent_id=created.root.id,
                content={"type": "text", "text": "Reply content."},
            )
        with pytest.raises(CycleUnavailable):
            close()


@pytest.mark.parametrize("operation", ["start", "rollover", "close"])
@pytest.mark.parametrize("access_change", ["rotate", "retire"])
def test_cycle_control_replays_check_current_address(
    credential_factory: CredentialFactory, operation: str, access_change: str
) -> None:
    created, _ = _conversation(credential_factory, f"control-{operation}-{access_change}")
    credential = created.tunnel.creator
    actor = credential.created_by
    assert actor is not None
    if operation == "start":
        close_cycle(
            credential=credential,
            idempotency_key="close-before-start-replay",
            address=created.address,
            expected_cycle_id=created.cycle.id,
        )

    def retry() -> StartedCycle | None:
        if operation == "close":
            return close_cycle(
                credential=credential,
                idempotency_key="control-access-close",
                address=created.address,
                expected_cycle_id=created.cycle.id,
            )
        common: RolloverCycleArguments = {
            "credential": credential,
            "idempotency_key": "control-access-create-cycle",
            "address": created.address,
            "expected_address_generation": 1,
            "root_content": {"type": "text", "text": "Next root."},
        }
        if operation == "start":
            return cycles.start_cycle(**common)
        return cycles.rollover_cycle(**common, expected_cycle_id=created.cycle.id)

    retry()
    retry()
    if access_change == "rotate":
        admin_actions.rotate_address_as_admin(
            actor=actor, tunnel_id=created.tunnel.id, expected_address_generation=1
        )
    else:
        admin_actions.retire_tunnel_as_admin(
            actor=actor,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
            confirmation=admin_actions.retirement_confirmation(created.tunnel),
        )
    count = Cycle.objects.count()
    with pytest.raises(TunnelUnavailable):
        retry()
    assert Cycle.objects.count() == count


@pytest.mark.parametrize("state", [Cycle.State.ACTIVE, Cycle.State.CLOSED])
def test_admin_cycle_labels_stop_at_history_deadline_without_extra_queries(
    credential_factory: CredentialFactory, state: str
) -> None:
    created, _ = _conversation(
        credential_factory, "admin-labels-first", closed=state == Cycle.State.CLOSED
    )
    actor = created.tunnel.creator.created_by
    assert actor is not None
    deadline = timezone.now() + timedelta(seconds=1)
    Cycle.objects.filter(pk=created.cycle.id).update(
        state=state,
        expires_at=deadline - timedelta(seconds=60),
        retention_seconds=60,
        closed_at=deadline - timedelta(seconds=60) if state == Cycle.State.CLOSED else None,
        delete_after=deadline if state == Cycle.State.CLOSED else None,
        final_sequence=2 if state == Cycle.State.CLOSED else None,
    )
    with time_machine.travel(deadline - timedelta(microseconds=1), tick=False):
        with CaptureQueriesContext(connection) as one_cycle_queries:
            before = list_admin_resources("cycles", actor=actor)
        before_items = TypeAdapter(list[CycleResourceData]).validate_python(
            before["items"], strict=True
        )
        assert before_items[0]["label"] == "Temporary cycle"
    second, _ = _conversation(credential_factory, "admin-labels-second")
    with (
        time_machine.travel(deadline, tick=False),
        CaptureQueriesContext(connection) as two_cycle_queries,
    ):
        after = list_admin_resources("cycles", actor=actor)
    assert len(two_cycle_queries) == len(one_cycle_queries)
    rows = {
        item["id"]: item
        for item in TypeAdapter(list[CycleResourceData]).validate_python(
            after["items"], strict=True
        )
    }
    assert rows[str(created.cycle.id)]["label"] == ""
    assert rows[str(created.cycle.id)]["state"] == state
    assert rows[str(created.cycle.id)]["number"] == 1
    assert rows[str(second.cycle.id)]["label"] == "Temporary cycle"


def test_branch_assembly_handles_history_deleted_after_focus_read(
    credential_factory: CredentialFactory,
) -> None:
    created, reply = _conversation(credential_factory, "read-deletion")
    credential = created.tunnel.creator
    close_cycle(
        credential=credential,
        idempotency_key="close-read-deletion",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    focus = messages.get_message(
        credential=credential,
        address=created.address,
        message_id=reply.message.id,
        cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    deadline = created.cycle.delete_after
    assert deadline is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=deadline)
    with pytest.raises(CycleUnavailable):
        messages.load_branch(focus)


def test_subtree_hydration_rejects_missing_selected_messages(
    credential_factory: CredentialFactory,
) -> None:
    created, reply = _conversation(credential_factory, "subtree-deletion")
    selected_ids = [created.root.id, reply.message.id]
    close_cycle(
        credential=created.tunnel.creator,
        idempotency_key="close-subtree-deletion",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    deadline = created.cycle.delete_after
    assert deadline is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=deadline)
    with pytest.raises(CycleUnavailable):
        messages._messages_by_id_in_order(selected_ids)


def test_message_hydration_does_not_return_a_partial_page(
    credential_factory: CredentialFactory,
) -> None:
    created, _ = _conversation(credential_factory, "partial-page")
    with pytest.raises(CycleUnavailable):
        messages._messages_by_id_in_order([created.root.id, uuid4()])


def test_branch_assembly_handles_a_deleted_uncached_ancestor(
    credential_factory: CredentialFactory,
) -> None:
    created, reply = _conversation(credential_factory, "uncached-ancestor")
    credential = created.tunnel.creator
    descendant = post_reply(
        credential=credential,
        idempotency_key="descendant-uncached-ancestor",
        address=created.address,
        parent_id=reply.message.id,
        content={"type": "text", "text": "Descendant content."},
    )
    focus = messages.get_message(
        credential=credential,
        address=created.address,
        message_id=descendant.message.id,
        cycle_id=created.cycle.id,
    )
    close_cycle(
        credential=credential,
        idempotency_key="close-uncached-ancestor",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    deadline = created.cycle.delete_after
    assert deadline is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=deadline)
    with pytest.raises(CycleUnavailable):
        messages.load_branch(focus)
