"""Bounded reconciliation for persisted deadlines."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from core.maintenance_liveness import publish_local_maintenance_heartbeat
from core.models import MaintenanceState, RateLimitBucket

from .models import AuditEvent, Cycle, IdempotencyRecord, Tunnel
from .retention import delete_due_cycle_content
from .services import _close_locked_cycle

ADVISORY_LOCK_ID = 7_195_415_308_821
MAINTENANCE_BATCH_SIZE = 100
AUDIT_RETENTION = timedelta(days=30)
logger = logging.getLogger(__name__)


def _minimum(values: list[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def next_deadline() -> datetime | None:
    """Return the first persisted deadline across all maintenance categories."""

    active_expiry = (
        Cycle.objects.filter(state=Cycle.State.ACTIVE)
        .order_by("expires_at")
        .values_list("expires_at", flat=True)
        .first()
    )
    history_delete = (
        Cycle.objects.filter(state=Cycle.State.CLOSED, delete_after__isnull=False)
        .order_by("delete_after")
        .values_list("delete_after", flat=True)
        .first()
    )
    idempotency_expiry = (
        IdempotencyRecord.objects.order_by("expires_at")
        .values_list("expires_at", flat=True)
        .first()
    )
    oldest_audit = (
        AuditEvent.objects.order_by("created_at").values_list("created_at", flat=True).first()
    )
    session_expiry = (
        Session.objects.order_by("expire_date").values_list("expire_date", flat=True).first()
    )
    bucket_expiry = (
        RateLimitBucket.objects.order_by("expires_at").values_list("expires_at", flat=True).first()
    )
    return _minimum(
        [
            active_expiry,
            history_delete,
            idempotency_expiry,
            oldest_audit + AUDIT_RETENTION if oldest_audit else None,
            session_expiry,
            bucket_expiry,
        ]
    )


def oldest_overdue_deletion_seconds(*, now: datetime) -> float:
    oldest = (
        Cycle.objects.filter(
            state=Cycle.State.CLOSED,
            delete_after__isnull=False,
            delete_after__lte=now,
        )
        .order_by("delete_after")
        .values_list("delete_after", flat=True)
        .first()
    )
    return max((now - oldest).total_seconds(), 0) if oldest else 0


def _close_expired_cycles(*, now: datetime) -> tuple[int, int]:
    cycle_ids = list(
        Cycle.objects.filter(state=Cycle.State.ACTIVE, expires_at__lte=now)
        .order_by("expires_at", "id")
        .values_list("id", flat=True)[:MAINTENANCE_BATCH_SIZE]
    )
    closed = 0
    for cycle_id in cycle_ids:
        with transaction.atomic():
            candidate = Cycle.objects.only("tunnel_id").filter(pk=cycle_id).first()
            if candidate is None:
                continue
            tunnel = Tunnel.objects.select_for_update().get(pk=candidate.tunnel_id)
            cycle = (
                Cycle.objects.select_for_update()
                .filter(pk=cycle_id, state=Cycle.State.ACTIVE, expires_at__lte=now)
                .first()
            )
            if cycle is None:
                continue
            _close_locked_cycle(tunnel=tunnel, cycle=cycle, reason="expired")
            closed += 1
    return closed, len(cycle_ids)


def _delete_due_cycles(*, now: datetime) -> tuple[int, int, int]:
    cycle_ids = list(
        Cycle.objects.filter(
            state=Cycle.State.CLOSED,
            delete_after__isnull=False,
            delete_after__lte=now,
        )
        .order_by("delete_after", "id")
        .values_list("id", flat=True)[:MAINTENANCE_BATCH_SIZE]
    )
    deleted = 0
    failures = 0
    for cycle_id in cycle_ids:
        try:
            if delete_due_cycle_content(cycle_id=cycle_id, now=now):
                deleted += 1
        except Exception:
            failures += 1
            logger.exception("Cycle content deletion failed for cycle %s.", cycle_id)
    return deleted, failures, len(cycle_ids)


def _delete_batch(query: QuerySet[Any], *, ordered_field: str) -> int:
    ids = list(
        query.order_by(ordered_field, "pk").values_list("pk", flat=True)[:MAINTENANCE_BATCH_SIZE]
    )
    if not ids:
        return 0
    return int(query.model.objects.filter(pk__in=ids).delete()[0])


def _record_status(*, now: datetime, lag: float, due_work_count: int, last_error: str) -> None:
    MaintenanceState.objects.update_or_create(
        key="worker",
        defaults={
            "reconciled_at": now,
            "oldest_overdue_seconds": lag,
            "last_error": last_error[:240],
            "due_work_count": due_work_count,
            "reconciliation_count": F("reconciliation_count") + 1,
        },
        create_defaults={
            "reconciled_at": now,
            "oldest_overdue_seconds": lag,
            "last_error": last_error[:240],
            "due_work_count": due_work_count,
            "reconciliation_count": 1,
        },
    )


def record_maintenance_error(message: str) -> None:
    MaintenanceState.objects.update_or_create(
        key="worker",
        defaults={"last_error": message[:240]},
    )


def record_notification_wake() -> None:
    MaintenanceState.objects.filter(key="worker").update(
        notification_wake_count=F("notification_wake_count") + 1
    )


def cleanup_once(*, now: datetime | None = None) -> dict[str, int]:
    checked_at = now or timezone.now()
    publish_local_maintenance_heartbeat()
    closed, close_candidates = _close_expired_cycles(now=checked_at)
    lag = oldest_overdue_deletion_seconds(now=checked_at)
    deleted, deletion_failures, delete_candidates = _delete_due_cycles(now=checked_at)
    idempotency = _delete_batch(
        IdempotencyRecord.objects.filter(expires_at__lte=checked_at),
        ordered_field="expires_at",
    )
    audit = _delete_batch(
        AuditEvent.objects.filter(created_at__lte=checked_at - AUDIT_RETENTION),
        ordered_field="created_at",
    )
    sessions = _delete_batch(
        Session.objects.filter(expire_date__lte=checked_at),
        ordered_field="expire_date",
    )
    buckets = _delete_batch(
        RateLimitBucket.objects.filter(expires_at__lte=checked_at),
        ordered_field="expires_at",
    )
    due_work_count = close_candidates + delete_candidates + idempotency + audit + sessions + buckets
    last_error = (
        f"{deletion_failures} cycle content deletion(s) failed." if deletion_failures else ""
    )
    _record_status(
        now=checked_at,
        lag=lag,
        due_work_count=due_work_count,
        last_error=last_error,
    )
    publish_local_maintenance_heartbeat()
    return {
        "cycles_closed": closed,
        "cycle_content_deleted": deleted,
        "cycle_delete_failures": deletion_failures,
        "idempotency_purged": idempotency,
        "audit_events_purged": audit,
        "sessions_purged": sessions,
        "rate_limit_buckets_purged": buckets,
    }


def has_due_work(*, now: datetime | None = None) -> bool:
    deadline = next_deadline()
    return deadline is not None and deadline <= (now or timezone.now())
