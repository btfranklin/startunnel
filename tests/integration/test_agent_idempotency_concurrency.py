"""PostgreSQL preserves one agent write when receipt requests compete."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection
from django.db.models import QuerySet
from django.utils import timezone

from agents.models import AgentCredential
from core.errors import IdempotencyConflict
from core.models import AgentReceipt
from tunnels.cycles import create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Message

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_concurrent_agent_requests_with_changed_input_conflict(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="agent-conflict-create",
        root_content={"type": "text", "text": "Root."},
    )
    both_started = threading.Barrier(2)

    def reply(text: str) -> UUID | None:
        close_old_connections()
        try:
            actor = AgentCredential.objects.get(pk=credential.pk)
            both_started.wait(timeout=15)
            try:
                result = post_reply(
                    credential=actor,
                    idempotency_key="agent-conflict-reply",
                    address=created.address,
                    parent_id=created.root.id,
                    content={"type": "text", "text": text},
                )
            except IdempotencyConflict:
                return None
            return result.message.id
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reply, text) for text in ("First request.", "Changed request.")]
        results = [future.result(timeout=20) for future in futures]
    successes = [result for result in results if result is not None]
    assert len(successes) == 1
    receipt = AgentReceipt.objects.get(credential=credential, operation="post_reply")
    assert receipt.resource_id == successes[0]
    assert Message.objects.filter(cycle=created.cycle).count() == 2


@pytest.mark.parametrize("changed_input", [False, True])
def test_waiting_agent_retry_recovers_after_expired_receipt_is_replaced(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch, changed_input: bool
) -> None:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="agent-expired-create",
        root_content={"type": "text", "text": "Root."},
    )
    post_reply(
        credential=credential,
        idempotency_key="agent-expired-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Old request."},
    )
    expired = AgentReceipt.objects.get(credential=credential, operation="post_reply")
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])

    both_selecting = threading.Barrier(2)
    expired_locked = threading.Event()
    release_owner = threading.Event()
    first_lookup = threading.local()
    backend_pids: set[int] = set()
    owner_pid: list[int] = []
    original_get = QuerySet.get

    def get_after_both_arrive(queryset: QuerySet[Any], *args: Any, **kwargs: Any) -> Any:
        if (
            queryset.model is AgentReceipt
            and queryset.query.select_for_update
            and not getattr(first_lookup, "started", False)
        ):
            first_lookup.started = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid = int(cursor.fetchone()[0])
                cursor.execute("SET statement_timeout = '15s'")
            backend_pids.add(pid)
            both_selecting.wait(timeout=15)
            record = original_get(queryset, *args, **kwargs)
            if record.pk == expired.pk:
                owner_pid.append(pid)
                expired_locked.set()
                if not release_owner.wait(timeout=15):
                    raise TimeoutError("The test did not release the expired receipt lock.")
            return record
        return original_get(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get", get_after_both_arrive)

    def retry(text: str) -> tuple[UUID | None, bool]:
        close_old_connections()
        try:
            actor = AgentCredential.objects.get(pk=credential.pk)
            try:
                result = post_reply(
                    credential=actor,
                    idempotency_key="agent-expired-reply",
                    address=created.address,
                    parent_id=created.root.id,
                    content={"type": "text", "text": text},
                )
            except IdempotencyConflict:
                return None, False
            return result.message.id, result.replay
        finally:
            connection.close()

    texts = ("New request.", "Changed request." if changed_input else "New request.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(retry, text) for text in texts]
        try:
            assert expired_locked.wait(timeout=15)
            assert len(backend_pids) == 2
            waiter_pid = next(pid for pid in backend_pids if pid != owner_pid[0])
            deadline = time.monotonic() + 10
            waiting = False
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT wait_event_type = 'Lock' AND %s = ANY(pg_blocking_pids(pid)) "
                        "FROM pg_stat_activity WHERE pid = %s",
                        [owner_pid[0], waiter_pid],
                    )
                    row = cursor.fetchone()
                if row and row[0]:
                    waiting = True
                    break
                time.sleep(0.01)
            assert waiting, "The second request did not wait for the expired receipt row."
        finally:
            release_owner.set()
        results = [future.result(timeout=20) for future in futures]

    monkeypatch.setattr(QuerySet, "get", original_get)
    receipt = AgentReceipt.objects.get(credential=credential, operation="post_reply")
    assert receipt.pk != expired.pk
    successes = [resource_id for resource_id, _ in results if resource_id is not None]
    if changed_input:
        assert len(successes) == 1
    else:
        assert successes == [receipt.resource_id, receipt.resource_id]
        assert sorted(replay for _, replay in results) == [False, True]
    assert receipt.resource_id == successes[0]
    assert Message.objects.filter(cycle=created.cycle).count() == 3
    message = Message.objects.get(pk=receipt.resource_id)
    replay = post_reply(
        credential=credential,
        idempotency_key="agent-expired-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": message.text_payload},
    )
    assert replay.replay
    assert replay.message.id == message.id
    assert Message.objects.filter(cycle=created.cycle).count() == 3
