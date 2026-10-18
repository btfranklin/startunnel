"""Deadline maintenance processes bounded persisted work."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.contrib.sessions.models import Session
from django.db.models import QuerySet
from django.utils import timezone

from accounts.models import AdminOperation
from core.logging import SafeJsonFormatter
from core.maintenance_status import read_maintenance_status
from core.models import MaintenanceState, RateLimitBucket
from tunnels.cycles import create_tunnel
from tunnels.maintenance import (
    _delete_batch,
    _delete_due_cycles,
    cleanup_once,
    has_due_work,
    next_deadline,
    record_maintenance_error,
    record_notification_wake,
)
from tunnels.management.commands.maintenance_loop import Command as MaintenanceCommand
from tunnels.messages import post_reply
from tunnels.models import AuditEvent, Cycle, IdempotencyRecord, Message

pytestmark = pytest.mark.django_db


def _tree(credential_factory: Any, suffix: str) -> Any:
    creator, _ = credential_factory(name=f"Coordinator {suffix}")
    created = create_tunnel(
        credential=creator,
        idempotency_key=f"create-{suffix}",
        root_content={"type": "text", "text": "Review the release."},
    )
    post_reply(
        credential=creator,
        idempotency_key=f"reply-{suffix}",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "The review is complete."},
    )
    return created


def test_cleanup_closes_expired_cycle_from_original_expiry(credential_factory: Any) -> None:
    created = _tree(credential_factory, "expired")
    now = timezone.now()
    expiry = now - timedelta(minutes=10)
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=expiry, retention_seconds=60)
    counts = cleanup_once(now=now)
    created.cycle.refresh_from_db()
    assert counts["cycles_closed"] == 1
    assert counts["cycle_content_deleted"] == 1
    assert created.cycle.closed_at == expiry
    assert created.cycle.state == Cycle.State.DELETED
    assert not Message.objects.filter(cycle=created.cycle).exists()


def test_cleanup_keeps_future_cycle_and_records_status(credential_factory: Any) -> None:
    created = _tree(credential_factory, "future")
    counts = cleanup_once(now=timezone.now())
    created.cycle.refresh_from_db()
    assert counts["cycles_closed"] == 0
    assert created.cycle.state == Cycle.State.ACTIVE
    row = MaintenanceState.objects.get(key="worker")
    assert row.reconciliation_count == 1
    cleanup_once(now=timezone.now())
    row.refresh_from_db()
    assert row.reconciliation_count == 2


def test_cleanup_purges_all_due_noncontent_categories(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    now = timezone.now()
    record = IdempotencyRecord.objects.create(
        credential=credential,
        operation="expired",
        key_digest=b"i" * 32,
        request_digest=b"r" * 32,
        expires_at=now - timedelta(seconds=1),
    )
    audit = AuditEvent.objects.create(
        actor_credential=credential,
        action="old",
        target_type="test",
    )
    AuditEvent.objects.filter(pk=audit.pk).update(created_at=now - timedelta(days=31))
    Session.objects.create(
        session_key="expired-session",
        session_data="e30:1:signature",
        expire_date=now - timedelta(seconds=1),
    )
    bucket = RateLimitBucket.objects.create(
        key_digest=b"b" * 32,
        accepted_at_ms=[],
        expires_at=now - timedelta(seconds=1),
    )
    counts = cleanup_once(now=now)
    assert counts["idempotency_purged"] == 1
    assert counts["audit_events_purged"] == 1
    assert counts["sessions_purged"] == 1
    assert counts["rate_limit_buckets_purged"] == 1
    assert not IdempotencyRecord.objects.filter(pk=record.pk).exists()
    assert not RateLimitBucket.objects.filter(pk=bucket.pk).exists()


def test_cleanup_limits_each_batch(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    now = timezone.now()
    monkeypatch.setattr("tunnels.maintenance.MAINTENANCE_BATCH_SIZE", 2)
    IdempotencyRecord.objects.bulk_create(
        [
            IdempotencyRecord(
                credential=credential,
                operation="bounded",
                key_digest=hashlib.sha256(str(index).encode()).digest(),
                request_digest=b"r" * 32,
                expires_at=now - timedelta(seconds=1),
            )
            for index in range(3)
        ]
    )
    assert cleanup_once(now=now)["idempotency_purged"] == 2
    assert cleanup_once(now=now)["idempotency_purged"] == 1


@pytest.mark.parametrize("kind", ["session", "rate_bucket"])
def test_delete_batch_preserves_rows_refreshed_after_selection(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    now = timezone.now()
    if kind == "session":
        row: Any = Session.objects.create(
            session_key="refreshed-session", session_data="", expire_date=now
        )
        field = "expire_date"
    else:
        row = RateLimitBucket.objects.create(key_digest=b"r" * 32, expires_at=now)
        field = "expires_at"
    original_delete = QuerySet.delete

    def refresh_before_delete(queryset: QuerySet[Any]) -> tuple[int, dict[str, int]]:
        type(row).objects.filter(pk=row.pk).update(**{field: now + timedelta(hours=1)})
        return original_delete(queryset)

    monkeypatch.setattr(QuerySet, "delete", refresh_before_delete)
    query = type(row).objects.filter(**{field + "__lte": now})
    assert _delete_batch(query, ordered_field=field) == 0
    assert type(row).objects.filter(pk=row.pk).exists()


def test_failed_deletion_does_not_delay_other_batches_or_nearer_deadlines(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _tree(credential_factory, "failed-deletion-backlog")
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now - timedelta(seconds=30),
        final_sequence=2,
    )
    RateLimitBucket.objects.bulk_create(
        [
            RateLimitBucket(
                key_digest=hashlib.sha256(str(index).encode()).digest(),
                expires_at=now - timedelta(seconds=1),
            )
            for index in range(201)
        ]
    )
    Session.objects.create(
        session_key="near-future-expiry", session_data="", expire_date=now + timedelta(seconds=5)
    )
    attempts: list[bool] = []

    def fail_deletion(**kwargs: Any) -> bool:
        attempts.append(True)
        raise RuntimeError("Synthetic content deletion failure.")

    monkeypatch.setattr("tunnels.maintenance.delete_due_cycle_content", fail_deletion)

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

    class Listener:
        def notifies(self, *, timeout: float, stop_after: int) -> list[object]:
            assert timeout == 5 and stop_after == 1
            assert not RateLimitBucket.objects.exists()
            stop.stopped = True
            return []

    stop = Stop()
    with time_machine.travel(now, tick=False):
        MaintenanceCommand()._postgres_loop(Listener(), stop)  # type: ignore[arg-type]
    assert len(attempts) == 4
    assert Cycle.objects.get(pk=created.cycle.pk).state == Cycle.State.CLOSED


def test_next_deadline_and_due_work_use_earliest_persisted_value(
    credential_factory: Any,
) -> None:
    created = _tree(credential_factory, "deadline")
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=now + timedelta(minutes=2))
    assert next_deadline() == Cycle.objects.get(pk=created.cycle.pk).expires_at
    assert not has_due_work(now=now)
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=now - timedelta(seconds=1))
    assert has_due_work(now=now)


def test_admin_receipt_deadlines_and_cleanup_are_bounded(
    user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = user_factory()
    now = timezone.now()
    expiry = now - timedelta(seconds=1)
    future = now + timedelta(hours=1)
    monkeypatch.setattr("tunnels.maintenance.MAINTENANCE_BATCH_SIZE", 2)
    AdminOperation.objects.bulk_create(
        [
            AdminOperation(
                owner=owner,
                operation="accounts.create",
                key_digest=hashlib.sha256(str(index).encode()).digest(),
                request_digest=b"r" * 32,
                derivation_nonce=b"n" * 32,
                expires_at=expiry if index < 3 else future,
            )
            for index in range(4)
        ]
    )
    audit = AuditEvent.objects.create(actor_user=owner, action="recent", target_type="test")

    assert next_deadline() == expiry
    assert has_due_work(now=now)
    assert cleanup_once(now=now)["admin_operations_purged"] == 2
    assert MaintenanceState.objects.get(key="worker").due_work_count == 2
    assert has_due_work(now=now)
    assert cleanup_once(now=now)["admin_operations_purged"] == 1
    assert cleanup_once(now=now)["admin_operations_purged"] == 0
    assert not has_due_work(now=now)
    assert next_deadline() == future
    assert list(AdminOperation.objects.values_list("expires_at", flat=True)) == [future]
    assert AuditEvent.objects.filter(pk=audit.pk).exists()


def test_failed_cycle_does_not_block_later_deletion(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    first = _tree(credential_factory, "failure-first")
    second = _tree(credential_factory, "failure-second")
    now = timezone.now()
    Cycle.objects.filter(pk__in=[first.cycle.pk, second.cycle.pk]).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now - timedelta(seconds=1),
        final_sequence=2,
    )
    original = __import__(
        "tunnels.maintenance", fromlist=["delete_due_cycle_content"]
    ).delete_due_cycle_content

    def fail_one(*, cycle_id: Any, now: Any) -> bool:
        if cycle_id == first.cycle.id:
            raise RuntimeError("expected test failure")
        return bool(original(cycle_id=cycle_id, now=now))

    monkeypatch.setattr("tunnels.maintenance.delete_due_cycle_content", fail_one)
    with caplog.at_level(logging.ERROR, logger="tunnels.maintenance"):
        deleted, failures, candidates = _delete_due_cycles(now=now)
    assert (deleted, failures, candidates) == (1, 1, 2)
    assert Cycle.objects.get(pk=second.cycle.pk).state == Cycle.State.DELETED
    event = json.loads(SafeJsonFormatter().format(caplog.records[-1]))
    assert event["message"] == "Cycle content deletion failed."
    assert event["cycle_id"] == str(first.cycle.id)
    assert event["exception_type"] == "RuntimeError"
    assert "expected test failure" not in json.dumps(event)


def test_cleanup_records_deletion_failure_and_observed_lag(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _tree(credential_factory, "record-failure")
    now = timezone.now()
    Cycle.objects.filter(pk=created.cycle.pk).update(
        state=Cycle.State.CLOSED,
        closed_at=now - timedelta(minutes=2),
        delete_after=now - timedelta(seconds=30),
        final_sequence=2,
    )
    monkeypatch.setattr(
        "tunnels.maintenance.delete_due_cycle_content",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("expected test failure")),
    )

    counts = cleanup_once(now=now)
    status = read_maintenance_status()
    assert counts["cycle_delete_failures"] == 1
    assert status.last_error == "1 cycle content deletion(s) failed."
    assert status.cleanup_lag_seconds == 30


def test_status_handles_missing_stale_error_and_notification_rows() -> None:
    assert read_maintenance_status().current is False
    old = timezone.now() - timedelta(minutes=4)
    MaintenanceState.objects.create(key="worker", reconciled_at=old)
    assert read_maintenance_status().current is False
    record_maintenance_error("x" * 300)
    record_notification_wake()
    status = read_maintenance_status()
    assert len(status.last_error) == 240
    assert status.notification_wake_count == 1
