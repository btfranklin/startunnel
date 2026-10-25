"""Show exact release evidence, retained attempts, and measured workflow time."""

from __future__ import annotations

import argparse
import json
import os
import re
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
        verify_evidence,
    )
    from .release_record import candidate_version
else:
    from release_candidate import (
        CandidateData,
        _download,
        candidate_data,
        git_commit,
        github_api,
        verify_evidence,
    )
    from release_record import candidate_version


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


class ProofDetails(TypedDict):
    phases: list[JsonObject]
    builds: list[JsonObject]


class ReleaseStatus(TypedDict):
    source_commit: str
    workflows: list[WorkflowStatus]
    candidate: NotRequired[CandidateData]
    proof_details: NotRequired[dict[str, ProofDetails]]


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


def render_summary(status: ReleaseStatus) -> str:
    lines = [f"Release source: `{status['source_commit']}`", ""]
    for workflow in status["workflows"]:
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
    parser.add_argument("--run-id", type=int)
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
    if arguments.directory:
        candidate = verify_evidence(arguments.directory, commit=commit, version=candidate_version())
        status["candidate"] = candidate
        source_id = candidate["source_validation_run_id"]
        if source_id not in run_ids:
            run_ids.insert(0, source_id)
    status["workflows"] = [workflow_status(arguments.repository, run_id) for run_id in run_ids]
    if not status.get("candidate") and status["workflows"]:
        latest = status["workflows"][-1]
        images = [
            item
            for item in latest["artifacts"]
            if re.fullmatch(r"candidate-image-[0-9]+-[12]", (item.get("name") or ""))
            and item.get("expired") is False
        ]
        if images:
            selected = max(
                images, key=lambda item: int(json_string(item["name"]).rsplit("-", 1)[1])
            )
            with tempfile.TemporaryDirectory(prefix="startunnel-status-") as temporary:
                _download(arguments.repository, latest["run_id"], selected, Path(temporary))
                identity = candidate_data(
                    json_object(json.loads((Path(temporary) / "candidate-image.json").read_text()))
                )
            if identity.get("source_commit") != commit:
                raise ValueError("Candidate identity belongs to a different source commit.")
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
