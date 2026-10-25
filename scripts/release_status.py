"""Show exact release evidence, retained attempts, and measured workflow time."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import (
        JsonObject,
        json_array,
        json_boolean,
        json_integer,
        json_object,
        json_string,
        object_array,
    )
else:
    from json_data import (
        JsonObject,
        json_array,
        json_boolean,
        json_integer,
        json_object,
        json_string,
        object_array,
    )


if TYPE_CHECKING or __package__:
    from .release_candidate import (
        CandidateData,
        _download,
        candidate_data,
        git_commit,
        github_api,
        validate_candidate,
        verify_evidence,
    )
    from .release_record import ROOT, candidate_version
else:
    from release_candidate import (
        CandidateData,
        _download,
        candidate_data,
        git_commit,
        github_api,
        validate_candidate,
        verify_evidence,
    )
    from release_record import ROOT, candidate_version


def _github_object(repository: str, path: str) -> JsonObject:
    return json_object(github_api(repository, path))


def _github_pages(repository: str, path: str) -> list[JsonObject]:
    return object_array(github_api(repository, path, paginate=True))


class ActionJob(TypedDict):
    id: int
    name: str
    run_attempt: NotRequired[int]
    status: NotRequired[str | None]
    conclusion: NotRequired[str | None]
    started_at: NotRequired[str | None]
    completed_at: NotRequired[str | None]
    created_at: NotRequired[str | None]


class JobTiming(TypedDict):
    job_id: int
    attempt: int
    status: str | None
    conclusion: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    queue_seconds: float | None
    dependency_ready_to_start_seconds: float | None


class WorkflowTiming(TypedDict):
    elapsed_seconds: float | None
    complete: bool
    longest_observed_dependency_path: list[str]
    longest_path_seconds: float | None
    longest_path_calculation: str
    jobs: dict[str, JobTiming]
    unavailable: str


class ArtifactSummary(TypedDict):
    id: int | None
    name: str | None
    digest: str | None
    expired: bool | None
    created_at: str | None
    expires_at: str | None


class WorkflowStatus(TypedDict):
    run_id: int
    source_commit: str
    url: str
    workflow: str
    status: str
    conclusion: str | None
    attempt: int
    queue_seconds: float | None
    timing: WorkflowTiming
    artifacts: list[ArtifactSummary]
    branch: NotRequired[str | None]
    event: NotRequired[str | None]


class ReleaseReadiness(TypedDict):
    version: str
    branch: str
    source_clean: bool
    tag_exists: bool
    evidence_verified: bool
    can_start_candidate: bool
    candidate_blockers: list[str]
    can_promote: bool
    promotion_blockers: list[str]


class ProofDetails(TypedDict):
    phases: list[JsonObject]
    builds: list[JsonObject]


class ReleaseStatus(TypedDict):
    source_commit: str
    workflows: list[WorkflowStatus]
    candidate: NotRequired[CandidateData]
    proof_details: NotRequired[dict[str, ProofDetails]]
    promotion: NotRequired[WorkflowStatus]
    readiness: NotRequired[ReleaseReadiness]


def _optional_text(value: object) -> str | None:
    return None if value is None else json_string(value)


def _action_job(value: Mapping[str, object]) -> ActionJob:
    result: ActionJob = {"id": json_integer(value["id"]), "name": json_string(value["name"])}
    if "run_attempt" in value:
        result["run_attempt"] = json_integer(value["run_attempt"])
    result["status"] = _optional_text(value.get("status"))
    result["conclusion"] = _optional_text(value.get("conclusion"))
    result["started_at"] = _optional_text(value.get("started_at"))
    result["completed_at"] = _optional_text(value.get("completed_at"))
    result["created_at"] = _optional_text(value.get("created_at"))
    return result


def _artifact_summary(value: JsonObject) -> ArtifactSummary:
    return {
        "id": None if value.get("id") is None else json_integer(value["id"]),
        "name": _optional_text(value.get("name")),
        "digest": _optional_text(value.get("digest")),
        "expired": None if value.get("expired") is None else json_boolean(value["expired"]),
        "created_at": _optional_text(value.get("created_at")),
        "expires_at": _optional_text(value.get("expires_at")),
    }


def _time(value: str | None) -> float | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def job_timings(
    jobs: Sequence[Mapping[str, object]], dependencies: dict[str, list[str]]
) -> WorkflowTiming:
    """Use observed boundaries, including gaps, without adding parallel jobs."""
    selected: dict[str, ActionJob] = {}
    for job in sorted(
        (_action_job(job) for job in jobs), key=lambda item: item.get("run_attempt", 1)
    ):
        name = job["name"]
        if job.get("conclusion") != "skipped" or name not in selected:
            selected[name] = job
    starts = [_time(job.get("started_at")) for job in selected.values()]
    ends = [_time(job.get("completed_at")) for job in selected.values()]
    valid_starts = [value for value in starts if value is not None]
    valid_ends = [value for value in ends if value is not None]
    results: dict[str, JobTiming] = {}
    paths: dict[str, list[str]] = {}
    path_durations: dict[str, float] = {}
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in paths:
            return
        if name in visiting:
            raise ValueError("Workflow dependencies contain a cycle.")
        visiting.add(name)
        job = selected[name]
        parents = [parent for parent in dependencies.get(name, []) if parent in selected]
        for parent in parents:
            visit(parent)
        latest_parent = max(
            parents,
            key=lambda parent: _time(selected[parent].get("completed_at")) or 0,
            default=None,
        )
        start, end, created = (
            _time(_optional_text(job.get(field)))
            for field in ("started_at", "completed_at", "created_at")
        )
        path_parent = max(parents, key=lambda parent: path_durations.get(parent, 0), default=None)
        paths[name] = [*paths[path_parent], name] if path_parent else [name]
        parent_end = _time(selected[latest_parent].get("completed_at")) if latest_parent else None
        if start is not None and end is not None:
            gap = max(0.0, start - parent_end) if parent_end is not None else 0.0
            path_durations[name] = (
                path_durations.get(path_parent or "", 0) + max(0.0, end - start) + gap
            )
        results[name] = {
            "job_id": job["id"],
            "attempt": job.get("run_attempt", 1),
            "status": job.get("status"),
            "conclusion": job.get("conclusion"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("completed_at"),
            "duration_seconds": max(0, end - start)
            if start is not None and end is not None
            else None,
            "queue_seconds": max(0, start - created)
            if start is not None and created is not None
            else None,
            "dependency_ready_to_start_seconds": max(0, start - parent_end)
            if start is not None and parent_end is not None
            else None,
        }
        visiting.remove(name)

    for name in selected:
        visit(name)
    longest = max(path_durations, key=lambda name: path_durations[name], default=None)
    return {
        "elapsed_seconds": max(valid_ends) - min(valid_starts)
        if valid_starts and valid_ends
        else None,
        "complete": bool(selected)
        and all(job.get("status") == "completed" for job in selected.values()),
        "longest_observed_dependency_path": paths[longest] if longest else [],
        "longest_path_seconds": path_durations[longest] if longest else None,
        "longest_path_calculation": (
            "Job durations plus observed gaps after dependencies; "
            "parallel job durations are not added"
        ),
        "jobs": results,
        "unavailable": "null means that Actions has not provided a boundary",
    }


def _dependencies(jobs: Sequence[Mapping[str, object]], workflow: str) -> dict[str, list[str]]:
    names = {json_string(job["name"]) for job in jobs}
    result: dict[str, list[str]] = {}
    if workflow == "ci.yml":
        canonical = next((name for name in names if name == "Canonical locked gate"), None)
        if canonical:
            for name in names:
                if name in {
                    "PostgreSQL",
                    "examples proof",
                    "agents proof",
                    "Chromium, Firefox, and WebKit",
                }:
                    result[name] = [canonical]
        result["Workflow timing"] = [name for name in names if name != "Workflow timing"]
    elif workflow == "release-candidate.yml":
        prepare, build, evidence = (
            "Reuse completed validation",
            "Build one release image",
            "Retain validated promotion record",
        )
        result[build] = [prepare]
        proofs = [name for name in names if name.startswith("validation /")]
        result.update({name: [build] for name in proofs})
        preflight = next(
            (name for name in proofs if name.endswith("Production input preflight")), None
        )
        if preflight:
            for name in proofs:
                if name.endswith(
                    ("Isolated clean-stack system proof", "Isolated fixed load proof")
                ):
                    result[name] = [preflight]
        result[evidence] = proofs or [build]
    return result


def workflow_status(repository: str, run_id: int) -> WorkflowStatus:
    run = _github_object(repository, f"actions/runs/{run_id}")
    jobs = [
        job
        for page in _github_pages(repository, f"actions/runs/{run_id}/jobs?filter=all&per_page=100")
        for job in object_array(page["jobs"])
    ]
    artifacts = [
        artifact
        for page in _github_pages(repository, f"actions/runs/{run_id}/artifacts?per_page=100")
        for artifact in object_array(page["artifacts"])
    ]
    return {
        "run_id": run_id,
        "source_commit": json_string(run["head_sha"]),
        "branch": _optional_text(run.get("head_branch")),
        "event": _optional_text(run.get("event")),
        "url": json_string(run["html_url"]),
        "workflow": json_string(run["path"]),
        "status": json_string(run["status"]),
        "conclusion": _optional_text(run.get("conclusion")),
        "attempt": json_integer(run["run_attempt"]),
        "queue_seconds": max(
            0,
            (_time(_optional_text(run.get("run_started_at"))) or 0)
            - (_time(_optional_text(run.get("created_at"))) or 0),
        )
        if run.get("run_attempt") == 1 and run.get("run_started_at") and run.get("created_at")
        else None,
        "timing": job_timings(
            jobs, _dependencies(jobs, json_string(run["path"]).rsplit("/", 1)[-1])
        ),
        "artifacts": [_artifact_summary(item) for item in artifacts],
    }


def release_readiness(
    status: ReleaseStatus,
    *,
    version: str,
    branch: str,
    source_clean: bool,
    tag_exists: bool,
    evidence_verified: bool,
) -> ReleaseReadiness:
    """Describe technical prerequisites; do not authorize or start a release."""
    common: list[str] = []
    if branch != "main":
        common.append("Check out main.")
    if not source_clean:
        common.append("Commit the source changes before validation.")
    if tag_exists:
        common.append(f"The remote {version} tag already exists; inspect promotion or recovery.")
    candidate = status.get("candidate")
    source_id = candidate["source_validation_run_id"] if candidate else None
    source_ok = any(
        workflow["workflow"] == ".github/workflows/ci.yml"
        and workflow["source_commit"] == status["source_commit"]
        and workflow.get("branch") == "main"
        and workflow.get("event") in {"push", "workflow_dispatch"}
        and workflow["status"] == "completed"
        and workflow["conclusion"] == "success"
        and (source_id is None or workflow["run_id"] == source_id)
        for workflow in status["workflows"]
    )
    if not source_ok:
        common.append("Wait for successful source CI on main for this exact commit.")
    candidate_blockers = common.copy()
    promotion_blockers = common.copy()
    candidate_runs = [
        workflow
        for workflow in status["workflows"]
        if workflow["workflow"] == ".github/workflows/release-candidate.yml"
        and workflow["source_commit"] == status["source_commit"]
    ]
    successful_candidate = any(
        workflow["status"] == "completed"
        and workflow["conclusion"] == "success"
        and workflow.get("branch") == "main"
        and workflow.get("event") == "workflow_dispatch"
        for workflow in candidate_runs
    )
    retained_candidate = any(
        workflow["conclusion"] == "success"
        and any(
            re.fullmatch(rf"release-candidate-{workflow['run_id']}-[12]", artifact["name"] or "")
            and artifact["expired"] is False
            for artifact in workflow["artifacts"]
        )
        for workflow in candidate_runs
    )
    if any(workflow["status"] != "completed" for workflow in candidate_runs):
        candidate_blockers.append("Wait for the current candidate run to finish.")
    elif any(workflow["conclusion"] != "success" for workflow in candidate_runs):
        candidate_blockers.append("Inspect the failed candidate before a bounded manual repeat.")
    if successful_candidate and evidence_verified and retained_candidate:
        candidate_blockers.append("The candidate is verified; use its promotion path.")
    if not successful_candidate:
        promotion_blockers.append(
            "Complete a successful manual candidate run on main for this commit."
        )
    if not evidence_verified:
        promotion_blockers.append(
            "Retain and verify all required candidate evidence and checksums."
        )
    if not retained_candidate:
        promotion_blockers.append("The retained candidate artifact is missing or expired.")
    return {
        "version": version,
        "branch": branch,
        "source_clean": source_clean,
        "tag_exists": tag_exists,
        "evidence_verified": evidence_verified,
        "can_start_candidate": not candidate_blockers,
        "candidate_blockers": candidate_blockers,
        "can_promote": not promotion_blockers,
        "promotion_blockers": promotion_blockers,
    }


def inspect_readiness(
    status: ReleaseStatus, repository: str, *, evidence_verified: bool
) -> ReleaseReadiness:
    version = candidate_version()
    refs = json_array(github_api(repository, f"git/matching-refs/tags/{version}"))
    tag_exists = any(json_object(ref).get("ref") == f"refs/tags/{version}" for ref in refs)
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    source_clean = not subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT, text=True
    ).strip()
    return release_readiness(
        status,
        version=version,
        branch=branch,
        source_clean=source_clean,
        tag_exists=tag_exists,
        evidence_verified=evidence_verified,
    )


def candidate_from_workflow(
    repository: str, workflow: WorkflowStatus, *, commit: str
) -> tuple[CandidateData | None, bool]:
    """Verify a retained candidate; an image identity alone never proves promotion."""
    if workflow["workflow"] != ".github/workflows/release-candidate.yml":
        return None, False
    run_id = workflow["run_id"]
    artifacts = workflow["artifacts"]
    names = [item["name"] for item in artifacts]
    if len(names) != len(set(names)):
        raise ValueError("The workflow contains duplicate artifact names.")
    retained = [
        item
        for item in artifacts
        if re.fullmatch(rf"release-candidate-{run_id}-[12]", item["name"] or "")
    ]
    images = [
        item
        for item in artifacts
        if re.fullmatch(rf"candidate-image-{run_id}-[12]", item["name"] or "")
    ]
    verified = (
        workflow["status"] == "completed" and workflow["conclusion"] == "success" and bool(retained)
    )
    choices = retained if verified else images
    if not choices:
        return None, False
    artifact = max(choices, key=lambda item: int((item["name"] or "").rsplit("-", 1)[1]))
    if artifact["expired"] is not False:
        return None, False
    with tempfile.TemporaryDirectory(prefix="startunnel-status-") as temporary:
        directory = Path(temporary)
        _download(repository, run_id, artifact, directory)
        if verified:
            candidate = verify_evidence(directory, commit=commit, version=candidate_version())
        else:
            candidate = candidate_data(
                json_object(json.loads((directory / "candidate-image.json").read_text()))
            )
            validate_candidate(candidate, commit=commit, version=candidate_version())
    return candidate, verified


def render_summary(status: ReleaseStatus) -> str:
    lines = [f"Release source: `{status['source_commit']}`", ""]
    if "readiness" in status:
        readiness = status["readiness"]
        lines += [
            f"Version: {readiness['version']}. Branch: {readiness['branch'] or 'detached'}.",
            f"Can start a candidate: {'yes' if readiness['can_start_candidate'] else 'no'}.",
            *[f"- {reason}" for reason in readiness["candidate_blockers"]],
            f"Can promote: {'yes' if readiness['can_promote'] else 'no'}.",
            *[f"- {reason}" for reason in readiness["promotion_blockers"]],
            "These are technical checks, not release authorization.",
            "",
        ]
    workflows = list(status["workflows"])
    if "promotion" in status:
        promotion = status["promotion"]
        lines += [f"Promotion dispatch source: `{promotion['source_commit']}`.", ""]
        workflows.append(promotion)
    for workflow in workflows:
        lines += [
            f"[{workflow['workflow']}]({workflow['url']}): "
            f"{workflow['status']} / {workflow['conclusion'] or 'pending'}; "
            f"attempt {workflow['attempt']}."
        ]
        timing = workflow["timing"]
        elapsed = timing["elapsed_seconds"]
        if elapsed is not None:
            lines.append(
                f"Observed elapsed time: {elapsed:.1f} seconds. Parallel jobs are not added."
            )
        path = timing["longest_observed_dependency_path"]
        if path:
            lines.append("Longest observed dependency path: " + " → ".join(path) + ".")
        lines += ["", "| Job | Attempt | Result | Seconds |", "|---|---:|---|---:|"]
        for name, job in timing["jobs"].items():
            duration = job["duration_seconds"]
            lines.append(
                f"| {name} | {job['attempt']} | {job['conclusion'] or job['status']} | "
                f"{duration if duration is not None else 'pending'} |"
            )
        lines += ["", "Retained artifacts:"]
        lines += [
            f"- {item['name']}: ID {item['id']}; expired={item['expired']}"
            for item in workflow["artifacts"]
        ]
        lines.append("")
    if status.get("candidate"):
        candidate = status["candidate"]
        lines += [
            f"Candidate image: `{candidate['image']}`.",
            f"Source CI: {candidate['source_validation_run_id']}.",
        ]
        for kind, reference in candidate.get("proofs", {}).items():
            lines.append(
                f"- {kind}: selected attempt {reference['attempt']}; "
                f"artifact {reference['artifact_id']}."
            )
    for kind, details in status.get("proof_details", {}).items():
        lines += ["", f"{kind} phase times:"]
        for phase in details["phases"]:
            phase_name = phase.get("phase", phase.get("name", "unknown"))
            seconds = phase.get("elapsed_seconds", phase.get("duration_seconds"))
            lines.append(f"- {phase_name}: {seconds} seconds; {phase.get('status', 'unknown')}.")
        for build in details["builds"]:
            lines.append(
                f"- Helper cache: {build.get('cache')}; hits={build.get('cache_hits')}; "
                f"fallback={build.get('cache_error_fallback')}; "
                f"build seconds={build.get('duration_seconds')}."
            )
    lines += [
        "",
        "Unavailable time fields remain null in the JSON report. "
        "Retained artifacts require checksum and input verification before promotion.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--run-id", type=int)
    run_group.add_argument("--promotion-run-id", type=int)
    parser.add_argument(
        "--repository", default=os.getenv("GITHUB_REPOSITORY", "btfranklin/startunnel")
    )
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    commit = git_commit()
    run_ids: list[int] = []
    if arguments.run_id:
        run_ids.append(arguments.run_id)
    else:
        runs = _github_object(
            arguments.repository,
            f"actions/workflows/release-candidate.yml/runs?branch=main&head_sha={commit}&per_page=1",
        )["workflow_runs"]
        if runs:
            run_ids.append(json_integer(json_object(json_array(runs)[0])["id"]))
    status: ReleaseStatus = {"source_commit": commit, "workflows": []}
    evidence_verified = False
    if arguments.directory:
        evidence_verified = True
        candidate = verify_evidence(arguments.directory, commit=commit, version=candidate_version())
        status["candidate"] = candidate
        source_id = candidate["source_validation_run_id"]
        if source_id not in run_ids:
            run_ids.insert(0, source_id)
    status["workflows"] = [workflow_status(arguments.repository, run_id) for run_id in run_ids]
    if not status.get("candidate") and status["workflows"]:
        latest = status["workflows"][-1]
        identity, evidence_verified = candidate_from_workflow(
            arguments.repository, latest, commit=commit
        )
        if identity is not None:
            status["candidate"] = identity
            source_id = identity["source_validation_run_id"]
            if source_id not in run_ids:
                status["workflows"].insert(0, workflow_status(arguments.repository, source_id))
    if not status.get("candidate"):
        source_runs = _github_object(
            arguments.repository,
            f"actions/workflows/ci.yml/runs?branch=main&head_sha={commit}&per_page=1",
        )["workflow_runs"]
        if (
            source_runs
            and json_integer(json_object(json_array(source_runs)[0])["id"]) not in run_ids
        ):
            status["workflows"].insert(
                0,
                workflow_status(
                    arguments.repository,
                    json_integer(json_object(json_array(source_runs)[0])["id"]),
                ),
            )
    if arguments.directory:
        details: dict[str, ProofDetails] = {}
        for kind, reference in status["candidate"]["proofs"].items():
            manifest_path = arguments.directory / reference["manifest"]
            proof = json_object(json.loads(manifest_path.read_text()))
            builds = [
                json_object(json.loads((manifest_path.parent / name).read_text()))
                for name in json_object(proof["files"])
                if Path(name).name.startswith("proof-build-")
            ]
            phases: list[JsonObject] = []
            for key in ("execution", "workload", "system"):
                if key in json_object(proof["reports"]):
                    report = json_object(
                        json.loads(
                            (
                                manifest_path.parent
                                / json_string(json_object(proof["reports"])[key])
                            ).read_text()
                        )
                    )
                    phases.extend(object_array(report.get("phases", report.get("steps", []))))
            details[kind] = {"phases": phases, "builds": builds}
        status["proof_details"] = details
    if any(workflow["source_commit"] != commit for workflow in status["workflows"]):
        raise ValueError("Workflow status belongs to a different source commit.")
    if arguments.promotion_run_id:
        promotion = workflow_status(arguments.repository, arguments.promotion_run_id)
        if promotion["workflow"] != ".github/workflows/release.yml":
            raise ValueError("Promotion timing requires the release workflow.")
        status["promotion"] = promotion
    status["readiness"] = inspect_readiness(
        status, arguments.repository, evidence_verified=evidence_verified
    )
    summary = render_summary(status)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
            output.write(summary)
    print(json.dumps(status, indent=2, sort_keys=True) if arguments.json else summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
