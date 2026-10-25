"""Record bounded proof phases with wall and monotonic clocks."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import NotRequired, TypedDict


class PhaseRecord(TypedDict):
    phase: str
    started_at: str
    started_monotonic_seconds: float
    status: str
    finished_at: NotRequired[str]
    finished_monotonic_seconds: NotRequired[float]
    elapsed_seconds: NotRequired[float]


class PhaseTimings:
    """Keep one small record for each fixed proof phase, including failures."""

    def __init__(self) -> None:
        self.records: list[PhaseRecord] = []

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = time.monotonic()
        record: PhaseRecord = {
            "phase": name,
            "started_at": datetime.now(UTC).isoformat(),
            "started_monotonic_seconds": start,
            "status": "failed",
        }
        self.records.append(record)
        try:
            yield
        except BaseException:
            raise
        else:
            record["status"] = "passed"
        finally:
            end = time.monotonic()
            record["finished_at"] = datetime.now(UTC).isoformat()
            record["finished_monotonic_seconds"] = end
            record["elapsed_seconds"] = max(0.0, end - start)
