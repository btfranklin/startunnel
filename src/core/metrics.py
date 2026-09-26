"""Low-cardinality service metrics."""

from __future__ import annotations

import os

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

API_REQUESTS = Counter(
    "startunnel_api_requests_total", "API requests", ["operation", "status_class"]
)
API_REQUEST_SECONDS = Histogram(
    "startunnel_api_request_seconds",
    "API request time",
    ["operation", "status_class"],
)
DB_OPERATION_SECONDS = Histogram(
    "startunnel_db_operation_seconds", "Database operation time", ["operation"]
)
TUNNELS_CREATED = Counter("startunnel_tunnels_created_total", "Created tunnels")
MESSAGES_SENT = Counter("startunnel_messages_sent_total", "Sent messages", ["type"])
RATE_LIMITED = Counter("startunnel_rate_limited_total", "Rejected rate-limited requests", ["rule"])
CLEANUP_LAG_SECONDS = Gauge(
    "startunnel_cleanup_lag_seconds",
    "Oldest pending cleanup lag",
    multiprocess_mode="mostrecent",
)
MAINTENANCE_RECONCILIATIONS = Gauge(
    "startunnel_maintenance_reconciliations_total",
    "Completed maintenance reconciliations",
    multiprocess_mode="mostrecent",
)
MAINTENANCE_DUE_WORK = Gauge(
    "startunnel_maintenance_due_work",
    "Due records seen in the latest maintenance reconciliation",
    multiprocess_mode="mostrecent",
)
MAINTENANCE_NOTIFICATION_WAKES = Gauge(
    "startunnel_maintenance_notification_wakes_total",
    "Maintenance wakes caused by PostgreSQL notifications",
    multiprocess_mode="mostrecent",
)


def render_metrics() -> bytes:
    """Render either the local registry or all Uvicorn worker shards."""

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return generate_latest()
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return generate_latest(registry)
