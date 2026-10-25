"""Checks checksum-protected proof manifests and their fixed inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts import proof_build, proof_evidence
from scripts import release_candidate as candidate
from scripts.release_candidate import verify_evidence
from tests.structure.test_candidate_promotion import COMMIT, IMAGE, artifact, evidence


@pytest.mark.parametrize("kind", proof_evidence.KINDS)
def test_recorded_proof_manifests_validate(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs" / kind / "1" / f"proof-{kind}.json"
    record = proof_evidence.read_object(manifest)

    proof_evidence.validate_proof(record, manifest.parent, kind=kind, commit=COMMIT, image=IMAGE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", "d" * 40),
        ("candidate_image", "ghcr.io/btfranklin/startunnel@sha256:" + "d" * 64),
        ("platform", "linux/arm64"),
        ("status", "failed"),
    ],
)
def test_manifest_rejects_wrong_source_image_platform_and_status(
    field: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/security/1/proof-security.json"
    record = proof_evidence.read_object(manifest)
    record[field] = value

    with pytest.raises(ValueError):
        proof_evidence.validate_proof(
            record, manifest.parent, kind="security", commit=COMMIT, image=IMAGE
        )


def test_manifest_rejects_wrong_input_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/security/1/proof-security.json"
    record = proof_evidence.read_object(manifest)
    record["input_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="inputs"):
        proof_evidence.validate_proof(
            record, manifest.parent, kind="security", commit=COMMIT, image=IMAGE
        )


@pytest.mark.parametrize("file_name", ["../outside.json", "/tmp/outside.json"])
def test_manifest_rejects_unsafe_evidence_paths(
    file_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/security/1/proof-security.json"
    record = proof_evidence.read_object(manifest)
    record["files"] = {file_name: "a" * 64}

    with pytest.raises(ValueError, match="paths"):
        proof_evidence.validate_proof(
            record, manifest.parent, kind="security", commit=COMMIT, image=IMAGE
        )


def test_manifest_rejects_corrupt_report_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/security/1/proof-security.json"
    report = manifest.parent / "security/scan/validated-image.json"
    report.write_text(report.read_text() + " ")

    with pytest.raises(ValueError, match="checksums"):
        proof_evidence.validate_proof(
            proof_evidence.read_object(manifest),
            manifest.parent,
            kind="security",
            commit=COMMIT,
            image=IMAGE,
        )


def test_load_manifest_rejects_changed_fixed_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/load/1/proof-load.json"
    workload = manifest.parent / "load-workload.json"
    report = json.loads(workload.read_text())
    report["configuration"]["duration_seconds"] = 299
    workload.write_text(json.dumps(report))
    record = proof_evidence.read_object(manifest)
    record["files"]["load-workload.json"] = proof_evidence.checksum(workload)

    with pytest.raises(ValueError, match="configuration"):
        proof_evidence.validate_proof(
            record, manifest.parent, kind="load", commit=COMMIT, image=IMAGE
        )


def test_system_manifest_rejects_changed_helper_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    manifest = tmp_path / "proofs/system/1/proof-system.json"
    helper = manifest.parent / "proof-build-fixture.json"
    provenance = json.loads(helper.read_text())
    provenance["hashes"]["Dockerfile"] = "0" * 64
    helper.write_text(json.dumps(provenance))
    record = proof_evidence.read_object(manifest)
    record["files"][helper.name] = proof_evidence.checksum(helper)

    with pytest.raises(ValueError, match="dependency inputs"):
        proof_evidence.validate_proof(
            record, manifest.parent, kind="system", commit=COMMIT, image=IMAGE
        )


def test_candidate_selection_rejects_unsafe_manifest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    selection_path = tmp_path / "proof-selection.json"
    selection = json.loads(selection_path.read_text())
    selection["selected"]["security"]["manifest"] = "../outside.json"
    selection["history"][0]["manifest"] = "../outside.json"
    selection_path.write_text(json.dumps(selection))

    with pytest.raises(ValueError, match="paths"):
        verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


def test_candidate_requires_same_runtime_ids_across_system_and_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence(tmp_path, monkeypatch)
    system_dir = tmp_path / "proofs/system/1"
    system_report = system_dir / "system-proof-fixture.json"
    report = json.loads(system_report.read_text())
    report["image_ids"]["maintenance"] = "sha256:" + "d" * 64
    system_report.write_text(json.dumps(report))
    manifest = system_dir / "proof-system.json"
    proof = json.loads(manifest.read_text())
    proof["files"][system_report.name] = proof_evidence.checksum(system_report)
    manifest.write_text(json.dumps(proof))
    selection_path = tmp_path / "proof-selection.json"
    selection = json.loads(selection_path.read_text())
    reference = selection["selected"]["system"]
    reference["sha256"] = proof_evidence.checksum(manifest)
    for history in selection["history"]:
        if history["kind"] == "system":
            history["sha256"] = reference["sha256"]
    selection_path.write_text(json.dumps(selection))

    with pytest.raises(ValueError, match="different runtime images"):
        verify_evidence(tmp_path, commit=COMMIT, version="v0.1.0")


@pytest.mark.parametrize("failure", ["expired", "missing", "duplicate", "too-many"])
def test_collection_rejects_expired_missing_duplicate_and_excess_attempt_artifacts(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_artifact = artifact(name="candidate-image-42-1", artifact_id=1)
    artifacts = [candidate_artifact]
    for kind in proof_evidence.KINDS:
        if failure == "missing" and kind == "security":
            continue
        attempt = 3 if failure == "too-many" and kind == "security" else 1
        entry = artifact(name=f"proof-{kind}-42-{attempt}", artifact_id=10 + len(artifacts))
        if failure == "expired" and kind == "security":
            entry["expired"] = True
        artifacts.append(entry)
        if failure == "duplicate" and kind == "security":
            artifacts.append(dict(entry))
    if failure == "expired":
        candidate_artifact["expired"] = True

    monkeypatch.setattr(candidate, "github_api", lambda *args, **kwargs: [{"artifacts": artifacts}])

    def download(
        _repository: str,
        _run_id: int,
        selected: dict[str, Any],
        destination: Path,
    ) -> None:
        assert selected["name"] == "candidate-image-42-1"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "candidate-image.json").write_text(
            json.dumps(
                {
                    "source_commit": COMMIT,
                    "version": "v0.1.0",
                    "image": IMAGE,
                    "platform": "linux/amd64",
                    "source_validation_run_id": 7,
                }
            )
        )

    monkeypatch.setattr(candidate, "_download", download)
    with pytest.raises(ValueError):
        candidate.collect_evidence("owner/repository", 42, tmp_path / "retained")


def test_input_hash_change_invalidates_recorded_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "input.py"
    source.write_text("value = 1\n", encoding="utf-8")
    directory = tmp_path / "security"
    report = directory / "scan" / "validated-image.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {"status": "passed", "image": "ghcr.io/btfranklin/startunnel@sha256:" + "a" * 64}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        proof_evidence,
        "proof_inputs",
        lambda kind, commit: {
            "source_commit": commit,
            "platform": "linux/amd64",
            "kind": kind,
            "input_sha256": proof_build._sha256(source),
        },
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    commit = "b" * 40
    image = "ghcr.io/btfranklin/startunnel@sha256:" + "a" * 64
    record = proof_evidence.record_proof(
        "security", directory, commit=commit, image=image, status="success"
    )
    source.write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inputs"):
        proof_evidence.validate_proof(
            record, directory, kind="security", commit=commit, image=image
        )
