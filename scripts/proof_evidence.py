"""Bind safe proof reports to exact source, inputs, and immutable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import (
        JsonObject,
        json_integer,
        json_object,
        json_string,
    )
    from .prove_production_release import validate_report as validate_production_report
    from .release_record import candidate_version
else:
    from json_data import (
        JsonObject,
        json_integer,
        json_object,
        json_string,
    )
    from prove_production_release import validate_report as validate_production_report
    from release_record import candidate_version


ROOT = Path(__file__).resolve().parents[1]
KINDS = ("security", "system", "load")
LOAD_CONFIGURATION: dict[str, int | str] = {
    "duration_seconds": 300,
    "drain_seconds": 30,
    "sends_per_second": 50,
    "concurrent_activity_readers": 100,
    "max_in_flight_sends": 256,
    "web_workers": "4",
    "web_database_pool_max_size": "18",
    "credential_count": 500,
    "tunnel_count": 10,
}


class ProofInputs(TypedDict):
    source_commit: str
    platform: str
    files: dict[str, str]
    profiles: list[str]
    configuration: NotRequired[dict[str, int | str]]
    drain_seconds: NotRequired[int]
    active_credential_limit: NotRequired[int]


class ProofRecord(TypedDict):
    kind: str
    source_commit: str
    candidate_image: str
    platform: str
    run_id: int
    attempt: int
    status: str
    inputs: ProofInputs
    input_fingerprint: str
    recorded_at: str
    files: dict[str, str]
    reports: dict[str, str]
    incomplete: NotRequired[bool]
    release_evidence_eligible: NotRequired[bool]


def read_object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Evidence must contain a JSON object.")
    return json_object(value)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def referenced_file(directory: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Evidence paths must stay inside their artifact.")
    result = directory / path
    if (
        result.is_symlink()
        or not result.is_file()
        or not result.resolve().is_relative_to(directory.resolve())
    ):
        raise ValueError("Evidence file is missing or outside its artifact.")
    return result


def proof_inputs(kind: str, commit: str) -> ProofInputs:
    if kind not in KINDS:
        raise ValueError("Unknown proof kind.")
    # Hash tracked proof inputs as well as the commit. This also rejects local
    # edits to proof code when someone supplies a candidate built from HEAD.
    files = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "Dockerfile",
                "compose*.yaml",
                "pyproject.toml",
                "pdm.lock",
                "scripts",
                "tests",
                "src",
                "docker",
                ".github",
            ],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    inputs: ProofInputs = {
        "source_commit": commit,
        "platform": "linux/amd64",
        "files": {name: checksum(ROOT / name) for name in files if name},
        "profiles": {
            "security": [],
            "system": ["examples", "agents", "proxy-proof"],
            "load": ["load"],
        }[kind],
    }
    if kind == "load":
        inputs["configuration"] = LOAD_CONFIGURATION
        inputs["drain_seconds"] = 30
        inputs["active_credential_limit"] = 500
    return inputs


def fingerprint(inputs: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _one(directory: Path, pattern: str) -> Path:
    paths = list(directory.glob(pattern))
    if len(paths) != 1:
        raise ValueError(f"Expected one {pattern} report in this proof attempt.")
    return paths[0]


def record_proof(
    kind: str, directory: Path, *, commit: str, image: str, status: str
) -> ProofRecord:
    """Write one attempt manifest, including failed and incomplete attempts."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    inputs = proof_inputs(kind, commit)
    diagnostic_success = status == "success" and not image
    record: ProofRecord = {
        "kind": kind,
        "source_commit": commit,
        "candidate_image": image,
        "platform": "linux/amd64",
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "status": "diagnostic"
        if diagnostic_success
        else "passed"
        if status == "success"
        else "failed",
        "inputs": inputs,
        "input_fingerprint": fingerprint(inputs),
        "recorded_at": datetime.now(UTC).isoformat(),
        "files": {},
        "reports": {},
    }
    try:
        reports: dict[str, Path] = {}
        if kind == "security":
            reports["validation"] = _one(directory, "security/*/validated-image.json")
            files = [path for path in reports["validation"].parent.iterdir() if path.is_file()]
        elif kind == "system":
            reports["system"] = _one(directory, "system-proof-*.json")
            if image:
                reports["production"] = _one(directory, "production-release-proof-*.json")
            files = [*reports.values(), *directory.glob("proof-build-*.json")]
        else:
            reports["execution"] = _one(directory, "load-run-*.json")
            execution = read_object(reports["execution"])
            reports["workload"] = referenced_file(
                directory, json_string(execution["workload_report"])
            )
            reports["observations"] = referenced_file(
                directory, json_string(execution["observations_file"])
            )
            reports["driver_observations"] = referenced_file(
                directory, json_string(execution["driver_observations_file"])
            )
            files = [*reports.values(), *directory.glob("proof-build-*.json")]
        record["reports"] = {
            name: str(path.relative_to(directory)) for name, path in reports.items()
        }
        record["files"] = {str(path.relative_to(directory)): checksum(path) for path in files}
        if record["status"] == "passed":
            validate_proof(record, directory, kind=kind, commit=commit, image=image)
    except ValueError, KeyError, OSError, TypeError:
        record["status"] = "failed"
        record["incomplete"] = True
        # Retain all safe reports that were produced before a failed phase.
        patterns = {
            "security": ("security/**/*.json", "security/**/*.sarif"),
            "system": (
                "system-proof-*.json",
                "production-release-proof-*.json",
                "proof-build-*.json",
            ),
            "load": (
                "load-run-*.json",
                "load-profile-*.json",
                "load-observations-*.jsonl",
                "load-profile-*.driver.jsonl",
                "proof-build-*.json",
            ),
        }[kind]
        record["files"] = {
            str(path.relative_to(directory)): checksum(path)
            for pattern in patterns
            for path in directory.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    record["release_evidence_eligible"] = record["status"] == "passed"
    (directory / f"proof-{kind}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    )
    return record


def validate_proof(
    record: Mapping[str, object], directory: Path, *, kind: str, commit: str, image: str
) -> None:
    """Check one selected successful attempt without searching its history."""
    inputs = proof_inputs(kind, commit)
    if any(
        record.get(key) != value
        for key, value in {
            "kind": kind,
            "source_commit": commit,
            "candidate_image": image,
            "platform": "linux/amd64",
            "status": "passed",
            "inputs": inputs,
            "input_fingerprint": fingerprint(inputs),
        }.items()
    ):
        raise ValueError("Proof source, image, platform, inputs, or status do not match.")
    if (
        type(record.get("run_id")) is not int
        or type(record.get("attempt")) is not int
        or not 1 <= json_integer(record["attempt"]) <= 2
    ):
        raise ValueError("Proof must identify a bounded workflow attempt.")
    files = json_object(record.get("files"))
    if not isinstance(files, dict) or not files:
        raise ValueError("Proof has no retained files.")
    for name, expected in files.items():
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or checksum(referenced_file(directory, name)) != expected
        ):
            raise ValueError("Proof evidence checksums do not match.")
    reports = {name: json_string(path) for name, path in json_object(record["reports"]).items()}
    for name in reports.values():
        if name not in files:
            raise ValueError("Selected report is not checksum protected.")
    if kind == "security":
        report = read_object(referenced_file(directory, reports["validation"]))
        if report.get("status") != "passed" or report.get("image") != image:
            raise ValueError("Security proof does not validate the candidate.")
        return
    build_paths = [name for name in files if Path(name).name.startswith("proof-build-")]
    if not build_paths:
        raise ValueError("Proof helper build provenance is missing.")
    helper_build_id = ""
    for name in build_paths:
        build = read_object(referenced_file(directory, name))
        if build.get("source_revision") != commit or build.get("platform") != "linux/amd64":
            raise ValueError("Proof helper source or platform differs.")
        if build.get("hashes") != {
            name: checksum(ROOT / name) for name in ("Dockerfile", "pyproject.toml", "pdm.lock")
        }:
            raise ValueError("Proof helper dependency inputs differ.")
        if kind == "load":
            build_ids = build.get("image_ids", {})
            if isinstance(build_ids, dict):
                helper_build_id = json_string(build_ids.get("load-profile", helper_build_id))
    if kind == "system":
        report = read_object(referenced_file(directory, reports["system"]))
        if (
            report.get("status") != "passed"
            or report.get("build_mode") != "immutable-candidate"
            or report.get("candidate_image") != image
            or report.get("release_evidence_eligible") is not True
            or report.get("disposable_volumes_removed") is not True
        ):
            raise ValueError("System proof must pass and remove disposable volumes.")
        for service in ("web", "migrate", "maintenance", "proxy-web"):
            if not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                json_string(json_object(report.get("image_ids", {})).get(service, "")),
            ):
                raise ValueError("System proof is missing required runtime identities.")
        production_name = reports.get("production")
        if not production_name:
            raise ValueError("System proof is missing production installation evidence.")
        production = read_object(referenced_file(directory, production_name))
        validate_production_report(
            production, commit=commit, image=image, version=candidate_version()
        )
        system_ids = json_object(report["image_ids"])
        production_ids = json_object(production["image_ids"])
        if any(
            production_ids.get(service) != system_ids.get(service)
            for service in ("web", "migrate", "maintenance")
        ):
            raise ValueError("Production and system proofs ran different runtime images.")
        return
    report = read_object(referenced_file(directory, reports["execution"]))
    workload = read_object(referenced_file(directory, reports["workload"]))
    if (
        report.get("status") != "passed"
        or report.get("candidate_image") != image
        or report.get("source_commit") != commit
        or report.get("platform") != "linux/amd64"
        or report.get("release_evidence_eligible") is not True
        or report.get("disposable_volumes_removed") is not True
    ):
        raise ValueError("Load execution must pass with exact identity and stack cleanup.")
    if workload.get("passed") is not True or workload.get("fixture_cleanup_complete") is not True:
        raise ValueError("Load workload and fixture cleanup must pass.")
    if (
        workload.get("release_evidence_eligible") is not True
        or type(workload.get("diagnostic_lock_seconds")) is not int
        or workload.get("diagnostic_lock_seconds") != 0
    ):
        raise ValueError("Load workload is diagnostic or not eligible for release evidence.")
    config = json_object(workload.get("configuration", {}))
    if any(config.get(key) != value for key, value in LOAD_CONFIGURATION.items()):
        raise ValueError("Load configuration differs from the fixed release workload.")
    provenance = json_object(workload.get("provenance", {}))
    if provenance.get("build_mode") != "immutable-candidate":
        raise ValueError("Load workload did not use an immutable candidate.")
    helper = json_object(report.get("helper", {}))
    load_images = dict(
        pair.split("=", 1) for pair in json_string(provenance.get("image_ids", "")).split(",")
    )
    if (
        not isinstance(helper, dict)
        or helper.get("imports_passed") is not True
        or helper.get("source_commit") != commit
        or helper.get("platform") != "linux/amd64"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", json_string(helper.get("image_id", "")))
        or helper.get("image_id") != helper_build_id
        or load_images.get("load-profile") != helper.get("image_id")
    ):
        raise ValueError("Load helper identity does not match the verified runtime image.")
    observations = report.get("observations")
    overhead = (
        observations.get("max_sample_overhead_seconds") if isinstance(observations, dict) else None
    )
    pool_samples = observations.get("pool_samples") if isinstance(observations, dict) else None
    services_seen = observations.get("services_seen") if isinstance(observations, dict) else None
    if (
        not isinstance(observations, dict)
        or observations.get("complete") is not True
        or observations.get("collector_failed") is not False
        or type(observations.get("sample_interval_seconds")) is not int
        or observations.get("sample_interval_seconds") != 1
        or type(observations.get("expected_pool_workers")) is not int
        or observations.get("expected_pool_workers") != 4
        or type(pool_samples) is not int
        or pool_samples <= 0
        or not isinstance(services_seen, list)
        or set(json_string(service) for service in services_seen)
        != {"web", "postgres", "maintenance", "load-profile"}
        or isinstance(overhead, bool)
        or not isinstance(overhead, (int, float))
        or not 0 <= overhead < 1
    ):
        raise ValueError("Load observations are incomplete or exceed the sampling limit.")
    observations_path = referenced_file(directory, reports["observations"])
    if not observations_path.read_text(encoding="utf-8").strip():
        raise ValueError("Load observations file is empty.")
