"""PostgreSQL proves Beacon publication serializes with other writes."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection

from agents.models import AgentCredential
from tunnels.beacons import beacon_data, beacon_queryset, publish_beacon, withdraw_beacon
from tunnels.errors import CycleClosed, LifecycleConflict
from tunnels.models import Beacon, BoardState, TunnelEvent
from tunnels.services import close_cycle, create_tunnel, post_reply

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL Beacon tests.")


def _credential(credential_id: UUID) -> AgentCredential:
    return AgentCredential.objects.get(pk=credential_id)


def test_concurrent_publications_leave_one_final_beacon(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Concurrent Beacon publisher")
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-beacon-create-0001",
        root_content={"type": "text", "text": "Review the proposal."},
    )
    barrier = threading.Barrier(2)

    def attempt(index: int) -> str:
        close_old_connections()
        try:
            credential = _credential(creator.id)
            barrier.wait(timeout=15)
            try:
                publish_beacon(
                    credential=credential,
                    idempotency_key=f"pg-beacon-publish-{index:04d}",
                    address=created.address,
                    text=f"Review request {index}.",
                )
            except LifecycleConflict:
                return "conflict"
            return "published"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))

    assert sorted(outcomes) == ["conflict", "published"]
    assert Beacon.objects.filter(cycle=created.cycle).count() == 1
    assert BoardState.objects.get(pk=1).position == 1
    events = list(
        TunnelEvent.objects.filter(tunnel=created.tunnel, event_type=TunnelEvent.Type.BEACON_LIT)
    )
    assert len(events) == 1


def test_publication_racing_a_reply_has_a_serialized_response_boundary(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Reply race publisher")
    responder, _ = credential_factory(name="Concurrent responder")
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-beacon-reply-create",
        root_content={"type": "text", "text": "Review the proposal."},
    )
    barrier = threading.Barrier(2)

    def publish() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            publish_beacon(
                credential=_credential(creator.id),
                idempotency_key="pg-beacon-reply-publish",
                address=created.address,
                text="Please review.",
            )
            return "published"
        finally:
            close_old_connections()

    def reply() -> str:
        close_old_connections()
        try:
            credential = _credential(responder.id)
            barrier.wait(timeout=15)
            post_reply(
                credential=credential,
                idempotency_key="pg-beacon-reply-write",
                address=created.address,
                parent_id=created.root.id,
                content={"type": "text", "text": "I can review."},
            )
            return "replied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        publish_future = executor.submit(publish)
        reply_future = executor.submit(reply)
        assert {publish_future.result(), reply_future.result()} == {"published", "replied"}

    beacon = Beacon.objects.get(cycle=created.cycle)
    reply_message = created.cycle.messages.get(parent__isnull=False)
    response_count = beacon_data(beacon_queryset().get(pk=beacon.pk))["response_count"]
    assert response_count == int(reply_message.sequence > beacon.publication_sequence)


def test_cycle_close_racing_withdraw_ends_beacon_once(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="End race publisher")
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-beacon-end-create",
        root_content={"type": "text", "text": "Review the proposal."},
    )
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="pg-beacon-end-publish",
        address=created.address,
        text="Please review.",
    )
    barrier = threading.Barrier(2)

    def close() -> str:
        close_old_connections()
        try:
            credential = _credential(creator.id)
            barrier.wait(timeout=15)
            close_cycle(
                credential=credential,
                idempotency_key="pg-beacon-end-close",
                address=created.address,
                expected_cycle_id=created.cycle.id,
            )
            return "closed"
        finally:
            close_old_connections()

    def withdraw() -> str:
        close_old_connections()
        try:
            credential = _credential(creator.id)
            barrier.wait(timeout=15)
            try:
                withdraw_beacon(
                    credential=credential,
                    idempotency_key="pg-beacon-end-withdraw",
                    address=created.address,
                    beacon_id=UUID(result["id"]),
                )
            except CycleClosed, LifecycleConflict:
                return "already closed"
            return "withdrawn"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda operation: operation(), (close, withdraw)))

    assert "closed" in outcomes
    beacon = Beacon.objects.get(pk=result["id"])
    assert beacon.ended_at is not None
    assert beacon.end_reason in {Beacon.EndReason.WITHDRAWN, Beacon.EndReason.CYCLE_CLOSED}
    assert (
        TunnelEvent.objects.filter(
            tunnel=created.tunnel, event_type=TunnelEvent.Type.BEACON_ENDED
        ).count()
        == 1
    )
    events = list(TunnelEvent.objects.filter(tunnel=created.tunnel).order_by("position"))
    assert [event.position for event in events] == list(range(1, len(events) + 1))
