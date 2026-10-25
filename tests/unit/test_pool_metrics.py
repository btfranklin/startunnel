"""The proof sampler reports pool snapshots without opening the pool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from django.conf import settings
from prometheus_client import multiprocess

from core import pool_metrics
from core.metrics import (
    DATABASE_POOL_ERRORS,
    DATABASE_POOL_QUEUED,
    DATABASE_POOL_REQUESTS_WAIT_MS,
    DATABASE_POOL_SAMPLER_STATUS,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_WAITING,
)


def metric_value(metric: object, name: str, labels: dict[str, str] | None = None) -> float:
    return next(
        float(sample.value)
        for family in metric.collect()  # type: ignore[attr-defined]
        for sample in family.samples
        if sample.name == name and sample.labels == (labels or {})
    )


def test_missing_pool_reports_zeroes_and_missing_status() -> None:
    assert pool_metrics.sample_pool_stats(None) == "missing"
    assert metric_value(DATABASE_POOL_SIZE, "startunnel_database_pool_size") == 0
    assert metric_value(DATABASE_POOL_WAITING, "startunnel_database_pool_waiting") == 0
    assert metric_value(DATABASE_POOL_QUEUED, "startunnel_database_pool_queued") == 0
    assert metric_value(DATABASE_POOL_ERRORS, "startunnel_database_pool_errors") == 0
    assert (
        metric_value(
            DATABASE_POOL_SAMPLER_STATUS,
            "startunnel_database_pool_sampler_status",
            {"status": "missing"},
        )
        == 1
    )


def test_pool_snapshot_reports_stats_and_counter_reset() -> None:
    pool_metrics.sample_pool_stats(
        {
            "pool_size": 5,
            "pool_available": 2,
            "requests_waiting": 3,
            "requests_queued": 9,
            "requests_wait_ms": 125,
            "requests_errors": 4,
            "connections_errors": 2,
            "connections_lost": 1,
        }
    )
    assert metric_value(DATABASE_POOL_SIZE, "startunnel_database_pool_size") == 5
    assert metric_value(DATABASE_POOL_WAITING, "startunnel_database_pool_waiting") == 3
    assert metric_value(DATABASE_POOL_QUEUED, "startunnel_database_pool_queued") == 9
    assert (
        metric_value(
            DATABASE_POOL_REQUESTS_WAIT_MS,
            "startunnel_database_pool_requests_wait_ms",
        )
        == 125
    )
    assert metric_value(DATABASE_POOL_ERRORS, "startunnel_database_pool_errors") == 7

    # Stats are cumulative snapshots. If psycopg resets them, report the new
    # value rather than retaining a stale delta from the previous sample.
    pool_metrics.sample_pool_stats({"requests_errors": 1})
    assert metric_value(DATABASE_POOL_ERRORS, "startunnel_database_pool_errors") == 1
    assert metric_value(DATABASE_POOL_SIZE, "startunnel_database_pool_size") == 0


def test_stats_failure_has_a_fixed_error_status() -> None:
    pool_metrics.sample_pool_error()
    assert (
        metric_value(
            DATABASE_POOL_SAMPLER_STATUS,
            "startunnel_database_pool_sampler_status",
            {"status": "error"},
        )
        == 1
    )
    assert (
        metric_value(
            DATABASE_POOL_SAMPLER_STATUS,
            "startunnel_database_pool_sampler_status",
            {"status": "ok"},
        )
        == 0
    )


def test_initialized_pool_lookup_does_not_create_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    class Wrapper:
        alias = "default"
        _connection_pools: dict[str, object] = {}

        @property
        def pool(self) -> object:
            raise AssertionError("lazy pool property must not be read")

    monkeypatch.setattr(pool_metrics, "connections", {"default": Wrapper()})
    assert pool_metrics._initialized_default_pool() is None


def test_stats_sampler_reads_initialized_pool_and_marks_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pool:
        def get_stats(self) -> dict[str, int]:
            return {"pool_size": 4}

    class Wrapper:
        alias = "default"
        _connection_pools: dict[str, object] = {"default": Pool()}

    monkeypatch.setattr(pool_metrics, "connections", {"default": Wrapper()})
    pool_metrics.sample_pool_once()
    assert metric_value(DATABASE_POOL_SIZE, "startunnel_database_pool_size") == 4

    class BrokenPool:
        def get_stats(self) -> dict[str, int]:
            raise RuntimeError("private details")

    Wrapper._connection_pools = {"default": BrokenPool()}
    pool_metrics.sample_pool_once()
    assert (
        metric_value(
            DATABASE_POOL_SAMPLER_STATUS,
            "startunnel_database_pool_sampler_status",
            {"status": "error"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sampler_lifecycle_is_opt_in_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    sampler = pool_metrics.PoolMetricsSampler()
    marked: list[int] = []
    monkeypatch.setattr(settings, "STARTUNNEL_PROOF_METRICS", False)
    await sampler.start()
    assert sampler._task is None

    monkeypatch.setattr(settings, "STARTUNNEL_PROOF_METRICS", True)
    monkeypatch.setattr(pool_metrics, "_sample_forever", AsyncMock())
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus-test")
    monkeypatch.setattr(multiprocess, "mark_process_dead", marked.append)
    await sampler.start()
    assert sampler._task is not None
    await sampler.stop()
    assert sampler._task is None
    assert marked == [pool_metrics.os.getpid()]
