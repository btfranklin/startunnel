"""Read persisted maintenance progress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from .models import MaintenanceState

STATUS_STALE_SECONDS = 180


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    current: bool
    cleanup_lag_seconds: float | None
    reconciliation_count: int | None
    due_work_count: int | None
    notification_wake_count: int | None
    last_error: str


def read_maintenance_status() -> MaintenanceStatus:
    row = MaintenanceState.objects.filter(key="worker").first()
    if row is None or row.reconciled_at is None:
        return MaintenanceStatus(False, None, None, None, None, "")
    current = row.reconciled_at >= timezone.now() - timedelta(seconds=STATUS_STALE_SECONDS)
    return MaintenanceStatus(
        current=current,
        cleanup_lag_seconds=row.oldest_overdue_seconds,
        reconciliation_count=row.reconciliation_count,
        due_work_count=row.due_work_count,
        notification_wake_count=row.notification_wake_count,
        last_error=row.last_error,
    )
