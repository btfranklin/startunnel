"""Check that a disposable lock diagnostic failed for an observed lock wait."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .proof_evidence import LOAD_CONFIGURATION
else:
    from proof_evidence import LOAD_CONFIGURATION  # type: ignore[import-not-found,no-redef]


def validate(directory: Path, project: str) -> dict[str, int | float]:
    run = json.loads((directory / f"load-run-{project}.json").read_text())
    workload = json.loads((directory / run["workload_report"]).read_text())
    if (
        run.get("status") != "failed"
        or run.get("release_evidence_eligible") is not False
        or run.get("disposable_volumes_removed") is not True
        or workload.get("fixture_cleanup_complete") is not True
        or any(
            workload.get("configuration", {}).get(key) != value
            for key, value in LOAD_CONFIGURATION.items()
        )
        or run.get("observations", {}).get("complete") is not True
        or run.get("observations", {}).get("collector_failed") is not False
        or not 1 <= run.get("diagnostic_lock_seconds", 0) <= 60
        or workload.get("passed") is not False
        or workload.get("results", {}).get("expected_sends") != 15000
        or not workload.get("results", {}).get("failures_by_status")
    ):
        raise ValueError("The lock diagnostic did not produce a complete expected failure.")
    peak_waiters = 0
    peak_pool_wait_ms = 0.0
    with (directory / run["observations_file"]).open() as stream:
        for line in stream:
            sample = json.loads(line)
            peak_waiters = max(peak_waiters, sample.get("postgres", {}).get("lock_waiters") or 0)
            peak_pool_wait_ms = max(
                peak_pool_wait_ms, sample.get("pool", {}).get("requests_wait_ms") or 0
            )
    if peak_waiters <= 0:
        raise ValueError("The diagnostic did not observe a database lock wait.")
    return {"peak_lock_waiters": peak_waiters, "peak_pool_wait_ms": peak_pool_wait_ms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--directory", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    result = validate(args.directory, args.project_name)
    print("The controlled load diagnostic produced the expected failure: " + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
