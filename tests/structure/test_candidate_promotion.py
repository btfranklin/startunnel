"""Exact-image evidence, immutable draft recovery, and isolated candidate boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts import compose_isolation as isolation
from scripts import proof_evidence
from scripts import prove_production_release as production_proof
from scripts import release_candidate as candidate
from scripts import release_draft as draft
from scripts.json_data import JsonObject, json_string
from tests.structure.workflows import load_workflow

IMAGE = "ghcr.io/btfranklin/startunnel@sha256:" + "a" * 64
CUSTOM_IMAGE = "registry.example:5000/team/startunnel@sha256:" + "a" * 64
COMMIT = "b" * 40
IMAGE_ID = "sha256:" + "c" * 64
HELPER_IMAGE_ID = "sha256:" + "f" * 64


def test_candidate_pull_selects_the_proof_platform_independently_of_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "pulled", "")

    values = {
        "the candidate source revision": COMMIT,
        "the current source commit": COMMIT,
        "the candidate version": "v" + (isolation.ROOT / "VERSION").read_text().strip(),
        "the candidate platform": "linux/amd64",
    }
    monkeypatch.setattr(isolation, "_run_process", run)
    monkeypatch.setattr(
        isolation, "required_output", lambda command, **kwargs: values[kwargs["label"]]
    )
    stack = isolation.IsolatedComposeProject(
        "stproof-candidate-platform", candidate_image=IMAGE, source_environment={}
    )
    stack.pull_candidate()
    assert calls == [["docker", "pull", "--platform", "linux/amd64", IMAGE]]


def test_image_checks_run_independently_and_all_gate_retained_evidence() -> None:
    validation = load_workflow("release-validation.yml")["jobs"]
    for name in ("security", "system", "full-load"):
        assert not validation[name].get("needs")
    workflow = load_workflow("release-candidate.yml")["jobs"]
    assert workflow["build"]["needs"] == "prepare"
    assert workflow["build"]["if"] == "needs.prepare.outputs.reuse_run_id == ''"
    assert workflow["validation"]["needs"] == "build"
    assert workflow["validation"]["uses"] == "./.github/workflows/release-validation.yml"
    assert set(workflow["evidence"]["needs"]) == {"build", "validation"}


def test_production_installation_is_recorded_in_the_system_attempt() -> None:
    steps = load_workflow("release-validation.yml")["jobs"]["system"]["steps"]
    production = next(
        step for step in steps if "scripts/prove_production_release.py" in step.get("run", "")
    )
    record = next(step for step in steps if "--proof system" in step.get("run", ""))
    assert production["if"] == "inputs.candidate_image != ''"
    assert '--candidate-image "$CANDIDATE_IMAGE"' in production["run"]
    assert steps.index(production) < steps.index(record)
    assert record["if"] == "always()"
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert "artifacts/production-release-proof-*.json" in upload["with"]["path"]


def test_candidate_writer_keeps_the_reused_source_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(candidate, "ROOT", tmp_path)
    monkeypatch.setattr(candidate, "git_commit", lambda: COMMIT)
    monkeypatch.setattr(candidate, "candidate_version", lambda: "v0.1.0")
    monkeypatch.setattr(
        sys, "argv", ["release_candidate.py", "--record", IMAGE, "--source-run-id", "42"]
    )
    assert candidate.main() == 0
    record = json.loads((tmp_path / "artifacts/candidate-image.json").read_text())
    assert record["source_commit"] == COMMIT
    assert record["source_validation_run_id"] == 42
    assert record["image"] == IMAGE


def test_failed_artifact_lookup_does_not_trigger_new_build_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(candidate, "github_api", unavailable)
    monkeypatch.setattr(candidate, "source_run", lambda *args: pytest.fail("lookup error ignored"))
    with pytest.raises(subprocess.CalledProcessError):
        candidate.prepare("owner/repository", COMMIT, "v0.1.0")


def evidence(directory: Path, monkeypatch: pytest.MonkeyPatch) -> candidate.CandidateData:
    monkeypatch.setattr(proof_evidence, "candidate_version", lambda: "v0.1.0")
    monkeypatch.setattr(
        proof_evidence,
        "proof_inputs",
        lambda kind, commit: {"kind": kind, "source_commit": commit, "platform": "linux/amd64"},
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    record: candidate.CandidateData = {
        "source_commit": COMMIT,
        "version": "v0.1.0",
        "image": IMAGE,
        "platform": "linux/amd64",
        "source_validation_run_id": 7,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "candidate-image.json").write_text(json.dumps(record))
    selection: candidate.ProofSelection = {"selected": {}, "history": []}
    for kind in proof_evidence.KINDS:
        attempt_dir = directory / "proofs" / kind / "1"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        if kind == "security":
            report = attempt_dir / "security" / "scan" / "validated-image.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"status": "passed", "image": IMAGE}))
        elif kind == "system":
            production = production_proof.new_report(COMMIT, "v0.1.0", IMAGE)
            production["status"] = "passed"
            production["image_ids"] = {
                service: IMAGE_ID for service in production_proof.APP_SERVICES
            }
            production["checks"] = {name: True for name in production_proof.CHECK_NAMES}
            production["disposable_volumes_removed"] = True
            production["private_files_removed"] = True
            production["release_evidence_eligible"] = True
            (attempt_dir / "production-release-proof-fixture.json").write_text(
                json.dumps(production)
            )
            (attempt_dir / "system-proof-fixture.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "build_mode": "immutable-candidate",
                        "candidate_image": IMAGE,
                        "release_evidence_eligible": True,
                        "disposable_volumes_removed": True,
                        "image_ids": {
                            service: IMAGE_ID
                            for service in ("web", "migrate", "maintenance", "proxy-web")
                        },
                    }
                )
            )
        else:
            (attempt_dir / "load-workload.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "fixture_cleanup_complete": True,
                        "configuration": proof_evidence.LOAD_CONFIGURATION,
                        "release_evidence_eligible": True,
                        "diagnostic_lock_seconds": 0,
                        "provenance": {
                            "build_mode": "immutable-candidate",
                            "image_ids": (
                                f"web={IMAGE_ID},maintenance={IMAGE_ID},"
                                f"load-profile={HELPER_IMAGE_ID}"
                            ),
                        },
                    }
                )
            )
            (attempt_dir / "load-observations.jsonl").write_text('{"sample":1}\n')
            (attempt_dir / "load-driver-observations.jsonl").write_text("{}\n")
            (attempt_dir / "load-run-fixture.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "candidate_image": IMAGE,
                        "source_commit": COMMIT,
                        "platform": "linux/amd64",
                        "release_evidence_eligible": True,
                        "disposable_volumes_removed": True,
                        "workload_report": "load-workload.json",
                        "observations_file": "load-observations.jsonl",
                        "driver_observations_file": "load-driver-observations.jsonl",
                        "helper": {
                            "image_id": HELPER_IMAGE_ID,
                            "source_commit": COMMIT,
                            "platform": "linux/amd64",
                            "imports_passed": True,
                        },
                        "observations": {
                            "complete": True,
                            "collector_failed": False,
                            "sample_interval_seconds": 1,
                            "expected_pool_workers": 4,
                            "pool_samples": 1,
                            "services_seen": ["web", "postgres", "maintenance", "load-profile"],
                            "max_sample_overhead_seconds": 0.1,
                        },
                    }
                )
            )
        if kind != "security":
            source_hashes = {
                name: proof_evidence.checksum(proof_evidence.ROOT / name)
                for name in ("Dockerfile", "pyproject.toml", "pdm.lock")
            }
            (attempt_dir / "proof-build-fixture.json").write_text(
                json.dumps(
                    {
                        "source_revision": COMMIT,
                        "platform": "linux/amd64",
                        "image_ids": {"load-profile": HELPER_IMAGE_ID},
                        "hashes": source_hashes,
                    }
                )
            )
        proof_evidence.record_proof(kind, attempt_dir, commit=COMMIT, image=IMAGE, status="success")
        relative_manifest = str((attempt_dir / f"proof-{kind}.json").relative_to(directory))
        reference: candidate.ProofReference = {
            "kind": kind,
            "run_id": 42,
            "attempt": 1,
            "artifact_id": 100 + proof_evidence.KINDS.index(kind),
            "artifact_digest": "sha256:" + "e" * 64,
            "manifest": relative_manifest,
            "sha256": proof_evidence.checksum(attempt_dir / f"proof-{kind}.json"),
            "status": "passed",
        }
        selection["selected"][kind] = reference
        selection["history"].append(reference)
    (directory / "proof-selection.json").write_text(json.dumps(selection, indent=2, sort_keys=True))
    return record


def test_evidence_requires_same_digest_and_actual_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    result = candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")
    assert result["status"] == "validated"
    assert len(result["proofs"]) == 3
    wrong = tmp_path / "proofs/security/1/security/scan/validated-image.json"
    wrong.write_text(json.dumps({"status": "passed", "image": IMAGE.replace("a", "d")}))
    with pytest.raises(ValueError, match="checksums"):
        candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


@pytest.mark.parametrize("kind", ["source", "missing", "diagnostic", "load-image", "cleanup"])
def test_rejects_incomplete_or_unrelated_evidence(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    commit = COMMIT
    if kind == "source":
        commit = "d" * 40
    elif kind == "missing":
        (tmp_path / "proofs/security/1/proof-security.json").unlink()
    elif kind == "diagnostic":
        path = tmp_path / "proofs/system/1/system-proof-fixture.json"
        report = json.loads(path.read_text())
        report["build_mode"] = "reused-current-images"
        path.write_text(json.dumps(report))
    else:
        path = tmp_path / "proofs/load/1/load-workload.json"
        report = json.loads(path.read_text())
        if kind == "cleanup":
            report["fixture_cleanup_complete"] = False
        else:
            report["provenance"]["image_ids"] = "web=other,maintenance=other"
        path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        candidate.verify_evidence(tmp_path, commit=commit, version="v0.1.0")


def test_retained_evidence_hashes_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    validated = candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")
    (tmp_path / "release-candidate.json").write_text(json.dumps(validated))
    assert candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0") == validated
    path = tmp_path / "proofs/security/1/security/scan/validated-image.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="checksums"):
        candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


def test_collect_selects_passed_second_attempt_and_retains_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    evidence(source, monkeypatch)
    artifacts = [artifact(name="candidate-image-42-1", artifact_id=1)]
    sources: dict[str, Path] = {"candidate-image-42-1": source}
    next_id = 10
    for kind in proof_evidence.KINDS:
        first = source / "proofs" / kind / "1" / f"proof-{kind}.json"
        first_record = json.loads(first.read_text())
        first_record["status"] = "failed"
        first.write_text(json.dumps(first_record))
        second_dir = source / "attempt-2" / kind
        shutil.copytree(first.parent, second_dir)
        second = second_dir / f"proof-{kind}.json"
        second_record = json.loads(second.read_text())
        second_record.update(attempt=2, status="passed")
        second.write_text(json.dumps(second_record))
        for attempt, proof_dir in ((1, first.parent), (2, second_dir)):
            name = f"proof-{kind}-42-{attempt}"
            artifacts.append(artifact(name=name, artifact_id=next_id))
            next_id += 1
            sources[name] = proof_dir

    monkeypatch.setattr(candidate, "github_api", lambda *args, **kwargs: [{"artifacts": artifacts}])

    def download(_repository: str, _run_id: int, selected: JsonObject, destination: Path) -> None:
        source_path = sources[json_string(selected["name"])]
        destination.mkdir(parents=True, exist_ok=True)
        if json_string(selected["name"]).startswith("candidate-image-"):
            shutil.copy(source_path / "candidate-image.json", destination / "candidate-image.json")
        else:
            shutil.copytree(source_path, destination, dirs_exist_ok=True)

    monkeypatch.setattr(candidate, "_download", download)
    retained = tmp_path / "retained"
    candidate.collect_evidence("owner/repository", 42, retained)
    result = candidate.verify_evidence(retained, commit=COMMIT, version="v0.1.0")

    assert [entry["status"] for entry in result["attempts"] if entry["kind"] == "security"] == [
        "failed",
        "passed",
    ]
    assert all(reference["attempt"] == 2 for reference in result["proofs"].values())
    assert set(result["evidence"]) >= {
        "proof-selection.json",
        *[reference["manifest"] for reference in result["proofs"].values()],
    }


@pytest.mark.parametrize("failed_kind", proof_evidence.KINDS)
def test_collect_retains_all_proofs_when_latest_attempt_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_kind: str,
) -> None:
    source = tmp_path / "source"
    evidence(source, monkeypatch)
    failed_path = source / "proofs" / failed_kind / "1" / f"proof-{failed_kind}.json"
    failed_record = json.loads(failed_path.read_text())
    failed_record["status"] = "failed"
    failed_path.write_text(json.dumps(failed_record))

    artifacts = [artifact(name="candidate-image-42-1", artifact_id=1)]
    sources: dict[str, Path] = {"candidate-image-42-1": source}
    for index, kind in enumerate(proof_evidence.KINDS, start=10):
        name = f"proof-{kind}-42-1"
        artifacts.append(artifact(name=name, artifact_id=index))
        sources[name] = source / "proofs" / kind / "1"
    monkeypatch.setattr(candidate, "github_api", lambda *args, **kwargs: [{"artifacts": artifacts}])
    downloaded: list[str] = []

    def download(_repository: str, _run_id: int, selected: JsonObject, destination: Path) -> None:
        name = json_string(selected["name"])
        downloaded.append(name)
        source_path = sources[name]
        destination.mkdir(parents=True, exist_ok=True)
        if name.startswith("candidate-image-"):
            shutil.copy(source_path / "candidate-image.json", destination / "candidate-image.json")
        else:
            shutil.copytree(source_path, destination, dirs_exist_ok=True)

    monkeypatch.setattr(candidate, "_download", download)
    retained = tmp_path / "retained"
    with pytest.raises(ValueError, match="latest proof attempt did not pass"):
        candidate.collect_evidence("owner/repository", 42, retained)

    selection = json.loads((retained / "proof-selection.json").read_text())
    assert set(downloaded) == set(sources)
    assert {entry["kind"] for entry in selection["history"]} == set(proof_evidence.KINDS)
    assert failed_kind not in selection["selected"]
    assert set(selection["selected"]) == set(proof_evidence.KINDS) - {failed_kind}
    with pytest.raises(ValueError, match="select all required proofs"):
        candidate.verify_evidence(retained, commit=COMMIT, version="v0.1.0")


def test_collect_does_not_select_older_pass_when_second_attempt_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    evidence(source, monkeypatch)
    second_dir = source / "attempt-2" / "security"
    shutil.copytree(source / "proofs/security/1", second_dir)
    second_manifest = second_dir / "proof-security.json"
    second_record = json.loads(second_manifest.read_text())
    second_record.update(attempt=2, status="failed")
    second_manifest.write_text(json.dumps(second_record))

    artifacts = [artifact(name="candidate-image-42-1", artifact_id=1)]
    sources: dict[str, Path] = {"candidate-image-42-1": source}
    for index, kind in enumerate(proof_evidence.KINDS, start=10):
        name = f"proof-{kind}-42-1"
        artifacts.append(artifact(name=name, artifact_id=index))
        sources[name] = source / "proofs" / kind / "1"
    second_name = "proof-security-42-2"
    artifacts.append(artifact(name=second_name, artifact_id=20))
    sources[second_name] = second_dir
    monkeypatch.setattr(candidate, "github_api", lambda *args, **kwargs: [{"artifacts": artifacts}])

    def download(_repository: str, _run_id: int, selected: JsonObject, destination: Path) -> None:
        name = json_string(selected["name"])
        source_path = sources[name]
        destination.mkdir(parents=True, exist_ok=True)
        if name.startswith("candidate-image-"):
            shutil.copy(source_path / "candidate-image.json", destination / "candidate-image.json")
        else:
            shutil.copytree(source_path, destination, dirs_exist_ok=True)

    monkeypatch.setattr(candidate, "_download", download)
    retained = tmp_path / "retained"
    with pytest.raises(ValueError, match="latest proof attempt did not pass"):
        candidate.collect_evidence("owner/repository", 42, retained)

    selection = json.loads((retained / "proof-selection.json").read_text())
    security_history = [entry for entry in selection["history"] if entry["kind"] == "security"]
    assert [(entry["attempt"], entry["status"]) for entry in security_history] == [
        (1, "passed"),
        (2, "failed"),
    ]
    assert "security" not in selection["selected"]
    assert set(selection["selected"]) == set(proof_evidence.KINDS) - {"security"}
    assert all(reference["attempt"] == 1 for reference in selection["selected"].values())
    with pytest.raises(ValueError, match="select all required proofs"):
        candidate.verify_evidence(retained, commit=COMMIT, version="v0.1.0")


def run_record(
    *, workflow: str = "release-candidate.yml", event: str = "workflow_dispatch"
) -> JsonObject:
    return {
        "id": 42,
        "head_sha": COMMIT,
        "head_branch": "main",
        "path": f".github/workflows/{workflow}",
        "event": event,
        "status": "completed",
        "conclusion": "success",
    }


def artifact(
    *, expired: bool = False, name: str | None = None, artifact_id: int = 77
) -> JsonObject:
    return {
        "id": artifact_id,
        "name": name or "release-candidate-42-1",
        "digest": "sha256:" + "e" * 64,
        "expired": expired,
        "workflow_run": {"id": 42, "head_sha": COMMIT, "head_branch": "main"},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "other"),
        ("head_branch", "feature"),
        ("path", ".github/workflows/other.yml"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ],
)
def test_validation_requires_completed_main_run_for_exact_commit(field: str, value: str) -> None:
    valid = run_record()
    assert candidate.successful_run(
        valid, COMMIT, "release-candidate.yml", events=("workflow_dispatch",)
    )
    assert not candidate.successful_run(
        {**valid, field: value}, COMMIT, "release-candidate.yml", events=("workflow_dispatch",)
    )


def test_promotion_finds_retained_candidate_across_artifact_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(candidate, "git_commit", lambda: COMMIT)
    monkeypatch.setattr(candidate, "candidate_version", lambda: "v0.1.0")
    monkeypatch.setattr(sys, "argv", ["release_candidate.py", "--find-run", "v0.1.0"])
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    calls: list[list[str]] = []

    def github(command: list[str], **kwargs: object) -> str:
        calls.append(command)
        if command[2] == "repos/owner/repository/actions/artifacts?per_page=100":
            assert command[-2:] == ["--paginate", "--slurp"]
            wrong = {
                **artifact(name="release-candidate-42-1"),
                "workflow_run": {"head_sha": "other"},
            }
            return json.dumps(
                [
                    {"artifacts": [artifact(name="release-candidate-42-1", expired=True), wrong]},
                    {"artifacts": [artifact(name="release-candidate-42-1")]},
                ]
            )
        if command[2] == "repos/owner/repository/actions/runs/42":
            return json.dumps(run_record())
        if command[2] == "repos/owner/repository/actions/runs/42/artifacts?per_page=100":
            return json.dumps([{"artifacts": [artifact(name="release-candidate-42-1")]}])
        raise AssertionError(command)

    monkeypatch.setattr("scripts.release_candidate.subprocess.check_output", github)
    assert candidate.main() == 0
    assert output.read_text() == "run_id=42\nartifact_name=release-candidate-42-1\n"
    assert len(calls) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "other"),
        ("head_branch", "feature"),
        ("path", ".github/workflows/ci.yml"),
        ("event", "push"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ],
)
def test_retained_artifact_cannot_make_an_unrelated_or_failed_run_eligible(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    def github(repository: str, path: str, **kwargs: object) -> object:
        if path.startswith("actions/artifacts?"):
            return [{"artifacts": [artifact()]}]
        return {**run_record(), field: value}

    monkeypatch.setattr(candidate, "github_api", github)
    assert candidate.validated_run("owner/repository", COMMIT) is None


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_prepare_reuses_completed_source_ci_without_running_tests(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    def github(repository: str, path: str, **kwargs: object) -> object:
        if path.startswith("actions/artifacts?"):
            return [{"artifacts": [artifact(expired=True)]}]
        return {"workflow_runs": [run_record(workflow="ci.yml", event=event)]}

    monkeypatch.setattr(candidate, "github_api", github)
    monkeypatch.setattr(
        "scripts.release_candidate.subprocess.run",
        lambda *args, **kwargs: pytest.fail("unexpected subprocess"),
    )
    assert candidate.prepare("owner/repository", COMMIT, "v0.1.0") == {
        "reuse_run_id": "",
        "source_run_id": 42,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "other"),
        ("head_branch", "feature"),
        ("path", ".github/workflows/other.yml"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ],
)
def test_source_reuse_rejects_unfinished_failed_or_unrelated_ci(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setattr(
        candidate,
        "github_api",
        lambda *args, **kwargs: {
            "workflow_runs": [{**run_record(workflow="ci.yml", event="push"), field: value}]
        },
    )
    with pytest.raises(ValueError, match="successful source CI"):
        candidate.source_run("owner/repository", COMMIT)


@pytest.mark.parametrize("tampered", [False, True])
def test_candidate_reuse_verifies_downloaded_evidence_before_skipping_work(
    monkeypatch: pytest.MonkeyPatch,
    tampered: bool,
) -> None:
    monkeypatch.setattr(candidate, "validated_run", lambda *args: 42)
    monkeypatch.setattr(candidate, "source_run", lambda *args: pytest.fail("source CI repeated"))

    def download(command: list[str], **kwargs: object) -> None:
        assert command[:6] == ["gh", "run", "download", "42", "--repo", "owner/repository"]
        directory = Path(command[-1])
        evidence(directory, monkeypatch)
        retained = candidate.verify_evidence(directory, commit=COMMIT, version="v0.1.0")
        (directory / "release-candidate.json").write_text(json.dumps(retained))
        if tampered:
            path = directory / "proofs/security/1/security/scan/validated-image.json"
            path.write_text(path.read_text() + "\n")

    monkeypatch.setattr("scripts.release_candidate.subprocess.run", download)
    monkeypatch.setattr(
        candidate,
        "github_api",
        lambda *args, **kwargs: [{"artifacts": [artifact(name="release-candidate-42-1")]}],
    )
    if tampered:
        with pytest.raises(ValueError, match="checksums"):
            candidate.prepare("owner/repository", COMMIT, "v0.1.0")
    else:
        assert candidate.prepare("owner/repository", COMMIT, "v0.1.0") == {
            "reuse_run_id": 42,
            "source_run_id": "",
        }


@pytest.mark.parametrize("run_id", [None, 0, -1, True])
def test_candidate_records_a_real_source_validation_run(run_id: object) -> None:
    document = {
        "version": "v0.1.0",
        "source_commit": COMMIT,
        "image": IMAGE,
        "platform": "linux/amd64",
        "source_validation_run_id": run_id,
    }
    with pytest.raises(ValueError, match="source CI run"):
        candidate.validate_candidate(document, commit=COMMIT, version="v0.1.0")


def test_promotion_must_preserve_registry_digest() -> None:
    candidate.check_registry_digest("Name: release\nDigest: sha256:" + "a" * 64 + "\n", IMAGE)
    with pytest.raises(ValueError, match="tested candidate digest"):
        candidate.check_registry_digest("Digest: sha256:" + "d" * 64 + "\n", IMAGE)


@pytest.mark.parametrize("outcome", ["published", "forbidden", "network", "absent"])
def test_draft_inspection_fails_closed_except_missing_release(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    monkeypatch.setenv("GH_REPO", "btfranklin/startunnel")
    result = subprocess.CompletedProcess(
        ["gh"],
        0 if outcome == "published" else 1,
        json.dumps({"draft": False}),
        "gh: Not Found (HTTP 404)" if outcome == "absent" else outcome,
    )
    monkeypatch.setattr("scripts.release_draft.subprocess.run", lambda *args, **kwargs: result)
    if outcome == "absent":
        assert draft.existing_draft("v0.1.0", {}) is False
    else:
        with pytest.raises(ValueError):
            draft.existing_draft("v0.1.0", {})


def test_draft_recovery_cannot_change_deployed_image() -> None:
    record = {
        "version": "v0.1.0",
        "source_commit": COMMIT,
        "image": IMAGE,
        "platform": "linux/amd64",
    }
    draft.check_existing_record(record, record.copy())
    with pytest.raises(ValueError, match="different deployment record"):
        draft.check_existing_record(record, {**record, "image": "different"})


@pytest.mark.parametrize("image", ["ghcr.io/btfranklin/startunnel:v0.1.0", "startunnel:local"])
def test_candidate_rejects_unpinned_images(image: str) -> None:
    with pytest.raises(isolation.IsolationError, match="pinned"):
        isolation.IsolatedComposeProject("sttest-candidate-fixture", candidate_image=image)


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="Compose rendering is checked on the host."
)
@pytest.mark.parametrize("image", [IMAGE, CUSTOM_IMAGE])
def test_candidate_overlay_cannot_rebuild_runtime_and_survives_backup_configuration(
    monkeypatch: pytest.MonkeyPatch,
    image: str,
) -> None:
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    with isolation.IsolatedComposeProject(
        "sttest-candidate-fixture",
        candidate_image=image,
        source_environment={"PATH": os.environ["PATH"]},
    ) as stack:
        result = subprocess.run(
            stack.compose_command("config", "--format", "json"),
            cwd=isolation.ROOT,
            env=stack.environment,
            text=True,
            capture_output=True,
            check=True,
        )
        model = json.loads(result.stdout)
        for service in ("web", "migrate", "maintenance"):
            assert "build" not in model["services"][service]
            assert model["services"][service]["image"] == image
        assert len(stack.compose_files()) == 3
        assert (
            model["services"]["web"]["environment"]["DJANGO_SETTINGS_MODULE"]
            == "startunnel.settings.development"
        )


@pytest.mark.parametrize("image", [IMAGE, CUSTOM_IMAGE])
@pytest.mark.parametrize("service", ["web", "migrate", "maintenance"])
def test_runtime_identity_rejects_a_different_service_image(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    image: str,
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-identity",
        candidate_image=image,
        source_environment={},
    )
    monkeypatch.setattr(stack, "capture", lambda step, *args: args[-1])

    def inspected(command: list[str], **kwargs: object) -> str:
        return "sha256:" + "d" * 64 if command[-1] == service else IMAGE_ID

    monkeypatch.setattr(isolation, "required_output", inspected)
    with pytest.raises(isolation.LaneError, match="did not run"):
        stack.verify_candidate_services()


@pytest.mark.parametrize("image", [IMAGE, CUSTOM_IMAGE])
def test_candidate_pull_rejects_wrong_source_before_services_start(
    monkeypatch: pytest.MonkeyPatch,
    image: str,
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-revision",
        candidate_image=image,
        source_environment={},
    )
    monkeypatch.setattr(
        isolation,
        "_run_process",
        lambda *args, **kwargs: subprocess.CompletedProcess(["docker"], 0, "pulled", ""),
    )
    monkeypatch.setattr(
        isolation,
        "required_output",
        lambda command, **kwargs: COMMIT if command[0] == "git" else "other",
    )
    with pytest.raises(isolation.IsolationError, match="source revision"):
        stack.pull_candidate()


@pytest.mark.parametrize(
    ("profiles", "services"),
    [
        (("load",), ("web", "migrate", "maintenance")),
        (
            ("examples", "agents", "proxy-proof"),
            ("web", "migrate", "maintenance", "proxy-web"),
        ),
    ],
)
def test_candidate_identity_checks_only_enabled_runtime_services(
    monkeypatch: pytest.MonkeyPatch,
    profiles: tuple[str, ...],
    services: tuple[str, ...],
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-profiles", candidate_image=IMAGE, profiles=profiles, source_environment={}
    )
    inspected_services: list[str] = []

    def captured(step: str, *arguments: str) -> str:
        service = arguments[-1]
        inspected_services.append(service)
        return service if service in services else ""

    monkeypatch.setattr(stack, "capture", captured)
    monkeypatch.setattr(isolation, "required_output", lambda *args, **kwargs: IMAGE_ID)
    stack.verify_candidate_services()
    assert tuple(inspected_services) == services


@pytest.mark.parametrize("service", ["web", "migrate", "maintenance", "proxy-web"])
@pytest.mark.parametrize("failure", ["missing", "mismatch"])
def test_candidate_identity_rejects_missing_or_wrong_enabled_service(
    monkeypatch: pytest.MonkeyPatch, service: str, failure: str
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-required",
        candidate_image=IMAGE,
        profiles=("examples", "agents", "proxy-proof"),
        source_environment={},
    )

    def captured(step: str, *arguments: str) -> str:
        selected = arguments[-1]
        return "" if failure == "missing" and selected == service else selected

    def inspected(command: list[str], **kwargs: object) -> str:
        return (
            "sha256:" + "d" * 64 if failure == "mismatch" and command[-1] == service else IMAGE_ID
        )

    monkeypatch.setattr(stack, "capture", captured)
    monkeypatch.setattr(isolation, "required_output", inspected)
    message = (
        f"Expected one image for {service}" if failure == "missing" else f"{service} did not run"
    )
    with pytest.raises(isolation.LaneError, match=message):
        stack.verify_candidate_services()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose rendering needs Docker.")
@pytest.mark.parametrize("image", [None, IMAGE])
@pytest.mark.parametrize("load", [False, True])
def test_file_metrics_are_present_only_in_load_profile(
    monkeypatch: pytest.MonkeyPatch, image: str | None, load: bool
) -> None:
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    with isolation.IsolatedComposeProject(
        "sttest-metrics-environment",
        candidate_image=image,
        profiles=("load",) if load else (),
        source_environment={"PATH": os.environ["PATH"]},
    ) as stack:
        result = subprocess.run(
            stack.compose_command("config", "--format", "json"),
            cwd=isolation.ROOT,
            env=stack.environment,
            text=True,
            capture_output=True,
            check=True,
        )
        environment = json.loads(result.stdout)["services"]["web"]["environment"]
        if load:
            assert environment["PROMETHEUS_MULTIPROC_DIR"] == "/tmp/startunnel-proof-metrics"
            assert environment["STARTUNNEL_PROOF_METRICS"] == "1"
        else:
            assert "PROMETHEUS_MULTIPROC_DIR" not in environment
