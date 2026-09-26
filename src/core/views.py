"""Health and internal metrics endpoints."""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from prometheus_client import CONTENT_TYPE_LATEST

from .maintenance_status import read_maintenance_status
from .metrics import (
    CLEANUP_LAG_SECONDS,
    MAINTENANCE_DUE_WORK,
    MAINTENANCE_NOTIFICATION_WAKES,
    MAINTENANCE_RECONCILIATIONS,
    render_metrics,
)


def health_live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "live"})


def health_ready(request: HttpRequest) -> JsonResponse:
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["postgres"] = "ready"
    except Exception:
        checks["postgres"] = "unavailable"
    ready = checks.get("postgres") == "ready"
    return JsonResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status=200 if ready else 503,
    )


def metrics(request: HttpRequest) -> HttpResponse:
    supplied = request.headers.get("X-Metrics-Token", "")
    if not supplied or not secrets.compare_digest(supplied, settings.METRICS_TOKEN):
        return HttpResponse(status=404)
    try:
        maintenance = read_maintenance_status()
    except Exception:
        return HttpResponse(status=503)
    if maintenance.cleanup_lag_seconds is not None:
        CLEANUP_LAG_SECONDS.set(maintenance.cleanup_lag_seconds)
    if maintenance.reconciliation_count is not None:
        MAINTENANCE_RECONCILIATIONS.set(maintenance.reconciliation_count)
    if maintenance.due_work_count is not None:
        MAINTENANCE_DUE_WORK.set(maintenance.due_work_count)
    if maintenance.notification_wake_count is not None:
        MAINTENANCE_NOTIFICATION_WAKES.set(maintenance.notification_wake_count)
    return HttpResponse(render_metrics(), content_type=CONTENT_TYPE_LATEST)
