"""Low-cardinality service metrics."""

from __future__ import annotations

import os
from collections.abc import Callable

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
DATABASE_POOL_SIZE = Gauge(
    "startunnel_database_pool_size",
    "Connections in the default PostgreSQL pool",
    multiprocess_mode="livesum",
)
DATABASE_POOL_AVAILABLE = Gauge(
    "startunnel_database_pool_available",
    "Available connections in the default PostgreSQL pool",
    multiprocess_mode="livesum",
)
DATABASE_POOL_WAITING = Gauge(
    "startunnel_database_pool_waiting",
    "Requests waiting for a connection in the default PostgreSQL pool",
    multiprocess_mode="livesum",
)
DATABASE_POOL_QUEUED = Gauge(
    "startunnel_database_pool_queued",
    "Requests queued for a connection in the default PostgreSQL pool",
    multiprocess_mode="livesum",
)
DATABASE_POOL_REQUESTS_WAIT_MS = Gauge(
    "startunnel_database_pool_requests_wait_ms",
    "Cumulative milliseconds that requests waited for a default PostgreSQL pool connection",
    multiprocess_mode="livesum",
)
DATABASE_POOL_ERRORS = Gauge(
    "startunnel_database_pool_errors",
    "Cumulative connection and request errors reported by the default PostgreSQL pool",
    multiprocess_mode="livesum",
)
DATABASE_POOL_SAMPLER_STATUS = Gauge(
    "startunnel_database_pool_sampler_status",
    "Workers in each pool stats sampler status category",
    ["status"],
    multiprocess_mode="livesum",
)


def render_metrics() -> bytes:
    """Render either the local registry or all Uvicorn worker shards."""

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return generate_latest()
    registry = CollectorRegistry()
    # The Prometheus multiprocess collector has no public type annotations.
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return generate_latest(registry)


def measure_database_query[T](
    execute: Callable[[str, object, bool, dict[str, object]], T],
    sql: str,
    params: object,
    many: bool,
    context: dict[str, object],
) -> T:
    """Measure each query in the thread that owns its database connection."""

    first_word = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else "other"
    operation = first_word if first_word in {"delete", "insert", "select", "update"} else "other"
    with DB_OPERATION_SECONDS.labels(operation=operation).time():
        return execute(sql, params, many, context)
