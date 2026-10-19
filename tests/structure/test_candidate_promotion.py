"""Exact-image evidence, immutable draft recovery, and isolated candidate boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_isolation as isolation
from scripts import release_candidate as candidate
from scripts import release_draft as draft
from tests.structure.workflows import load_workflow

IMAGE = "ghcr.io/btfranklin/startunnel@sha256:" + "a" * 64
CUSTOM_IMAGE = "registry.example:5000/team/startunnel@sha256:" + "a" * 64
COMMIT = "b" * 40
IMAGE_ID = "sha256:" + "c" * 64


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
    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", "api"])

    monkeypatch.setattr(candidate, "github_api", unavailable)
    monkeypatch.setattr(candidate, "source_run", lambda *args: pytest.fail("lookup error ignored"))
    with pytest.raises(subprocess.CalledProcessError):
        candidate.prepare("owner/repository", COMMIT, "v0.1.0")


def evidence(directory: Path) -> dict[str, Any]:
    record = {
        "source_commit": COMMIT,
        "version": "v0.1.0",
        "image": IMAGE,
        "platform": "linux/amd64",
        "source_validation_run_id": 7,
    }
    files = {
        "candidate-image.json": record,
        "validated-image.json": {"status": "passed", "image": IMAGE},
        "validated-load-image.json": {"status": "passed", "image": IMAGE},
        "system-proof-fixture.json": {
            "status": "passed",
            "build_mode": "immutable-candidate",
            "candidate_image": IMAGE,
            "release_evidence_eligible": True,
            "disposable_volumes_removed": True,
            "image_ids": {"web": IMAGE_ID},
        },
        "load-profile-fixture.json": {
            "passed": True,
            "fixture_cleanup_complete": True,
            "provenance": {
                "build_mode": "immutable-candidate",
                "image_ids": f"web={IMAGE_ID},maintenance={IMAGE_ID}",
            },
        },
    }
    for name, value in files.items():
        (directory / name).write_text(json.dumps(value))
    return record


def test_evidence_requires_same_digest_and_actual_runtime(tmp_path: Path) -> None:
    evidence(tmp_path)
    result = candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")
    assert result["status"] == "validated"
    assert len(result["evidence"]) == 4
    wrong = tmp_path / "validated-image.json"
    wrong.write_text(json.dumps({"status": "passed", "image": IMAGE.replace("a", "d")}))
    with pytest.raises(ValueError, match="candidate digest"):
        candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


@pytest.mark.parametrize("kind", ["source", "missing", "diagnostic", "load-image", "cleanup"])
def test_rejects_incomplete_or_unrelated_evidence(tmp_path: Path, kind: str) -> None:
    evidence(tmp_path)
    commit = COMMIT
    if kind == "source":
        commit = "d" * 40
    elif kind == "missing":
        (tmp_path / "validated-image.json").unlink()
    elif kind == "diagnostic":
        path = tmp_path / "system-proof-fixture.json"
        report = json.loads(path.read_text())
        report["build_mode"] = "reused-current-images"
        path.write_text(json.dumps(report))
    else:
        path = tmp_path / "load-profile-fixture.json"
        report = json.loads(path.read_text())
        if kind == "cleanup":
            report["fixture_cleanup_complete"] = False
        else:
            report["provenance"]["image_ids"] = "web=other,maintenance=other"
        path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        candidate.verify_evidence(tmp_path, commit=commit, version="v0.1.0")


def test_retained_evidence_hashes_are_checked(tmp_path: Path) -> None:
    evidence(tmp_path)
    validated = candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")
    (tmp_path / "release-candidate.json").write_text(json.dumps(validated))
    assert candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0") == validated
    path = tmp_path / "validated-image.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="checksums"):
        candidate.verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


def run_record(
    *, workflow: str = "release-candidate.yml", event: str = "workflow_dispatch"
) -> dict[str, Any]:
    return {
        "id": 42,
        "head_sha": COMMIT,
        "head_branch": "main",
        "path": f".github/workflows/{workflow}",
        "event": event,
        "status": "completed",
        "conclusion": "success",
    }


def artifact(*, expired: bool = False) -> dict[str, Any]:
    return {
        "name": "release-candidate",
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

    def github(command: list[str], **kwargs: Any) -> str:
        calls.append(command)
        if "actions/artifacts?" in command[2]:
            assert command[-2:] == ["--paginate", "--slurp"]
            wrong = {**artifact(), "workflow_run": {"head_sha": "other"}}
            return json.dumps(
                [{"artifacts": [artifact(expired=True), wrong]}, {"artifacts": [artifact()]}]
            )
        assert command[2] == "repos/owner/repository/actions/runs/42"
        return json.dumps(run_record())

    monkeypatch.setattr("scripts.release_candidate.subprocess.check_output", github)
    assert candidate.main() == 0
    assert output.read_text() == "run_id=42\n"
    assert len(calls) == 2


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
    def github(repository: str, path: str, **kwargs: Any) -> Any:
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
    def github(repository: str, path: str, **kwargs: Any) -> Any:
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

    def download(command: list[str], **kwargs: Any) -> None:
        assert command[:6] == ["gh", "run", "download", "42", "--repo", "owner/repository"]
        directory = Path(command[-1])
        evidence(directory)
        retained = candidate.verify_evidence(directory, commit=COMMIT, version="v0.1.0")
        (directory / "release-candidate.json").write_text(json.dumps(retained))
        if tampered:
            path = directory / "validated-image.json"
            path.write_text(path.read_text() + "\n")

    monkeypatch.setattr("scripts.release_candidate.subprocess.run", download)
    if tampered:
        with pytest.raises(ValueError, match="checksums"):
            candidate.prepare("owner/repository", COMMIT, "v0.1.0")
    else:
        assert candidate.prepare("owner/repository", COMMIT, "v0.1.0") == {
            "reuse_run_id": 42,
            "source_run_id": "",
        }


@pytest.mark.parametrize("run_id", [None, 0, -1, True])
def test_candidate_records_a_real_source_validation_run(run_id: Any) -> None:
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

    def inspected(command: list[str], **kwargs: Any) -> str:
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
