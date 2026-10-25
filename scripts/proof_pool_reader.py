"""Read fixed numeric pool counters from worker files for at most 900 seconds."""

from __future__ import annotations

import json
import math
import os
import resource
import sys
import time

from prometheus_client import CollectorRegistry, multiprocess

POOL_FIELDS = (
    "size",
    "available",
    "waiting",
    "queued",
    "errors",
    "request_errors",
    "connection_errors",
    "connections_lost",
    "requests_wait_ms",
)
POOL_STATUSES = ("ok", "missing", "error")
DEPENDENCY_REASONS = (
    "pool_timeout",
    "pool_queue_full",
    "deadlock",
    "serialization_failure",
    "connection_failure",
    "query_cancelled",
    "lock_unavailable",
    "other",
)


def read_pool(registry: CollectorRegistry) -> dict[str, int | float | None]:
    """Read only the fixed numeric pool counters, with no dynamic label output."""
    values: dict[str, int | float | None] = {
        **{name: None for name in POOL_FIELDS},
        **{f"sampler_{status}": None for status in POOL_STATUSES},
        **{f"dependency_{reason}": None for reason in DEPENDENCY_REASONS},
    }
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == "startunnel_database_dependency_failures_total":
                if math.isfinite(sample.value) and sample.value >= 0:
                    for reason in DEPENDENCY_REASONS:
                        if sample.labels == {"reason": reason}:
                            values[f"dependency_{reason}"] = sample.value
                continue
            if not sample.name.startswith("startunnel_database_pool_"):
                continue
            name = sample.name.removeprefix("startunnel_database_pool_")
            value = sample.value
            if not math.isfinite(value) or value < 0:
                continue
            if name in POOL_FIELDS and not sample.labels:
                values[name] = value
            elif name == "sampler_status":
                for status in POOL_STATUSES:
                    if sample.labels == {"status": status}:
                        values[f"sampler_{status}"] = value
    return values


def run(duration_seconds: float = 900) -> None:
    """Write one bounded JSON line each second and never retain sample history."""
    if not 0 < duration_seconds <= 900:
        raise ValueError("The pool reader duration must be at most 900 seconds.")
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    print(json.dumps({"pid": os.getpid()}), flush=True)
    due = time.monotonic()
    deadline = due + duration_seconds
    while time.monotonic() < deadline:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        print(
            json.dumps(
                {
                    "pool": read_pool(registry),
                    "monotonic_seconds": time.monotonic(),
                    "cpu_seconds": time.process_time(),
                    "peak_memory_bytes": usage.ru_maxrss
                    * (1 if sys.platform == "darwin" else 1024),
                },
                allow_nan=False,
            ),
            flush=True,
        )
        due += 1
        time.sleep(max(0.0, min(due, deadline) - time.monotonic()))


if __name__ == "__main__":
    run()
