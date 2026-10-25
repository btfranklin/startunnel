"""Opt-in, low-cost sampling of the initialized Django PostgreSQL pool."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Protocol, cast

from django.conf import settings
from django.db import connections
from prometheus_client import multiprocess

from .metrics import (
    DATABASE_POOL_AVAILABLE,
    DATABASE_POOL_CONNECTION_ERRORS,
    DATABASE_POOL_CONNECTIONS_LOST,
    DATABASE_POOL_ERRORS,
    DATABASE_POOL_QUEUED,
    DATABASE_POOL_REQUEST_ERRORS,
    DATABASE_POOL_REQUESTS_WAIT_MS,
    DATABASE_POOL_SAMPLER_STATUS,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_WAITING,
)

logger = logging.getLogger(__name__)

_POOL_STAT_KEYS = {
    "pool_size": DATABASE_POOL_SIZE,
    "pool_available": DATABASE_POOL_AVAILABLE,
    "requests_waiting": DATABASE_POOL_WAITING,
    "requests_queued": DATABASE_POOL_QUEUED,
    "requests_wait_ms": DATABASE_POOL_REQUESTS_WAIT_MS,
    "requests_errors": DATABASE_POOL_REQUEST_ERRORS,
    "connections_errors": DATABASE_POOL_CONNECTION_ERRORS,
    "connections_lost": DATABASE_POOL_CONNECTIONS_LOST,
}
_POOL_ERROR_KEYS = ("requests_errors", "connections_errors", "connections_lost")


class _PoolStats(Protocol):
    def get_stats(self) -> dict[str, int]: ...


def _initialized_default_pool() -> _PoolStats | None:
    """Return an existing pool without invoking Django's lazy pool property."""

    wrapper = connections["default"]
    # Django's ``pool`` property constructs an unopened pool when first read.
    # The class cache lets this sampler observe only pools already used by the app.
    pools = getattr(type(wrapper), "_connection_pools", {})
    return cast(_PoolStats | None, pools.get(wrapper.alias))


def sample_pool_stats(stats: Mapping[str, int] | None) -> str:
    """Publish one pool snapshot and return its fixed status category."""

    if stats is None:
        for gauge in _POOL_STAT_KEYS.values():
            gauge.set(0)
        DATABASE_POOL_ERRORS.set(0)
        status = "missing"
    else:
        for key, gauge in _POOL_STAT_KEYS.items():
            gauge.set(max(0, int(stats.get(key, 0))))
        DATABASE_POOL_ERRORS.set(sum(max(0, int(stats.get(key, 0))) for key in _POOL_ERROR_KEYS))
        status = "ok"
    DATABASE_POOL_SAMPLER_STATUS.labels(status="ok").set(int(status == "ok"))
    DATABASE_POOL_SAMPLER_STATUS.labels(status="missing").set(int(status == "missing"))
    DATABASE_POOL_SAMPLER_STATUS.labels(status="error").set(0)
    return status


def sample_pool_error() -> None:
    """Mark one failed stats read without exposing exception data as labels."""

    DATABASE_POOL_SAMPLER_STATUS.labels(status="ok").set(0)
    DATABASE_POOL_SAMPLER_STATUS.labels(status="missing").set(0)
    DATABASE_POOL_SAMPLER_STATUS.labels(status="error").set(1)


def sample_pool_once() -> None:
    """Read one stats snapshot and report missing or failed reads explicitly."""

    try:
        pool = _initialized_default_pool()
        sample_pool_stats(None if pool is None else pool.get_stats())
    except Exception:
        sample_pool_error()
        logger.warning("Database pool stats sampling failed.")


async def _sample_forever() -> None:
    while True:
        try:
            sample_pool_once()
        except asyncio.CancelledError:
            raise
        await asyncio.sleep(1)


class PoolMetricsSampler:
    """Own one optional process-local stats sampling task."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if settings.STARTUNNEL_PROOF_METRICS and self._task is None:
            self._task = asyncio.create_task(_sample_forever(), name="pool-metrics-sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
                if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
                    # The Prometheus process cleanup function has no type annotations.
                    multiprocess.mark_process_dead(os.getpid())  # type: ignore[no-untyped-call]


pool_metrics_sampler = PoolMetricsSampler()
