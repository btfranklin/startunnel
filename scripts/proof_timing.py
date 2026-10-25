"""Record bounded proof phases with wall and monotonic clocks."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime


class PhaseTimings:
    """Keep one small record for each fixed proof phase, including failures."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = time.monotonic()
        record: dict[str, object] = {
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
            record.update(
                finished_at=datetime.now(UTC).isoformat(),
                finished_monotonic_seconds=end,
                elapsed_seconds=max(0.0, end - start),
            )
