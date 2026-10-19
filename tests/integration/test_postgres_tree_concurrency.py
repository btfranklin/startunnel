"""Real PostgreSQL races prove the first immutable-tree transaction boundary."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from agents.models import AgentCredential
from core import limits as instance_limits
from core.errors import InvalidCredential, InvalidRequest, QuotaExceeded
from core.limits import Limits
from tunnels.admin_actions import rotate_address_as_admin
from tunnels.codec import parse_address
from tunnels.context import get_context
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import PostedReply, get_subtree, list_leaves, list_replies, post_reply
from tunnels.models import Cycle, Message, Tunnel, TunnelAddress, TunnelEvent

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def _post_in_thread(
    *,
    credential_id: UUID,
    address: str,
    parent_id: UUID,
    idempotency_key: str,
    barrier: threading.Barrier,
    content: str,
) -> PostedReply:
    close_old_connections()
    try:
        credential = AgentCredential.objects.get(pk=credential_id)
        barrier.wait(timeout=15)
        return post_reply(
            credential=credential,
            idempotency_key=idempotency_key,
            address=address,
            parent_id=parent_id,
            content={"type": "text", "text": content},
        )
    finally:
        close_old_connections()


def test_fifty_replies_to_one_parent_get_unique_order_and_exact_counters(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Tree creator")
    workers = [credential_factory(name=f"Tree worker {index}")[0] for index in range(50)]
    created = create_tunnel(
        credential=creator,
        idempotency_key="tree-fifty-create",
        root_content={"type": "text", "text": "Investigate the release."},
    )
    barrier = threading.Barrier(len(workers))

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        results = list(
            executor.map(
                lambda item: _post_in_thread(
                    credential_id=item[1].id,
                    address=created.address,
                    parent_id=created.root.id,
                    idempotency_key=f"tree-fifty-reply-{item[0]}",
                    barrier=barrier,
                    content=f"Finding {item[0]}",
                ),
                enumerate(workers),
            )
        )

    messages = list(Message.objects.filter(cycle=created.cycle).order_by("sequence"))
    reply_events = list(
        TunnelEvent.objects.filter(
            tunnel=created.tunnel,
            event_type=TunnelEvent.Type.MESSAGE_POSTED,
            message__parent=created.root,
        ).order_by("position")
    )
    created.cycle.refresh_from_db()
    created.tunnel.refresh_from_db()

    assert len(results) == 50
    assert {result.message.sequence for result in results} == set(range(2, 52))
    assert {result.event_position for result in results} == set(range(3, 53))
    assert [message.sequence for message in messages] == list(range(1, 52))
    assert all(message.parent_id == created.root.id for message in messages[1:])
    assert all(message.depth == 1 for message in messages[1:])
    assert [event.position for event in reply_events] == list(range(3, 53))
    assert created.cycle.next_sequence == 52
    assert created.cycle.message_count == 51
    assert created.cycle.content_bytes == sum(message.byte_count for message in messages)
    assert created.tunnel.next_event_position == 53


def test_maximum_cycle_traversal_and_context_materialize_only_requested_pages(
    credential_factory: Any,
    django_assert_num_queries: Any,
) -> None:
    credential, _ = credential_factory(name="Maximum cycle reader")
    created = create_tunnel(
        credential=credential,
        idempotency_key="maximum-cycle-read-create",
        root_content={"type": "text", "text": "Coordinate a maximum-size cycle."},
    )
    payload = "x"
    message_total = Limits().messages_per_cycle
    reply_rows = [
        Message(
            id=uuid4(),
            tunnel=created.tunnel,
            cycle=created.cycle,
            parent=created.root,
            sender=credential,
            sender_name=credential.name,
            sequence=sequence,
            depth=1,
            payload_type=Message.PayloadType.TEXT,
            text_payload=payload,
            byte_count=1,
        )
        for sequence in range(2, message_total + 1)
    ]
    Message.objects.bulk_create(reply_rows, batch_size=500)
    Tunnel.objects.filter(pk=created.tunnel.id).update(
        next_event_position=message_total + 2,
    )
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=created.tunnel,
                cycle=created.cycle,
                message=message,
                position=message.sequence + 1,
                event_type=TunnelEvent.Type.MESSAGE_POSTED,
            )
            for message in reply_rows
        ],
        batch_size=500,
    )
    Cycle.objects.filter(pk=created.cycle.id).update(
        next_sequence=message_total + 1,
        message_count=message_total,
        content_bytes=created.root.byte_count + message_total - 1,
    )
    created.cycle.refresh_from_db()
    created.tunnel.refresh_from_db()

    with django_assert_num_queries(8, exact=False):
        subtree = get_subtree(
            credential=credential,
            address=created.address,
            message_id=created.root.id,
            limit=3,
        )
    reply_page = list_replies(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        limit=3,
    )
    leaves = list_leaves(
        credential=credential,
        address=created.address,
        limit=3,
    )

    assert [message.sequence for message in subtree.messages] == [1, 2, 3]
    assert subtree.truncated is True
    assert [message.sequence for message in reply_page.messages] == [2, 3, 4]
    assert reply_page.complete is False
    assert [message.sequence for message in leaves.messages] == [2, 3, 4]
    assert leaves.complete is False

    with CaptureQueriesContext(connection) as direct_queries:
        direct_context = get_context(
            credential=credential,
            address=created.address,
            focus_message_id=created.root.id,
            include_recent_activity=False,
            max_items=2,
        )
    with CaptureQueriesContext(connection) as activity_queries:
        activity_context = get_context(
            credential=credential,
            address=created.address,
            focus_message_id=created.root.id,
            include_direct_replies=False,
            include_recent_activity=True,
            after_activity_position=0,
            max_items=2,
        )

    assert [item.message.sequence for item in direct_context.items] == [1, 2]
    assert direct_context.truncated is True
    assert [item.message.sequence for item in activity_context.items] == [1, 2]
    assert activity_context.truncated is True
    assert any(
        'FROM "tunnels_message"' in query["sql"] and "LIMIT 2" in query["sql"]
        for query in direct_queries.captured_queries
    )
    assert any(
        'FROM "tunnels_tunnelevent"' in query["sql"] and "LIMIT 2" in query["sql"]
        for query in activity_queries.captured_queries
    )


def test_address_rotation_retries_one_database_collision(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential, _ = credential_factory(name="Rotation collision owner")
    created = create_tunnel(
        credential=credential,
        idempotency_key="rotation-collision-create",
        root_content={"type": "text", "text": "Rotate after one collision."},
    )
    generated_tokens = iter((parse_address(created.address), b"\xff" * 16))
    monkeypatch.setattr("tunnels.cycles.secrets.token_bytes", lambda size: next(generated_tokens))

    rotated = rotate_address_as_admin(
        actor=credential.created_by,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )

    assert rotated.address != created.address
    assert rotated.generation == 2
    addresses = list(TunnelAddress.objects.filter(tunnel=created.tunnel).order_by("generation"))
    assert [address.state for address in addresses] == ["retired", "current"]
    assert [row.address for row in addresses] == [created.address, rotated.address]


def test_tunnel_create_retries_collision_and_rolls_back_when_exhausted(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential, _ = credential_factory(name="Create collision owner")
    first = create_tunnel(
        credential=credential,
        idempotency_key="create-collision-seed",
        root_content={"type": "text", "text": "Seed address."},
    )
    colliding_token = parse_address(first.address)
    fresh_token = b"\xfe" * 16
    generated_tokens = iter((colliding_token, fresh_token))
    monkeypatch.setattr("tunnels.cycles.secrets.token_bytes", lambda size: next(generated_tokens))
    second = create_tunnel(
        credential=credential,
        idempotency_key="create-collision-retry",
        root_content={"type": "text", "text": "Retry address."},
    )
    assert second.address != first.address
    assert second.tunnel.addresses.get().address == second.address

    monkeypatch.setattr("tunnels.cycles.secrets.token_bytes", lambda size: colliding_token)
    with pytest.raises(InvalidRequest, match="could not allocate"):
        create_tunnel(
            credential=credential,
            idempotency_key="create-collision-exhausted",
            root_content={"type": "text", "text": "No available address."},
        )
    assert Tunnel.objects.count() == 2
    assert credential.agent_receipts.count() == 2


def test_concurrent_exact_idempotency_retry_creates_one_reply(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Retry creator")
    worker, _ = credential_factory(name="Retry worker")
    created = create_tunnel(
        credential=creator,
        idempotency_key="tree-concurrent-retry-create",
        root_content={"type": "text", "text": "Store one exact result."},
    )
    barrier = threading.Barrier(2)

    def attempt() -> PostedReply:
        return _post_in_thread(
            credential_id=worker.id,
            address=created.address,
            parent_id=created.root.id,
            idempotency_key="tree-concurrent-exact-retry",
            barrier=barrier,
            content="This exact request can race safely.",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=30) for future in [executor.submit(attempt) for _ in range(2)]
        ]

    assert results[0].message.id == results[1].message.id
    assert sorted(result.replay for result in results) == [False, True]
    assert Message.objects.filter(cycle=created.cycle, parent=created.root).count() == 1
    assert (
        TunnelEvent.objects.filter(
            tunnel=created.tunnel,
            event_type=TunnelEvent.Type.MESSAGE_POSTED,
            message__parent=created.root,
        ).count()
        == 1
    )


def test_concurrent_posts_cannot_cross_the_message_limit(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = replace(Limits(), messages_per_cycle=2)
    monkeypatch.setattr(instance_limits, "effective_limits", lambda: limits)
    creator, _ = credential_factory(name="Quota creator")
    workers = [credential_factory(name=f"Quota worker {index}")[0] for index in range(10)]
    created = create_tunnel(
        credential=creator,
        idempotency_key="tree-quota-create",
        root_content={"type": "text", "text": "Only one reply can fit."},
    )
    barrier = threading.Barrier(len(workers))

    def attempt(item: tuple[int, AgentCredential]) -> str:
        try:
            _post_in_thread(
                credential_id=item[1].id,
                address=created.address,
                parent_id=created.root.id,
                idempotency_key=f"tree-quota-reply-{item[0]}",
                barrier=barrier,
                content=f"Quota reply {item[0]}",
            )
        except QuotaExceeded:
            return "limited"
        return "posted"

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        outcomes = list(executor.map(attempt, enumerate(workers)))

    cycle = Cycle.objects.get(pk=created.cycle.id)
    messages = list(Message.objects.filter(cycle=cycle))
    reply_events = TunnelEvent.objects.filter(
        tunnel=created.tunnel,
        event_type=TunnelEvent.Type.MESSAGE_POSTED,
        message__parent=created.root,
    )

    assert outcomes.count("posted") == 1
    assert outcomes.count("limited") == 9
    assert len(messages) == 2
    assert cycle.message_count == 2
    assert cycle.next_sequence == 3
    assert cycle.content_bytes == sum(message.byte_count for message in messages)
    assert reply_events.count() == 1


def test_postgresql_rejects_cross_cycle_parent_and_message_update(
    credential_factory: Any,
) -> None:
    first_creator, _ = credential_factory(name="First guard creator")
    second_creator, _ = credential_factory(name="Second guard creator")
    first = create_tunnel(
        credential=first_creator,
        idempotency_key="tree-guard-first",
        root_content={"type": "text", "text": "First root"},
    )
    second = create_tunnel(
        credential=second_creator,
        idempotency_key="tree-guard-second",
        root_content={"type": "text", "text": "Second root"},
    )

    with pytest.raises(DatabaseError, match="same tunnel and cycle"), transaction.atomic():
        Message.objects.create(
            tunnel=second.tunnel,
            cycle=second.cycle,
            parent=first.root,
            sender=second_creator,
            sender_name=second_creator.name,
            sequence=2,
            depth=1,
            payload_type=Message.PayloadType.TEXT,
            text_payload="Invalid cross-cycle reply",
            byte_count=len(b"Invalid cross-cycle reply"),
        )

    with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
        Message.objects.filter(pk=first.root.id).update(sender_name="Changed after commit")

    with pytest.raises(DatabaseError, match="immutable"), transaction.atomic():
        Message.objects.filter(pk=first.root.id).update(session="new-label")

    with pytest.raises(DatabaseError, match="identity is immutable"), transaction.atomic():
        TunnelAddress.objects.filter(tunnel=first.tunnel).update(address=second.address)

    assert TunnelAddress.objects.get(tunnel=first.tunnel).address == first.address
    first.root.refresh_from_db()
    assert first.root.sender_name == first_creator.name
    assert first.root.session is None
    assert Message.objects.filter(cycle=second.cycle).count() == 1


@pytest.mark.parametrize("after_sequence", [0, 1])
def test_subtree_rejects_root_deleted_before_recursive_read(
    credential_factory: Any, after_sequence: int
) -> None:
    from core.errors import CycleUnavailable
    from tunnels.messages import _postgresql_subtree_page
    from tunnels.retention import delete_due_cycle_content

    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="pg-subtree-deleted-root",
        root_content={"type": "text", "text": "Temporary root."},
    )
    close_cycle(
        credential=credential,
        idempotency_key="pg-subtree-close-deleted-root",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    deadline = created.cycle.delete_after
    assert deadline is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=deadline)
    with pytest.raises(CycleUnavailable):
        _postgresql_subtree_page(
            cycle=created.cycle,
            root=created.root,
            max_depth=8,
            limit=100,
            after_sequence=after_sequence,
            high_water=1,
        )


def test_context_checks_revocation_before_returning_assembled_messages(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunnels.context import _wire_bytes

    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="context-revocation-create",
        root_content={"type": "text", "text": "Context root."},
    )

    def revoke_during_assembly(message: Message, reason: Any) -> int:
        AgentCredential.objects.filter(pk=credential.pk).update(revoked_at=timezone.now())
        return _wire_bytes(message, reason)

    monkeypatch.setattr("tunnels.context._wire_bytes", revoke_during_assembly)
    with pytest.raises(InvalidCredential):
        get_context(
            credential=credential,
            address=created.address,
            focus_message_id=created.root.id,
            include_direct_replies=False,
            include_recent_activity=False,
        )
