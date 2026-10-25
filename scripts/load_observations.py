"""Stream numeric load observations from one disposable Compose project."""

from __future__ import annotations

import json
import math
import os
import re
import resource
import subprocess  # nosec B404
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
# The token stays in the container environment and never enters a host argument.
METRICS_COMMAND = (
    "import os,urllib.request; "
    "r=urllib.request.Request('http://127.0.0.1:8000/metrics',"
    "headers={'X-Metrics-Token':os.environ['STARTUNNEL_METRICS_TOKEN']}); "
    "print(urllib.request.urlopen(r,timeout=1).read(1048576).decode())"
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


def _command(stack: IsolatedComposeProject, *arguments: str) -> str:
    result = subprocess.run(  # nosec B603
        stack.compose_command(*arguments),
        env=stack.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )
    if result.returncode:
        raise ValueError("Observation unavailable.")
    return result.stdout


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
        # One iteration has four Engine reads plus three bounded CLI reads.
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            self.failed = True
            self.errors += 1
        elif self._output is not None:
            self._output.close()

    def summary(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "complete": (
                not self.failed
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
                identifiers = _command(self.stack, "ps", "--all", "--format", "json")
                rows = (
                    json.loads(identifiers)
                    if identifiers.lstrip().startswith("[")
                    else [json.loads(line) for line in identifiers.splitlines() if line]
                )
                for row in rows:
                    name, identifier = row.get("Service"), row.get("ID", "")
                    if (
                        name in SERVICES
                        and row.get("State") == "running"
                        and re.fullmatch(r"[a-f0-9]{12,64}", identifier)
                    ):
                        self.identifiers[name] = identifier
            services: dict[str, object] = {name: None for name in SERVICES}
            for name, identifier in self.identifiers.items():
                try:
                    services[name] = container_counters(
                        self.stats.get(
                            f"/containers/{quote(identifier)}/stats?stream=false&one-shot=true"
                        )
                    )
                    self.services_seen.add(name)
                except (ValueError, OSError):
                    errors.append(f"{name}_counters_unavailable")
            sample["services"] = services
        except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError):
            errors.append("container_inventory_unavailable")
        try:
            raw = _command(
                self.stack,
                "exec",
                "-T",
                "-e",
                "PGCONNECT_TIMEOUT=1",
                "-e",
                "PGOPTIONS=-c statement_timeout=1000 -c lock_timeout=500",
                "postgres",
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
            postgres = numeric_fields(json.loads(raw), DATABASE_FIELDS)
            sample["postgres"] = postgres
            if all(value is not None for value in postgres.values()):
                self.postgres_samples += 1
        except (ValueError, OSError, subprocess.SubprocessError):
            errors.append("postgres_counters_unavailable")
        try:
            pool = pool_metrics(
                _command(
                    self.stack, "exec", "-T", "web", "/app/.venv/bin/python", "-c", METRICS_COMMAND
                )
            )
            sample["pool"] = pool
            if pool.get("sampler_ok") == EXPECTED_POOL_WORKERS:
                self.pool_samples += 1
            if (numeric(pool.get("sampler_missing")) or 0) > 0:
                self.pool_missing_samples += 1
            if (numeric(pool.get("sampler_error")) or 0) > 0:
                self.pool_error_samples += 1
                errors.append("pool_sampler_error")
        except (ValueError, OSError, subprocess.SubprocessError):
            errors.append("pool_counters_unavailable")
        sample["errors"] = errors
        self.errors += len(errors)
        sample["collector"] = {
            "thread_cpu_seconds": time.thread_time(),
            "process_cpu_seconds": usage.ru_utime + usage.ru_stime,
            "child_cpu_seconds": children.ru_utime + children.ru_stime,
            "peak_memory_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            "sample_overhead_seconds": time.monotonic() - started,
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
