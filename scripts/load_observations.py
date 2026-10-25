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
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TextIO, TypedDict
from urllib.parse import quote, urlencode

if TYPE_CHECKING or __package__:
    from .json_data import (
        JsonObject,
        JsonValue,
        json_object,
        json_string,
        json_value,
    )
else:
    from json_data import (
        JsonObject,
        JsonValue,
        json_object,
        json_string,
        json_value,
    )


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


NumericFields = dict[str, int | float | None]


class ContainerCounters(TypedDict):
    cpu_usage_ns: int | float | None
    cpu_throttling: NumericFields
    memory: NumericFields
    memory_pressure: NumericFields


class PoolSnapshot(TypedDict):
    pool: NumericFields
    monotonic_seconds: int | float | None
    cpu_seconds: int | float | None
    peak_memory_bytes: int | float | None


class CollectorCounters(TypedDict):
    thread_cpu_seconds: float
    process_cpu_seconds: float
    child_cpu_seconds: float
    peak_memory_bytes: int
    sample_overhead_seconds: float
    pool_reader_cpu_seconds: int | float | None
    pool_reader_peak_memory_bytes: int | float | None
    scheduling_lag_seconds: NotRequired[float]


class ObservationSample(TypedDict):
    captured_at: str
    monotonic_seconds: float
    services: dict[str, ContainerCounters | None]
    postgres: NumericFields
    pool: NumericFields
    errors: list[str]
    collector: NotRequired[CollectorCounters]


class ObservationSummary(TypedDict):
    samples: int
    complete: bool
    services_seen: list[str]
    postgres_samples: int
    pool_samples: int
    expected_pool_workers: int
    pool_missing_samples: int
    pool_error_samples: int
    first_monotonic_seconds: float | None
    last_monotonic_seconds: float | None
    max_sample_gap_seconds: float
    max_sample_overhead_seconds: float
    errors: int
    collector_failed: bool
    sample_interval_seconds: int
    host_capacity: dict[str, int | float | None]
    missing_values: str
    wait_estimate: str


def numeric(value: object) -> int | float | None:
    """Discard all text and non-finite numbers at the output boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def numeric_fields(source: object, names: tuple[str, ...]) -> dict[str, int | float | None]:
    values = source if isinstance(source, dict) else {}
    return {name: numeric(values.get(name)) for name in names}


def container_counters(raw: JsonObject) -> ContainerCounters:
    cpu = json_object(raw.get("cpu_stats") or {})
    memory = json_object(raw.get("memory_stats") or {})
    return {
        "cpu_usage_ns": numeric(json_object(cpu.get("cpu_usage") or {}).get("total_usage")),
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


class PoolReader:
    """Own one bounded remote reader and retain only its latest numeric snapshot."""

    def __init__(self, stack: IsolatedComposeProject, stats: DockerStats, identifier: str) -> None:
        self.stack, self.stats, self.identifier = stack, stats, identifier
        self.process: subprocess.Popen[str] | None = None
        self.pid: int | None = None
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.failed = False
        self.stopping = False
        self.closed = False
        self.latest: PoolSnapshot | None = None
        self.received_at: float | None = None
        self.thread = threading.Thread(target=self._read, name="pool-reader", daemon=True)

    def start(self) -> None:
        program = Path(__file__).with_name("proof_pool_reader.py").read_text(encoding="utf-8")
        self.process = subprocess.Popen(  # nosec B603
            self.stack.compose_command(
                "exec", "-T", "web", "/app/.venv/bin/python", "-u", "-c", program
            ),
            env=self.stack.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.thread.start()
        if not self.ready.wait(timeout=5) or self.failed:
            self.close()
            raise ValueError("The bounded pool reader did not start.")

    def _read(self) -> None:
        try:
            assert self.process is not None and self.process.stdout is not None
            header = self.process.stdout.readline(4097)
            document = json.loads(header)
            pid = document.get("pid") if isinstance(document, dict) else None
            if (
                len(header) > 4096
                or not header.endswith("\n")
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or not 2 <= pid <= 2**31 - 1
                or set(document) != {"pid"}
            ):
                raise ValueError("Invalid pool reader header.")
            self.pid = pid
            while True:
                line = self.process.stdout.readline(4097)
                if not line:
                    if not self.stopping:
                        self.failed = True
                    break
                if len(line) > 4096 or not line.endswith("\n"):
                    raise ValueError("Invalid pool reader sample.")
                raw = json.loads(line)
                if not isinstance(raw, dict) or not isinstance(raw.get("pool"), dict):
                    raise ValueError("Invalid pool reader sample.")
                pool = numeric_fields(
                    raw["pool"], (*POOL_FIELDS, *(f"sampler_{status}" for status in POOL_STATUSES))
                )
                snapshot: PoolSnapshot = {
                    "pool": pool,
                    "monotonic_seconds": numeric(raw.get("monotonic_seconds")),
                    "cpu_seconds": numeric(raw.get("cpu_seconds")),
                    "peak_memory_bytes": numeric(raw.get("peak_memory_bytes")),
                }
                if any(
                    snapshot[name] is None
                    for name in ("monotonic_seconds", "cpu_seconds", "peak_memory_bytes")
                ):
                    raise ValueError("Invalid pool reader counters.")
                with self.lock:
                    self.latest, self.received_at = snapshot, time.monotonic()
                self.ready.set()
        except (ValueError, OSError, TypeError):
            self.failed = True
        finally:
            self.ready.set()

    def snapshot(self) -> PoolSnapshot:
        with self.lock:
            if (
                self.failed
                or self.latest is None
                or self.received_at is None
                or time.monotonic() - self.received_at > 2
            ):
                raise ValueError("The pool reader snapshot is missing or stale.")
            return self.latest.copy()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopping = True
        if self.pid is not None:
            try:
                self.stats.exec(self.identifier, ("/bin/kill", "-TERM", str(self.pid)))
            except (ValueError, OSError):
                self.failed = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            if self.thread.ident is not None:
                self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.failed = True
            if self.process.stdout is not None and not self.thread.is_alive():
                self.process.stdout.close()


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
        self.capacity: NumericFields = {"host_cpu_count": os.cpu_count()}
        self._output: TextIO | None = None
        self.executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="load-counter")
        self.max_sample_overhead_seconds = 0.0
        self.pool_reader: PoolReader | None = None

    def __enter__(self) -> LoadObservations:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.path.open("x", encoding="utf-8")
        try:
            try:
                self.capacity.update(numeric_fields(self.stats.get("/info"), ("NCPU", "MemTotal")))
            except InterruptedError:
                raise
            except (ValueError, OSError):
                self.capacity.update(NCPU=None, MemTotal=None)
            if "web" not in self.identifiers:
                self._inventory()
            self.pool_reader = PoolReader(self.stack, self.stats, self.identifiers["web"])
            self.pool_reader.start()
            self.thread.start()
        except BaseException as error:
            try:
                if self.pool_reader is not None:
                    self.pool_reader.close()
            finally:
                self.executor.shutdown(wait=True, cancel_futures=True)
                self._output.close()
            if isinstance(error, (InterruptedError, KeyboardInterrupt)):
                raise
            if isinstance(error, (ValueError, OSError, KeyError)):
                raise ValueError("The bounded pool reader did not start.") from None
            raise
        return self

    def __exit__(self, *arguments: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.failed = True
            self.errors += 1
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.pool_reader is not None:
            self.pool_reader.close()
            if self.pool_reader.failed:
                self.failed = True
                self.errors += 1
        if self._output is not None:
            self._output.close()

    def summary(self) -> ObservationSummary:
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

    def _inventory(self) -> None:
        query = urlencode(
            {
                "all": "true",
                "filters": json.dumps(
                    {"label": [f"com.docker.compose.project={self.stack.project_name}"]}
                ),
            }
        )
        for row in self.stats.get_list(f"/containers/json?{query}"):
            name = json_object(row.get("Labels") or {}).get("com.docker.compose.service")
            identifier = json_string(row.get("Id", ""))
            if (
                isinstance(name, str)
                and name in SERVICES
                and row.get("State") == "running"
                and re.fullmatch(r"[a-f0-9]{12,64}", identifier)
            ):
                self.identifiers[name] = identifier

    def _service_value(self, identifier: str) -> JsonValue:
        return json_value(
            self.stats.get(f"/containers/{quote(identifier)}/stats?stream=false&one-shot=true")
        )

    def _postgres_value(self, identifier: str) -> JsonValue:
        return self.stats.exec(identifier, POSTGRES_COMMAND, environment=POSTGRES_ENVIRONMENT)

    def _sample(self) -> ObservationSample:
        started = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        sample: ObservationSample = {
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
                self._inventory()
        except (ValueError, OSError, KeyError, TypeError):
            errors.append("container_inventory_unavailable")
        jobs: dict[str, Future[JsonValue]] = {
            name: self.executor.submit(self._service_value, identifier)
            for name, identifier in self.identifiers.items()
        }
        if "postgres" in self.identifiers:
            jobs["postgres_aggregate"] = self.executor.submit(
                self._postgres_value, self.identifiers["postgres"]
            )
        _, pending = wait(jobs.values(), timeout=max(0.0, started + 1 - time.monotonic()))
        if pending:
            # Stop the collector after a missed deadline. No old job can queue into a later sample.
            self.failed = True
            self.stop_event.set()
            errors.append("sample_deadline_exceeded")
            for future in pending:
                future.cancel()
        services: dict[str, ContainerCounters | None] = {name: None for name in SERVICES}
        for name, future in jobs.items():
            if future in pending:
                errors.append(f"{name}_counters_unavailable")
                continue
            try:
                value = future.result()
                if name in SERVICES:
                    services[name] = container_counters(json_object(value))
                    self.services_seen.add(name)
                elif name == "postgres_aggregate":
                    postgres = numeric_fields(json.loads(json_string(value)), DATABASE_FIELDS)
                    sample["postgres"] = postgres
                    if all(item is not None for item in postgres.values()):
                        self.postgres_samples += 1
            except (ValueError, OSError, KeyError, TypeError):
                errors.append(f"{name}_counters_unavailable")
        reader: PoolSnapshot | None = None
        try:
            if self.pool_reader is None:
                raise ValueError("The pool reader is missing.")
            reader = self.pool_reader.snapshot()
            pool = reader["pool"]
            sample["pool"] = pool
            if pool.get("sampler_ok") == EXPECTED_POOL_WORKERS:
                self.pool_samples += 1
            if (numeric(pool.get("sampler_missing")) or 0) > 0:
                self.pool_missing_samples += 1
            if (numeric(pool.get("sampler_error")) or 0) > 0:
                self.pool_error_samples += 1
                errors.append("pool_sampler_error")
        except (ValueError, KeyError, TypeError):
            errors.append("pool_counters_unavailable")
            self.failed = True
            self.stop_event.set()
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
            "pool_reader_cpu_seconds": reader.get("cpu_seconds") if reader is not None else None,
            "pool_reader_peak_memory_bytes": reader.get("peak_memory_bytes")
            if reader is not None
            else None,
        }
        return sample

    def _run(self) -> None:
        due = time.monotonic()
        assert self._output is not None
        try:
            while not self.stop_event.is_set():
                sample = self._sample()
                captured = float(str(sample["monotonic_seconds"]))
                if self.first_monotonic is None:
                    self.first_monotonic = captured
                if self.last_monotonic is not None:
                    self.max_gap_seconds = max(self.max_gap_seconds, captured - self.last_monotonic)
                self.last_monotonic = captured
                sample["collector"]["scheduling_lag_seconds"] = max(
                    0.0,
                    sample["monotonic_seconds"] - due,
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
