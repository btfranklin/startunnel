"""Stream numeric load observations from one disposable Compose project."""

from __future__ import annotations

import json
import math
import os
import re
import resource
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import IsolatedComposeProject, assert_local_docker
    from scripts.docker_stats import DockerStats
else:
    from compose_isolation import IsolatedComposeProject, assert_local_docker
    from docker_stats import DockerStats

SERVICES = ("web", "postgres", "maintenance", "load-profile")
POOL_FIELDS = (
    "size",
    "available",
    "waiting",
    "queued",
    "errors",
    "requests_wait_ms",
)
POOL_STATUSES = ("ok", "missing", "error")
EXPECTED_POOL_WORKERS = 4
DATABASE_FIELDS = (
    "connections",
    "active_connections",
    "idle_in_transaction",
    "oldest_transaction_seconds",
    "lock_count",
    "ungranted_locks",
    "lock_waiters",
    "io_waiters",
    "oldest_wait_seconds_estimate",
)
SQL = """SELECT json_build_object(
 'connections', count(*),
 'active_connections', count(*) FILTER (WHERE state = 'active'),
 'idle_in_transaction', count(*) FILTER (WHERE state = 'idle in transaction'),
 'oldest_transaction_seconds', coalesce(max(extract(epoch FROM clock_timestamp()-xact_start)),0),
 'lock_waiters', count(*) FILTER (WHERE wait_event_type = 'Lock'),
 'io_waiters', count(*) FILTER (WHERE wait_event_type = 'IO'),
 'oldest_wait_seconds_estimate', coalesce(max(extract(epoch FROM clock_timestamp()-query_start))
     FILTER (WHERE wait_event_type = 'Lock'),0),
 'lock_count', (SELECT count(*) FROM pg_locks),
 'ungranted_locks', (SELECT count(*) FROM pg_locks WHERE NOT granted)
 ) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid();"""
# This trusted Engine exec reads worker shards without an HTTP request or a token.
METRICS_COMMAND = (
    "from prometheus_client import CollectorRegistry,generate_latest,multiprocess; "
    "registry=CollectorRegistry(); multiprocess.MultiProcessCollector(registry); "
    "print(generate_latest(registry).decode())"
)
POSTGRES_COMMAND = (
    "psql",
    "--username",
    "startunnel_admin",
    "--dbname",
    "startunnel",
    "--tuples-only",
    "--no-align",
    "--quiet",
    "--command",
    SQL,
)
POSTGRES_ENVIRONMENT = (
    "PGCONNECT_TIMEOUT=1",
    "PGOPTIONS=-c statement_timeout=500 -c lock_timeout=250",
)


def numeric(value: object) -> int | float | None:
    """Discard all text and non-finite numbers at the output boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def numeric_fields(source: object, names: tuple[str, ...]) -> dict[str, int | float | None]:
    values = source if isinstance(source, dict) else {}
    return {name: numeric(values.get(name)) for name in names}


def pool_metrics(raw: str) -> dict[str, object]:
    values: dict[str, object] = {name: None for name in POOL_FIELDS}
    values.update({f"sampler_{status}": None for status in POOL_STATUSES})
    for line in raw.splitlines():
        match = re.fullmatch(
            r"startunnel_database_pool_([a-z_]+)(?:\{([^}]*)\})? ([0-9.eE+-]+)", line
        )
        if not match:
            continue
        name, labels, raw_value = match.groups()
        try:
            value = numeric(float(raw_value))
        except ValueError:
            continue
        if name in POOL_FIELDS and labels is None:
            values[name] = value
        elif name == "sampler_status":
            for status in POOL_STATUSES:
                if labels == f'status="{status}"':
                    values[f"sampler_{status}"] = value
    return values


def container_counters(raw: dict[str, Any]) -> dict[str, object]:
    cpu = raw.get("cpu_stats") or {}
    memory = raw.get("memory_stats") or {}
    return {
        "cpu_usage_ns": numeric((cpu.get("cpu_usage") or {}).get("total_usage")),
        "cpu_throttling": numeric_fields(
            cpu.get("throttling_data"), ("periods", "throttled_periods", "throttled_time")
        ),
        "memory": numeric_fields(memory, ("usage", "limit", "max_usage", "failcnt")),
        "memory_pressure": numeric_fields(
            memory.get("stats"),
            (
                "anon",
                "rss",
                "pgfault",
                "pgmajfault",
                "oom",
                "oom_kill",
                "pgscan",
                "pgsteal",
                "total_pgfault",
                "total_pgmajfault",
                "pgscan_kswapd",
                "pgscan_direct",
                "pgsteal_kswapd",
                "pgsteal_direct",
            ),
        ),
    }


class LoadObservations:
    """Write each sample at once; never retain the sample history in memory."""

    def __init__(self, stack: IsolatedComposeProject, path: Path) -> None:
        self.stack = stack
        self.path = path
        endpoint = stack.environment.get("DOCKER_HOST") or assert_local_docker(stack.environment)
        self.stats = DockerStats(endpoint, timeout=1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="load-observations", daemon=True)
        self.samples = 0
        self.errors = 0
        self.failed = False
        self.services_seen: set[str] = set()
        self.postgres_samples = 0
        self.pool_samples = 0
        self.pool_missing_samples = 0
        self.pool_error_samples = 0
        self.first_monotonic: float | None = None
        self.last_monotonic: float | None = None
        self.max_gap_seconds = 0.0
        self.identifiers: dict[str, str] = {}
        self.capacity: dict[str, object] = {"host_cpu_count": os.cpu_count()}
        self._output: Any = None
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="load-counter")
        self.max_sample_overhead_seconds = 0.0

    def __enter__(self) -> LoadObservations:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.path.open("x", encoding="utf-8")
        try:
            self.capacity.update(numeric_fields(self.stats.get("/info"), ("NCPU", "MemTotal")))
        except (ValueError, OSError):
            self.capacity.update(NCPU=None, MemTotal=None)
        self.thread.start()
        return self

    def __exit__(self, *arguments: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.failed = True
            self.errors += 1
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self._output is not None:
            self._output.close()

    def summary(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "complete": (
                not self.failed
                and self.max_sample_overhead_seconds < 1
                and self.samples > 0
                and self.services_seen == set(SERVICES)
                and self.postgres_samples > 0
                and self.pool_samples > 0
            ),
            "services_seen": sorted(self.services_seen),
            "postgres_samples": self.postgres_samples,
            "pool_samples": self.pool_samples,
            "expected_pool_workers": EXPECTED_POOL_WORKERS,
            "pool_missing_samples": self.pool_missing_samples,
            "pool_error_samples": self.pool_error_samples,
            "first_monotonic_seconds": self.first_monotonic,
            "last_monotonic_seconds": self.last_monotonic,
            "max_sample_gap_seconds": self.max_gap_seconds,
            "max_sample_overhead_seconds": self.max_sample_overhead_seconds,
            "errors": self.errors,
            "collector_failed": self.failed,
            "sample_interval_seconds": 1,
            "host_capacity": self.capacity,
            "missing_values": "null means unavailable; no unavailable counter is treated as zero",
            "wait_estimate": "Age of the oldest query waiting on a lock; not exact wait duration",
        }

    def _sample(self) -> dict[str, object]:
        started = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        sample: dict[str, object] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "monotonic_seconds": started,
            "services": {name: None for name in SERVICES},
            "postgres": {name: None for name in DATABASE_FIELDS},
            "pool": {
                **{name: None for name in POOL_FIELDS},
                **{f"sampler_{status}": None for status in POOL_STATUSES},
            },
            "errors": [],
        }
        errors: list[str] = []
        try:
            if len(self.identifiers) < len(SERVICES):
                query = urlencode(
                    {
                        "all": "true",
                        "filters": json.dumps(
                            {"label": [f"com.docker.compose.project={self.stack.project_name}"]}
                        ),
                    }
                )
                for row in self.stats.get_list(f"/containers/json?{query}"):
                    name = (row.get("Labels") or {}).get("com.docker.compose.service")
                    identifier = row.get("Id", "")
                    if (
                        name in SERVICES
                        and row.get("State") == "running"
                        and re.fullmatch(r"[a-f0-9]{12,64}", identifier)
                    ):
                        self.identifiers[name] = identifier
        except (ValueError, OSError, KeyError, TypeError):
            errors.append("container_inventory_unavailable")
        jobs: dict[str, Future[Any]] = {
            name: self.executor.submit(
                self.stats.get, f"/containers/{quote(identifier)}/stats?stream=false&one-shot=true"
            )
            for name, identifier in self.identifiers.items()
        }
        if "postgres" in self.identifiers:
            jobs["postgres_aggregate"] = self.executor.submit(
                self.stats.exec,
                self.identifiers["postgres"],
                POSTGRES_COMMAND,
                environment=POSTGRES_ENVIRONMENT,
            )
        if "web" in self.identifiers:
            jobs["pool"] = self.executor.submit(
                self.stats.exec,
                self.identifiers["web"],
                ("/app/.venv/bin/python", "-c", METRICS_COMMAND),
            )
        _, pending = wait(jobs.values(), timeout=max(0.0, started + 1 - time.monotonic()))
        if pending:
            # Stop the collector after a missed deadline. No old job can queue into a later sample.
            self.failed = True
            self.stop_event.set()
            errors.append("sample_deadline_exceeded")
            for future in pending:
                future.cancel()
        services: dict[str, object] = {name: None for name in SERVICES}
        for name, future in jobs.items():
            if future in pending:
                errors.append(f"{name}_counters_unavailable")
                continue
            try:
                value = future.result()
                if name in SERVICES:
                    services[name] = container_counters(value)
                    self.services_seen.add(name)
                elif name == "postgres_aggregate":
                    postgres = numeric_fields(json.loads(value), DATABASE_FIELDS)
                    sample["postgres"] = postgres
                    if all(item is not None for item in postgres.values()):
                        self.postgres_samples += 1
                elif name == "pool":
                    pool = pool_metrics(value)
                    sample["pool"] = pool
                    if pool.get("sampler_ok") == EXPECTED_POOL_WORKERS:
                        self.pool_samples += 1
                    if (numeric(pool.get("sampler_missing")) or 0) > 0:
                        self.pool_missing_samples += 1
                    if (numeric(pool.get("sampler_error")) or 0) > 0:
                        self.pool_error_samples += 1
                        errors.append("pool_sampler_error")
            except (ValueError, OSError, KeyError, TypeError):
                errors.append(f"{name}_counters_unavailable")
        sample["services"] = services
        sample["errors"] = errors
        self.errors += len(errors)
        overhead = time.monotonic() - started
        self.max_sample_overhead_seconds = max(self.max_sample_overhead_seconds, overhead)
        if overhead >= 1:
            self.failed = True
            self.stop_event.set()
        sample["collector"] = {
            "thread_cpu_seconds": time.thread_time(),
            "process_cpu_seconds": usage.ru_utime + usage.ru_stime,
            "child_cpu_seconds": children.ru_utime + children.ru_stime,
            "peak_memory_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            "sample_overhead_seconds": overhead,
        }
        return sample

    def _run(self) -> None:
        due = time.monotonic()
        try:
            while not self.stop_event.is_set():
                sample = self._sample()
                captured = float(str(sample["monotonic_seconds"]))
                if self.first_monotonic is None:
                    self.first_monotonic = captured
                if self.last_monotonic is not None:
                    self.max_gap_seconds = max(self.max_gap_seconds, captured - self.last_monotonic)
                self.last_monotonic = captured
                sample["collector"]["scheduling_lag_seconds"] = max(  # type: ignore[index]
                    0.0,
                    sample["monotonic_seconds"] - due,  # type: ignore[operator]
                )
                self._output.write(json.dumps(sample, sort_keys=True) + "\n")
                self._output.flush()
                self.samples += 1
                due += 1
                # Do not queue catch-up work when a sample takes longer than a second.
                due = max(due, time.monotonic())
                self.stop_event.wait(max(0.0, due - time.monotonic()))
        except Exception:
            # Never serialize an exception, which can contain command or response text.
            self.failed = True
            self.errors += 1
