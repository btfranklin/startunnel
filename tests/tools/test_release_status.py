"""Release timing uses observed job boundaries and preserves attempt history."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from scripts.release_status import job_timings, render_summary


def job(
    name: str,
    start: int,
    finish: int | None,
    *,
    attempt: int = 1,
    conclusion: str | None = "success",
    created: int | None = None,
) -> dict[str, Any]:
    origin = datetime(2026, 10, 24, tzinfo=UTC)

    def stamp(seconds: int | None) -> str | None:
        return (origin + timedelta(seconds=seconds)).isoformat() if seconds is not None else None

    return {
        "id": start + 1,
        "name": name,
        "started_at": stamp(start),
        "completed_at": stamp(finish),
        "created_at": stamp(created),
        "run_attempt": attempt,
        "status": "completed" if finish is not None else "in_progress",
        "conclusion": conclusion,
    }


def test_parallel_jobs_are_not_added_as_serial_time() -> None:
    result = job_timings(
        [job("build", 0, 10), job("system", 11, 40), job("load", 12, 80), job("evidence", 82, 85)],
        {"system": ["build"], "load": ["build"], "evidence": ["system", "load"]},
    )
    assert result["elapsed_seconds"] == 85
    assert result["longest_observed_dependency_path"] == ["build", "load", "evidence"]
    assert result["jobs"]["evidence"]["dependency_ready_to_start_seconds"] == 2
    assert result["jobs"]["system"]["queue_seconds"] is None


def test_rerun_uses_latest_proof_and_keeps_successful_earlier_jobs() -> None:
    result = job_timings(
        [
            job("build", 0, 10),
            job("load", 11, 20, conclusion="failure"),
            job("load", 30, 40, attempt=2),
            job("build", 30, 30, attempt=2, conclusion="skipped"),
        ],
        {"load": ["build"]},
    )
    assert result["jobs"]["build"]["attempt"] == 1
    assert result["jobs"]["load"]["attempt"] == 2
    assert result["elapsed_seconds"] == 40


def test_queue_time_requires_its_own_boundary_and_pending_job_is_incomplete() -> None:
    result = job_timings([job("load", 5, None, created=2, conclusion=None)], {})
    assert result["jobs"]["load"]["queue_seconds"] == 3
    assert result["jobs"]["load"]["duration_seconds"] is None
    assert result["elapsed_seconds"] is None
    assert not result["complete"]


def test_dependency_cycle_fails_instead_of_inventing_a_path() -> None:
    with pytest.raises(ValueError, match="cycle"):
        job_timings(
            [job("build", 0, 10), job("load", 1, 20)], {"build": ["load"], "load": ["build"]}
        )


def test_summary_names_actual_artifacts_and_selected_attempt() -> None:
    status = {
        "source_commit": "a" * 40,
        "candidate": {
            "image": "ghcr.io/example@sha256:" + "b" * 64,
            "source_validation_run_id": 1,
            "proofs": {"load": {"attempt": 2, "artifact_id": 123}},
        },
        "workflows": [
            {
                "workflow": "release-candidate.yml",
                "url": "https://example.invalid/run",
                "status": "completed",
                "conclusion": "success",
                "attempt": 2,
                "timing": job_timings([job("load", 5, 10)], {}),
                "artifacts": [
                    {"name": "proof-load-10-1", "id": 100, "expired": False},
                    {"name": "proof-load-10-2", "id": 123, "expired": False},
                ],
            }
        ],
    }
    summary = render_summary(status)
    assert "proof-load-10-1" in summary
    assert "proof-load-10-2" in summary
    assert "selected attempt 2; artifact 123" in summary


def test_longest_path_can_finish_before_a_later_short_job() -> None:
    result = job_timings([job("early", 0, 90), job("late", 100, 110)], {})
    assert result["longest_observed_dependency_path"] == ["early"]
    assert result["longest_path_seconds"] == 90
    assert result["elapsed_seconds"] == 110
