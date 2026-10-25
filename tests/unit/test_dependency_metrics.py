"""Dependency failures retain only fixed cause categories."""

from __future__ import annotations

import pytest
from django.db import OperationalError
from prometheus_client import REGISTRY
from psycopg_pool import PoolTimeout, TooManyRequests

from core.metrics import (
    DATABASE_DEPENDENCY_FAILURES,
    DEPENDENCY_FAILURE_REASONS,
    record_dependency_failure,
)


class StateError(Exception):
    def __init__(self, state: str) -> None:
        self.sqlstate = state
        super().__init__("private-query-and-credential")


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        (PoolTimeout("private"), "pool_timeout"),
        (TooManyRequests("private"), "pool_queue_full"),
        (StateError("40P01"), "deadlock"),
        (StateError("40001"), "serialization_failure"),
        (StateError("08006"), "connection_failure"),
        (StateError("57014"), "query_cancelled"),
        (StateError("55P03"), "lock_unavailable"),
        (StateError("private"), "other"),
        (Exception("private"), "other"),
    ],
)
def test_wrapped_failure_increments_only_its_fixed_category(cause: Exception, reason: str) -> None:
    outer = OperationalError("private")
    outer.__cause__ = cause
    before = (
        REGISTRY.get_sample_value(
            "startunnel_database_dependency_failures_total", {"reason": reason}
        )
        or 0
    )
    record_dependency_failure(outer)
    assert (
        REGISTRY.get_sample_value(
            "startunnel_database_dependency_failures_total", {"reason": reason}
        )
        == before + 1
    )
    labels = [
        sample.labels
        for metric in DATABASE_DEPENDENCY_FAILURES.collect()
        for sample in metric.samples
    ]
    assert all(label["reason"] in DEPENDENCY_FAILURE_REASONS for label in labels)
    assert "private" not in str(labels)


def test_cyclic_exception_chain_is_bounded_and_unknown() -> None:
    error = Exception("private")
    error.__cause__ = error
    before = (
        REGISTRY.get_sample_value(
            "startunnel_database_dependency_failures_total", {"reason": "other"}
        )
        or 0
    )
    record_dependency_failure(error)
    assert (
        REGISTRY.get_sample_value(
            "startunnel_database_dependency_failures_total", {"reason": "other"}
        )
        == before + 1
    )
