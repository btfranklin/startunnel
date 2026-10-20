"""PostgreSQL proves Beacon publication serializes with other writes."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from agents.models import AgentCredential
from core import maintenance
from core.errors import CycleClosed, LifecycleConflict
from core.models import AuditEvent
from tunnels.beacons import (
    beacon_data,
    beacon_queryset,
    publish_beacon,
    withdraw_beacon,
    withdraw_beacon_as_admin,
)
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Beacon, BoardState, TunnelEvent

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def _credential(credential_id: UUID) -> AgentCredential:
    return AgentCredential.objects.get(pk=credential_id)


def _wait_for_production_tunnel_lock(*, waiter: int, owner: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type = 'Lock' AND %s = ANY(pg_blocking_pids(pid)) "
                "FROM pg_stat_activity WHERE pid = %s",
                [owner, waiter],
            )
            row = cursor.fetchone()
        if row and row[0]:
            return
        time.sleep(0.01)
    pytest.fail("The second operation did not wait for the first production tunnel lock.")


@pytest.mark.parametrize("operation", ["close", "agent_withdraw", "admin_withdraw"])
@pytest.mark.parametrize("first", ["expiry", "write"])
def test_maintenance_expiry_and_beacon_writes_respect_both_tunnel_lock_orders(
    credential_factory: Any,
    user_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    first: str,
) -> None:
    creator, _ = credential_factory()
    actor = user_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-beacon-expiry-race-create",
        root_content={"type": "text", "text": "Keep one final Beacon ending."},
    )
    published, _ = publish_beacon(
        credential=creator,
        idempotency_key="pg-beacon-expiry-race-publish",
        address=created.address,
        text="Review this work.",
        lifetime_seconds=60,
    )
    beacon_id = UUID(published["id"])
    expiry = datetime.fromisoformat(published["expires_at"])
    assert expiry < created.cycle.expires_at
    monkeypatch.setattr(timezone, "now", lambda: expiry)
    initial_board_position = BoardState.objects.get(pk=1).position
    owner_locked = threading.Event()
    release_owner = threading.Event()
    second_attempted = threading.Event()
    pids: dict[str, int] = {}

    def operate(role: str) -> str:
        close_old_connections()

        def observe_lock(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
            tunnel_lock = (
                sql.startswith("SELECT") and 'FROM "tunnels_tunnel"' in sql and "FOR UPDATE" in sql
            )
            if tunnel_lock and role != first:
                second_attempted.set()
            result = execute(sql, params, many, context)
            if tunnel_lock and role == first and not owner_locked.is_set():
                owner_locked.set()
                if not release_owner.wait(timeout=15):
                    raise TimeoutError("The test did not release the production tunnel lock.")
            return result

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = int(cursor.fetchone()[0])
            with connection.execute_wrapper(observe_lock):
                if role == "expiry":
                    expired, candidates = maintenance._expire_beacons(now=expiry)
                    assert candidates == 1
                    return "expired" if expired else "already ended"
                credential = _credential(creator.id)
                if operation == "close":
                    close_cycle(
                        credential=credential,
                        idempotency_key="pg-beacon-expiry-race-close",
                        address=created.address,
                        expected_cycle_id=created.cycle.id,
                    )
                    return "closed"
                try:
                    if operation == "agent_withdraw":
                        withdraw_beacon(
                            credential=credential,
                            idempotency_key="pg-beacon-expiry-race-withdraw",
                            address=created.address,
                            beacon_id=beacon_id,
                        )
                    else:
                        withdraw_beacon_as_admin(actor=actor, beacon_id=beacon_id)
                except LifecycleConflict:
                    return "conflict"
                return "withdrawn"
        finally:
            connection.close()

    second = "write" if first == "expiry" else "expiry"
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(operate, first)
        try:
            assert owner_locked.wait(timeout=10)
            waiter = pool.submit(operate, second)
            assert second_attempted.wait(timeout=10)
            _wait_for_production_tunnel_lock(waiter=pids[second], owner=pids[first])
        finally:
            release_owner.set()
        outcomes = {first: owner.result(timeout=20), second: waiter.result(timeout=20)}

    if operation == "agent_withdraw":
        assert outcomes == {"expiry": "expired", "write": "conflict"}
    elif first == "expiry":
        assert outcomes == {
            "expiry": "expired",
            "write": "closed" if operation == "close" else "conflict",
        }
    else:
        assert outcomes == {
            "expiry": "already ended",
            "write": "closed" if operation == "close" else "withdrawn",
        }
    beacon = Beacon.objects.get(pk=beacon_id)
    assert beacon.ended_at == expiry
    assert beacon.end_reason == Beacon.EndReason.EXPIRED
    assert BoardState.objects.get(pk=1).position == initial_board_position + 1
    events = list(TunnelEvent.objects.filter(tunnel=created.tunnel).order_by("position"))
    assert [event.position for event in events] == list(range(1, len(events) + 1))
    endings = [event for event in events if event.event_type == TunnelEvent.Type.BEACON_ENDED]
    assert len(endings) == 1
    assert endings[0].metadata == {"beacon_id": str(beacon_id), "reason": "expired"}
    assert AuditEvent.objects.filter(action="beacon.ended", target_id=beacon_id).count() == 1


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


@pytest.mark.parametrize("operation", ["publish", "withdraw"])
def test_concurrent_identical_beacon_retries_return_one_result(
    credential_factory: Any, operation: str
) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-beacon-retry-create",
        root_content={"type": "text", "text": "Root."},
    )
    beacon_id = None
    if operation == "withdraw":
        published, _ = publish_beacon(
            credential=creator,
            idempotency_key="pg-beacon-retry-initial-publish",
            address=created.address,
            text="Review this.",
        )
        beacon_id = UUID(published["id"])
    barrier = threading.Barrier(2)

    def attempt(_: int) -> tuple[dict[str, Any], bool]:
        close_old_connections()
        try:
            credential = _credential(creator.id)
            barrier.wait(timeout=15)
            if operation == "publish":
                return publish_beacon(
                    credential=credential,
                    idempotency_key="pg-identical-beacon-retry",
                    address=created.address,
                    text="Review this.",
                )
            assert beacon_id is not None
            return withdraw_beacon(
                credential=credential,
                idempotency_key="pg-identical-beacon-retry",
                address=created.address,
                beacon_id=beacon_id,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(replayed for _, replayed in results) == [False, True]
    expected_position = 2 if operation == "withdraw" else 1
    assert BoardState.objects.get(pk=1).position == expected_position
    event_type = (
        TunnelEvent.Type.BEACON_ENDED if operation == "withdraw" else TunnelEvent.Type.BEACON_LIT
    )
    assert created.tunnel.events.filter(event_type=event_type).count() == 1
