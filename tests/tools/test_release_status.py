"""Release timing uses observed job boundaries and preserves attempt history."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts import release_status
from scripts.json_data import JsonObject, json_object
from scripts.release_candidate import CandidateData
from scripts.release_status import ReleaseStatus, job_timings, render_summary
from tests.json_values import json_at


def job(
    name: str,
    start: int,
    finish: int | None,
    *,
    attempt: int = 1,
    conclusion: str | None = "success",
    created: int | None = None,
) -> JsonObject:
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
    assert json_at(result, "jobs", "evidence", "dependency_ready_to_start_seconds") == 2
    assert json_at(result, "jobs", "system", "queue_seconds") is None


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
    assert json_at(result, "jobs", "build", "attempt") == 1
    assert json_at(result, "jobs", "load", "attempt") == 2
    assert result["elapsed_seconds"] == 40


def test_queue_time_requires_its_own_boundary_and_pending_job_is_incomplete() -> None:
    result = job_timings([job("load", 5, None, created=2, conclusion=None)], {})
    assert json_at(result, "jobs", "load", "queue_seconds") == 3
    assert json_at(result, "jobs", "load", "duration_seconds") is None
    assert result["elapsed_seconds"] is None
    assert not result["complete"]


def test_dependency_cycle_fails_instead_of_inventing_a_path() -> None:
    with pytest.raises(ValueError, match="cycle"):
        job_timings(
            [job("build", 0, 10), job("load", 1, 20)], {"build": ["load"], "load": ["build"]}
        )


def test_summary_names_actual_artifacts_and_selected_attempt() -> None:
    status: ReleaseStatus = {
        "source_commit": "a" * 40,
        "candidate": {
            "image": "ghcr.io/example@sha256:" + "b" * 64,
            "version": "v0.4.0",
            "source_commit": "a" * 40,
            "platform": "linux/amd64",
            "source_validation_run_id": 1,
            "proofs": {
                "load": {
                    "kind": "load",
                    "run_id": 10,
                    "attempt": 2,
                    "artifact_id": 123,
                    "artifact_digest": None,
                    "manifest": "proofs/load/2/proof-load.json",
                    "sha256": "c" * 64,
                    "status": "passed",
                }
            },
        },
        "workflows": [
            {
                "workflow": "release-candidate.yml",
                "run_id": 10,
                "source_commit": "a" * 40,
                "queue_seconds": None,
                "url": "https://example.invalid/run",
                "status": "completed",
                "conclusion": "success",
                "attempt": 2,
                "timing": job_timings([job("load", 5, 10)], {}),
                "artifacts": [
                    {
                        "name": "proof-load-10-1",
                        "id": 100,
                        "expired": False,
                        "digest": None,
                        "created_at": None,
                        "expires_at": None,
                    },
                    {
                        "name": "proof-load-10-2",
                        "id": 123,
                        "expired": False,
                        "digest": None,
                        "created_at": None,
                        "expires_at": None,
                    },
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


def test_rerun_gap_is_not_reported_as_workflow_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    def api(repository: str, path: str, *, paginate: bool = False) -> object:
        if "/jobs?" in path:
            return [{"jobs": []}]
        if "/artifacts?" in path:
            return [{"artifacts": []}]
        return {
            "head_sha": "a" * 40,
            "html_url": "https://example.invalid/run",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
            "created_at": "2026-10-24T00:00:00Z",
            "run_started_at": "2026-10-24T01:00:00Z",
        }

    monkeypatch.setattr(release_status, "github_api", api)
    assert release_status.workflow_status("owner/repo", 42)["queue_seconds"] is None


@pytest.mark.parametrize("promotion", [False, True])
def test_dispatch_source_is_separate_only_for_promotion_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    promotion: bool,
) -> None:
    tagged_source = "a" * 40
    dispatch_source = "b" * 40
    output = tmp_path / "status.json"
    workflow: release_status.WorkflowStatus = {
        "run_id": 42,
        "source_commit": dispatch_source,
        "url": "https://example.invalid/run/42",
        "workflow": ".github/workflows/release.yml",
        "status": "completed",
        "conclusion": "success",
        "attempt": 1,
        "queue_seconds": None,
        "timing": job_timings([], {}),
        "artifacts": [],
    }
    monkeypatch.setattr(
        release_status,
        "inspect_readiness",
        lambda *args, **kwargs: readiness_for(status_fixture()),
    )
    monkeypatch.setattr(release_status, "git_commit", lambda: tagged_source)
    monkeypatch.setattr(release_status, "workflow_status", lambda *args: workflow)
    monkeypatch.setattr(release_status, "github_api", lambda *args, **kwargs: {"workflow_runs": []})
    option = "--promotion-run-id" if promotion else "--run-id"
    monkeypatch.setattr(sys, "argv", ["release-status", option, "42", "--output", str(output)])
    if promotion:
        assert release_status.main() == 0
        report = json_object(json.loads(output.read_text()))
        assert report["source_commit"] == tagged_source
        assert json_at(report, "promotion", "source_commit") == dispatch_source
        assert report["workflows"] == []
        summary = capsys.readouterr().out
        assert f"Release source: `{tagged_source}`" in summary
        assert f"Promotion dispatch source: `{dispatch_source}`" in summary
    else:
        with pytest.raises(ValueError, match="different source commit"):
            release_status.main()
        assert not output.exists()


def test_promotion_timing_rejects_an_unrelated_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow: release_status.WorkflowStatus = {
        "run_id": 42,
        "source_commit": "a" * 40,
        "url": "https://example.invalid/run/42",
        "workflow": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "attempt": 1,
        "queue_seconds": None,
        "timing": job_timings([], {}),
        "artifacts": [],
    }
    monkeypatch.setattr(release_status, "git_commit", lambda: "a" * 40)
    monkeypatch.setattr(release_status, "workflow_status", lambda *args: workflow)
    monkeypatch.setattr(release_status, "github_api", lambda *args, **kwargs: {"workflow_runs": []})
    monkeypatch.setattr(sys, "argv", ["release-status", "--promotion-run-id", "42"])
    with pytest.raises(ValueError, match="requires the release workflow"):
        release_status.main()


def status_fixture(*, candidate_result: str | None = None) -> ReleaseStatus:
    def workflow(run_id: int, name: str, result: str) -> release_status.WorkflowStatus:
        return {
            "run_id": run_id,
            "source_commit": "a" * 40,
            "url": f"https://example.invalid/run/{run_id}",
            "workflow": f".github/workflows/{name}",
            "branch": "main",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": result,
            "attempt": 1,
            "queue_seconds": None,
            "timing": job_timings([], {}),
            "artifacts": [],
        }

    status: ReleaseStatus = {
        "source_commit": "a" * 40,
        "workflows": [workflow(1, "ci.yml", "success")],
    }
    if candidate_result:
        candidate_run = workflow(2, "release-candidate.yml", candidate_result)
        candidate_run["artifacts"] = [
            {
                "id": 20,
                "name": "release-candidate-2-1",
                "digest": None,
                "expired": False,
                "created_at": None,
                "expires_at": None,
            }
        ]
        status["workflows"].append(candidate_run)
    return status


def readiness_for(
    status: ReleaseStatus, *, verified: bool = False
) -> release_status.ReleaseReadiness:
    return release_status.release_readiness(
        status,
        version="v0.4.1",
        branch="main",
        source_clean=True,
        tag_exists=False,
        evidence_verified=verified,
    )


def test_green_source_can_start_candidate_but_cannot_promote() -> None:
    result = readiness_for(status_fixture())
    assert result["can_start_candidate"]
    assert not result["can_promote"]
    assert result["promotion_blockers"]


@pytest.mark.parametrize("condition", ["dirty", "branch", "tag", "failed_ci", "other_source", "pr"])
def test_readiness_reports_source_and_version_blockers(condition: str) -> None:
    status = status_fixture()
    source = status["workflows"][0]
    if condition == "failed_ci":
        source["conclusion"] = "failure"
    if condition == "other_source":
        source["source_commit"] = "b" * 40
    if condition == "pr":
        source["event"] = "pull_request"
    result = release_status.release_readiness(
        status,
        version="v0.4.1",
        branch="feature" if condition == "branch" else "main",
        source_clean=condition != "dirty",
        tag_exists=condition == "tag",
        evidence_verified=True,
    )
    assert not result["can_start_candidate"]
    assert not result["can_promote"]
    assert result["candidate_blockers"]


def test_only_successful_manual_candidate_with_verified_retained_evidence_can_promote() -> None:
    status = status_fixture(candidate_result="success")
    assert not readiness_for(status)["can_promote"]
    result = readiness_for(status, verified=True)
    assert result["can_promote"]
    assert not result["can_start_candidate"]
    assert "Can promote: yes." in render_summary({**status, "readiness": result})
    status["workflows"][1]["event"] = "push"
    assert not readiness_for(status, verified=True)["can_promote"]


@pytest.mark.parametrize("condition", ["expired", "missing", "wrong_source_ci"])
def test_local_verified_files_do_not_replace_retained_artifacts_or_source_ci(
    condition: str,
) -> None:
    status = status_fixture(candidate_result="success")
    if condition == "expired":
        status["workflows"][1]["artifacts"][0]["expired"] = True
    elif condition == "missing":
        status["workflows"][1]["artifacts"] = []
    else:
        status["candidate"] = {
            "source_commit": "a" * 40,
            "version": "v0.4.1",
            "platform": "linux/amd64",
            "image": "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64,
            "source_validation_run_id": 99,
        }
    assert not readiness_for(status, verified=True)["can_promote"]


@pytest.mark.parametrize("running", [False, True])
def test_failed_or_pending_candidate_requires_inspection_before_another_run(running: bool) -> None:
    status = status_fixture(candidate_result="failure")
    if running:
        status["workflows"][1]["status"] = "in_progress"
    result = readiness_for(status)
    assert not result["can_start_candidate"]
    assert not result["can_promote"]
    assert any(
        ("Wait" if running else "Inspect") in reason for reason in result["candidate_blockers"]
    )


def test_status_downloads_complete_evidence_before_reporting_promotion_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = status_fixture(candidate_result="success")
    workflow = status["workflows"][1]
    candidate: CandidateData = {
        "source_commit": "a" * 40,
        "version": "v0.4.1",
        "platform": "linux/amd64",
        "image": "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64,
        "source_validation_run_id": 1,
    }
    downloads: list[str] = []

    def download(repository: str, run_id: int, artifact: object, directory: Path) -> None:
        assert run_id == 2
        downloads.append(str(json_object(artifact)["name"]))

    def verify(directory: Path, *, commit: str, version: str) -> CandidateData:
        assert downloads == ["release-candidate-2-1"]
        assert commit == "a" * 40 and version == "v0.4.1"
        return candidate

    monkeypatch.setattr(release_status, "_download", download)
    monkeypatch.setattr(release_status, "verify_evidence", verify)
    monkeypatch.setattr(release_status, "candidate_version", lambda: "v0.4.1")
    assert release_status.candidate_from_workflow("owner/repo", workflow, commit="a" * 40) == (
        candidate,
        True,
    )

    def corrupt(directory: Path, *, commit: str, version: str) -> CandidateData:
        raise ValueError("Retained evidence checksums do not match.")

    monkeypatch.setattr(release_status, "verify_evidence", corrupt)
    with pytest.raises(ValueError, match="checksums"):
        release_status.candidate_from_workflow("owner/repo", workflow, commit="a" * 40)


def test_expired_or_duplicate_artifacts_cannot_produce_verified_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = status_fixture(candidate_result="success")["workflows"][1]
    workflow["artifacts"][0]["expired"] = True
    monkeypatch.setattr(
        release_status, "_download", lambda *args: pytest.fail("unexpected download")
    )
    assert release_status.candidate_from_workflow("owner/repo", workflow, commit="a" * 40) == (
        None,
        False,
    )
    workflow["artifacts"].append(workflow["artifacts"][0].copy())
    with pytest.raises(ValueError, match="duplicate"):
        release_status.candidate_from_workflow("owner/repo", workflow, commit="a" * 40)
