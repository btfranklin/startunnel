"""Forced PostgreSQL lock orders prove close and expiry write boundaries."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from agents.models import AgentCredential
from tunnels import cycles, maintenance, messages
from tunnels.access import resolve_tunnel
from tunnels.errors import CycleClosed
from tunnels.models import Cycle, IdempotencyRecord, Message, Tunnel, TunnelEvent

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def _wait_for_row_lock(*, waiter: int, owner: int) -> None:
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
    pytest.fail("The second operation did not wait for the tunnel row lock.")


@pytest.mark.parametrize("boundary", ["close", "expiry"])
@pytest.mark.parametrize("first", ["post", "close"])
def test_post_and_close_respect_both_tunnel_lock_orders(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch, boundary: str, first: str
) -> None:
    creator, _ = credential_factory()
    writer, _ = credential_factory()
    created = cycles.create_tunnel(
        credential=creator,
        idempotency_key="pg-forced-boundary-create",
        root_content={"type": "text", "text": "Record the write boundary."},
    )
    expiry = timezone.now() - timedelta(seconds=60)
    if boundary == "expiry":
        Cycle.objects.filter(pk=created.cycle.id).update(expires_at=expiry, retention_seconds=300)

    owner_locked = threading.Event()
    release_owner = threading.Event()
    second_started = threading.Event()
    worker_state = threading.local()
    pids: dict[str, int] = {}
    original_resolve = resolve_tunnel
    original_close = cycles._close_locked_cycle

    def pause_first_operation() -> None:
        if worker_state.operation == first and not owner_locked.is_set():
            owner_locked.set()
            if not release_owner.wait(timeout=15):
                raise TimeoutError("The test did not release the tunnel lock.")

    def resolve_then_pause(**kwargs: Any) -> Tunnel:
        tunnel = original_resolve(**kwargs)
        if kwargs.get("for_update"):
            pause_first_operation()
        return tunnel

    def close_after_pause(**kwargs: Any) -> None:
        pause_first_operation()
        original_close(**kwargs)

    monkeypatch.setattr(cycles, "resolve_tunnel", resolve_then_pause)
    monkeypatch.setattr(messages, "resolve_tunnel", resolve_then_pause)
    monkeypatch.setattr(cycles, "_close_locked_cycle", close_after_pause)
    monkeypatch.setattr(maintenance, "_close_locked_cycle", close_after_pause)

    def operate(operation: str) -> str:
        close_old_connections()
        worker_state.operation = operation
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[operation] = int(cursor.fetchone()[0])
            if operation != first:
                second_started.set()
            if operation == "post":
                credential = AgentCredential.objects.get(pk=writer.id)
                try:
                    messages.post_reply(
                        credential=credential,
                        idempotency_key="pg-forced-boundary-reply",
                        address=created.address,
                        parent_id=created.root.id,
                        content={"type": "text", "text": "A reply at the close boundary."},
                    )
                except CycleClosed:
                    return "rejected"
                return "posted"
            if boundary == "expiry":
                assert maintenance._close_expired_cycles(now=timezone.now()) == (1, 1)
            else:
                credential = AgentCredential.objects.get(pk=creator.id)
                cycles.close_cycle(
                    credential=credential,
                    idempotency_key="pg-forced-boundary-close",
                    address=created.address,
                    expected_cycle_id=created.cycle.id,
                )
            return "closed"
        finally:
            connection.close()

    second = "close" if first == "post" else "post"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(operate, first)
        try:
            assert owner_locked.wait(timeout=10)
            second_future = pool.submit(operate, second)
            assert second_started.wait(timeout=10)
            _wait_for_row_lock(waiter=pids[second], owner=pids[first])
        finally:
            release_owner.set()
        outcomes = {
            first: first_future.result(timeout=20),
            second: second_future.result(timeout=20),
        }

    posted = boundary == "close" and first == "post"
    assert outcomes == {"close": "closed", "post": "posted" if posted else "rejected"}
    cycle = Cycle.objects.get(pk=created.cycle.id)
    tunnel = Tunnel.objects.get(pk=created.tunnel.id)
    stored_messages = list(Message.objects.filter(cycle=cycle).order_by("sequence"))
    events = list(TunnelEvent.objects.filter(tunnel=tunnel).order_by("position"))
    assert cycle.state == Cycle.State.CLOSED
    assert tunnel.state == Tunnel.State.DORMANT
    assert len(stored_messages) == (2 if posted else 1)
    assert cycle.final_sequence == cycle.message_count == len(stored_messages)
    assert cycle.content_bytes == sum(message.byte_count for message in stored_messages)
    assert [event.position for event in events] == list(range(1, len(events) + 1))
    assert [event.event_type for event in events[-2:]] == [
        TunnelEvent.Type.CYCLE_CLOSED,
        TunnelEvent.Type.TUNNEL_DORMANT,
    ]
    assert tunnel.next_event_position == len(events) + 1
    assert (
        IdempotencyRecord.objects.filter(credential=writer, operation="post_reply").exists()
        is posted
    )
    if boundary == "expiry":
        assert cycle.closed_at == expiry
        assert cycle.close_reason == "expired"
        assert cycle.delete_after == expiry + timedelta(seconds=300)
