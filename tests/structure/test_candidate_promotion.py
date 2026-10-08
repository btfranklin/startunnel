"""Exact-image evidence, immutable draft recovery, and isolated candidate boundaries."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_isolation as isolation
from scripts import release_candidate as candidate
from scripts import release_draft as draft

IMAGE = "ghcr.io/btfranklin/startunnel@sha256:" + "a" * 64
COMMIT = "b" * 40
IMAGE_ID = "sha256:" + "c" * 64


def evidence(directory: Path) -> dict[str, Any]:
    record = {
        "source_commit": COMMIT,
        "version": "v0.1.0",
        "image": IMAGE,
        "platform": "linux/amd64",
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


def test_ci_selection_requires_completed_main_push_for_exact_commit() -> None:
    valid = {
        "id": 42,
        "head_sha": COMMIT,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    assert candidate.successful_run({"workflow_runs": [valid]}, COMMIT) == 42
    for field, value in (
        ("head_sha", "other"),
        ("head_branch", "feature"),
        ("event", "workflow_dispatch"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ):
        with pytest.raises(ValueError, match="exact commit"):
            candidate.successful_run({"workflow_runs": [{**valid, field: value}]}, COMMIT)


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
    with pytest.raises(isolation.IsolationError, match="immutable"):
        isolation.IsolatedComposeProject("sttest-candidate-fixture", candidate_image=image)


def test_candidate_overlay_cannot_rebuild_runtime_and_survives_backup_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    with isolation.IsolatedComposeProject(
        "sttest-candidate-fixture",
        candidate_image=IMAGE,
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
            assert model["services"][service]["image"] == IMAGE
        assert len(stack.compose_files()) == 3
        assert (
            model["services"]["web"]["environment"]["DJANGO_SETTINGS_MODULE"]
            == "startunnel.settings.development"
        )


@pytest.mark.parametrize("service", ["web", "migrate", "maintenance"])
def test_runtime_identity_rejects_a_different_service_image(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-identity",
        candidate_image=IMAGE,
        source_environment={},
    )
    monkeypatch.setattr(stack, "capture", lambda step, *args: args[-1])

    def inspected(command: list[str], **kwargs: Any) -> str:
        return "sha256:" + "d" * 64 if command[-1] == service else IMAGE_ID

    monkeypatch.setattr(isolation, "required_output", inspected)
    with pytest.raises(isolation.LaneError, match="did not run"):
        stack.verify_candidate_services()


def test_candidate_pull_rejects_wrong_source_before_services_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = isolation.IsolatedComposeProject(
        "sttest-candidate-revision",
        candidate_image=IMAGE,
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
