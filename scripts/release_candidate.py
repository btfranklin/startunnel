"""Record tested images and select exact-commit CI evidence for release promotion."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import (
        JsonObject,
        JsonValue,
        json_array,
        json_integer,
        json_object,
        json_string,
        json_value,
        object_array,
    )
else:
    from json_data import (
        JsonObject,
        JsonValue,
        json_array,
        json_integer,
        json_object,
        json_string,
        json_value,
        object_array,
    )


if TYPE_CHECKING or __package__:
    from .proof_evidence import (
        KINDS,
        checksum,
        read_object,
        record_proof,
        referenced_file,
        validate_proof,
    )
    from .release_record import ROOT, candidate_version
else:
    from proof_evidence import (
        KINDS,
        checksum,
        read_object,
        record_proof,
        referenced_file,
        validate_proof,
    )
    from release_record import ROOT, candidate_version

OFFICIAL_IMAGE = re.compile(r"ghcr\.io/btfranklin/startunnel@sha256:[0-9a-f]{64}")


class ProofReference(TypedDict):
    kind: str
    run_id: int
    attempt: int
    artifact_id: int
    artifact_digest: str | None
    manifest: str
    sha256: str
    status: str


class ProofSelection(TypedDict):
    selected: dict[str, ProofReference]
    history: list[ProofReference]


class CandidateData(TypedDict):
    version: str
    source_commit: str
    image: str
    platform: str
    source_validation_run_id: int
    evidence: NotRequired[dict[str, str]]
    proofs: NotRequired[dict[str, ProofReference]]
    attempts: NotRequired[list[ProofReference]]
    status: NotRequired[str]


def proof_reference(value: object) -> ProofReference:
    data = json_object(value)
    digest = data.get("artifact_digest")
    if digest is not None:
        digest = json_string(digest)
    return {
        "kind": json_string(data["kind"]),
        "run_id": json_integer(data["run_id"]),
        "attempt": json_integer(data["attempt"]),
        "artifact_id": json_integer(data["artifact_id"]),
        "artifact_digest": digest,
        "manifest": json_string(data["manifest"]),
        "sha256": json_string(data["sha256"]),
        "status": json_string(data["status"]),
    }


def candidate_data(value: Mapping[str, object]) -> CandidateData:
    result: CandidateData = {
        "version": json_string(value["version"]),
        "source_commit": json_string(value["source_commit"]),
        "image": json_string(value["image"]),
        "platform": json_string(value["platform"]),
        "source_validation_run_id": json_integer(value["source_validation_run_id"]),
    }
    if "evidence" in value:
        result["evidence"] = {
            key: json_string(item) for key, item in json_object(value["evidence"]).items()
        }
    if "proofs" in value:
        result["proofs"] = {
            key: proof_reference(item) for key, item in json_object(value["proofs"]).items()
        }
    if "attempts" in value:
        result["attempts"] = [proof_reference(item) for item in json_array(value["attempts"])]
    if "status" in value:
        result["status"] = json_string(value["status"])
    return result


def github_object(repository: str, path: str) -> JsonObject:
    return json_object(github_api(repository, path))


def github_pages(repository: str, path: str) -> list[JsonObject]:
    return object_array(github_api(repository, path, paginate=True))


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def validate_candidate(candidate: Mapping[str, object], *, commit: str, version: str) -> None:
    if candidate.get("source_commit") != commit or candidate.get("version") != version:
        raise ValueError("Candidate must match the exact source commit and VERSION.")
    if not OFFICIAL_IMAGE.fullmatch(json_string(candidate.get("image", ""))):
        raise ValueError("Candidate must use the official repository and an immutable digest.")
    if candidate.get("platform") != "linux/amd64":
        raise ValueError("Candidate platform must be linux/amd64.")
    source_run = candidate.get("source_validation_run_id")
    if type(source_run) is not int or source_run <= 0:
        raise ValueError("Candidate must record its successful source CI run.")


def successful_run(
    run: Mapping[str, object], commit: str, workflow: str, *, events: tuple[str, ...]
) -> bool:
    return (
        run.get("head_sha") == commit
        and run.get("head_branch") == "main"
        and run.get("path") == f".github/workflows/{workflow}"
        and run.get("event") in events
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def github_api(repository: str, path: str, *, paginate: bool = False) -> JsonValue:
    command = ["gh", "api", f"repos/{repository}/{path}"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    return json_value(json.loads(subprocess.check_output(command, text=True)))


def source_run(repository: str, commit: str) -> int:
    response = github_object(
        repository,
        f"actions/workflows/ci.yml/runs?branch=main&head_sha={commit}&status=success&per_page=100",
    )
    for run in object_array(response["workflow_runs"]):
        if successful_run(run, commit, "ci.yml", events=("push", "workflow_dispatch")):
            return json_integer(run["id"])
    raise ValueError(
        "Wait for successful source CI on main for this exact commit, or rerun ci.yml."
    )


def validated_run(repository: str, commit: str) -> int | None:
    pages = github_pages(repository, "actions/artifacts?per_page=100")
    for page in pages:
        for artifact in object_array(page["artifacts"]):
            source = json_object(artifact.get("workflow_run", {}))
            if (
                not re.fullmatch(
                    r"release-candidate-[0-9]+-[12]", json_string(artifact.get("name", ""))
                )
                or artifact.get("expired") is not False
                or source.get("head_sha") != commit
                or source.get("head_branch") != "main"
            ):
                continue
            run_id = json_integer(source["id"])
            run = github_object(repository, f"actions/runs/{run_id}")
            if successful_run(run, commit, "release-candidate.yml", events=("workflow_dispatch",)):
                return run_id
    return None


def prepare(repository: str, commit: str, version: str) -> dict[str, int | str]:
    run_id = validated_run(repository, commit)
    if run_id is not None:
        with tempfile.TemporaryDirectory(prefix="startunnel-candidate-") as directory:
            subprocess.run(
                [
                    "gh",
                    "run",
                    "download",
                    str(run_id),
                    "--repo",
                    repository,
                    "--name",
                    json_string(retained_artifact(repository, run_id)["name"]),
                    "--dir",
                    directory,
                ],
                check=True,
            )
            verify_evidence(Path(directory), commit=commit, version=version)
        print(f"Reusing validated candidate: https://github.com/{repository}/actions/runs/{run_id}")
        return {"reuse_run_id": run_id, "source_run_id": ""}
    run_id = source_run(repository, commit)
    print(f"Reusing source CI: https://github.com/{repository}/actions/runs/{run_id}")
    return {"reuse_run_id": "", "source_run_id": run_id}


def retained_artifact(repository: str, run_id: int) -> JsonObject:
    artifacts = [
        item
        for page in github_pages(repository, f"actions/runs/{run_id}/artifacts?per_page=100")
        for item in object_array(page["artifacts"])
        if re.fullmatch(rf"release-candidate-{run_id}-[12]", json_string(item.get("name", "")))
    ]
    if not artifacts:
        raise ValueError("The selected run has no retained candidate artifact.")
    artifact = max(artifacts, key=lambda item: int(json_string(item["name"]).rsplit("-", 1)[1]))
    if artifact.get("expired") is not False:
        raise ValueError("Retained candidate evidence has expired.")
    return artifact


def _download(
    repository: str, run_id: int, artifact: Mapping[str, object], directory: Path
) -> None:
    subprocess.run(
        [
            "gh",
            "run",
            "download",
            str(run_id),
            "--repo",
            repository,
            "--name",
            json_string(artifact["name"]),
            "--dir",
            str(directory),
        ],
        check=True,
    )


def collect_evidence(repository: str, run_id: int, directory: Path) -> None:
    """Select attempts explicitly and retain failed attempts beside them."""
    artifacts = [
        item
        for page in github_pages(repository, f"actions/runs/{run_id}/artifacts?per_page=100")
        for item in object_array(page["artifacts"])
    ]
    named = {json_string(item["name"]): item for item in artifacts}
    if len(named) != len(artifacts):
        raise ValueError("The workflow contains duplicate artifact names.")
    directory.mkdir(parents=True, exist_ok=True)
    candidates = [
        item
        for name, item in named.items()
        if re.fullmatch(rf"candidate-image-{run_id}-[12]", name)
    ]
    if not candidates:
        raise ValueError("The candidate image artifact is missing.")
    candidate_artifact = max(
        candidates, key=lambda item: int(json_string(item["name"]).rsplit("-", 1)[1])
    )
    if candidate_artifact.get("expired") is not False:
        raise ValueError("The candidate image artifact has expired.")
    _download(repository, run_id, candidate_artifact, directory)
    selection: ProofSelection = {"selected": {}, "history": []}
    failed_kinds: list[str] = []
    for kind in KINDS:
        attempts = sorted(
            (
                item
                for name, item in named.items()
                if re.fullmatch(rf"proof-{kind}-{run_id}-[0-9]+", name)
            ),
            key=lambda item: int(json_string(item["name"]).rsplit("-", 1)[1]),
        )
        if not attempts or len(attempts) > 2:
            raise ValueError(f"The {kind} proof is missing or exceeds the bounded retry limit.")
        for artifact in attempts:
            attempt = int(json_string(artifact["name"]).rsplit("-", 1)[1])
            if attempt not in (1, 2) or artifact.get("expired") is not False:
                raise ValueError("A required proof attempt is expired or outside the retry limit.")
            destination = directory / "proofs" / kind / str(attempt)
            _download(repository, run_id, artifact, destination)
            path = destination / f"proof-{kind}.json"
            record = read_object(path)
            if (
                record.get("run_id") != run_id
                or record.get("attempt") != attempt
                or record.get("kind") != kind
            ):
                raise ValueError("Proof attempt metadata differs from its artifact name.")
            reference: ProofReference = {
                "kind": kind,
                "run_id": run_id,
                "attempt": attempt,
                "artifact_id": json_integer(artifact["id"]),
                "artifact_digest": json_string(artifact["digest"])
                if artifact.get("digest") is not None
                else None,
                "manifest": str(path.relative_to(directory)),
                "sha256": checksum(path),
                "status": json_string(record.get("status")),
            }
            selection["history"].append(reference)
        latest = selection["history"][-1]
        if latest["status"] != "passed":
            failed_kinds.append(kind)
        else:
            selection["selected"][kind] = latest
    (directory / "proof-selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=True) + "\n"
    )
    if failed_kinds:
        kinds = ", ".join(failed_kinds)
        raise ValueError(f"The latest proof attempt did not pass for: {kinds}.")


def verify_evidence(directory: Path, *, commit: str, version: str) -> CandidateData:
    candidate = candidate_data(read_object(referenced_file(directory, "candidate-image.json")))
    validate_candidate(candidate, commit=commit, version=version)
    selection_path = referenced_file(directory, "proof-selection.json")
    selection_raw = read_object(selection_path)
    selection: ProofSelection = {
        "selected": {
            key: proof_reference(item)
            for key, item in json_object(selection_raw.get("selected", {})).items()
        },
        "history": [proof_reference(item) for item in json_array(selection_raw.get("history", []))],
    }
    if set(selection["selected"]) != set(KINDS):
        raise ValueError("Candidate evidence must select all required proofs.")
    evidence: dict[str, str] = {"proof-selection.json": checksum(selection_path)}
    runtime_ids: dict[str, str] = {}
    history = selection["history"]
    if not isinstance(history, list) or not history:
        raise ValueError("Candidate proof attempt history is missing.")
    seen: set[tuple[str, int]] = set()
    for reference in history:
        kind = reference["kind"]
        attempt = reference["attempt"]
        if (
            kind not in KINDS
            or type(attempt) is not int
            or attempt not in (1, 2)
            or (kind, attempt) in seen
        ):
            raise ValueError("Candidate proof attempts are ambiguous or outside the retry limit.")
        seen.add((kind, attempt))
        if (
            type(reference.get("artifact_id")) is not int
            or reference["artifact_id"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", reference.get("artifact_digest") or "")
        ):
            raise ValueError("Proof artifact identity and digest are required.")
        manifest_path = referenced_file(directory, reference["manifest"])
        if checksum(manifest_path) != reference["sha256"]:
            raise ValueError("Retained proof manifest checksum differs.")
        proof = read_object(manifest_path)
        if any(
            proof.get(key) != reference.get(key) for key in ("kind", "run_id", "attempt", "status")
        ):
            raise ValueError("Proof reference differs from the retained manifest.")
        if (
            proof.get("source_commit") != commit
            or proof.get("candidate_image") != candidate["image"]
        ):
            raise ValueError("Retained attempt belongs to another source or image.")
        evidence[reference["manifest"]] = checksum(manifest_path)
        for name, expected in json_object(proof.get("files", {})).items():
            path = referenced_file(manifest_path.parent, name)
            if checksum(path) != expected:
                raise ValueError("Retained evidence checksums do not match.")
            evidence[str(path.relative_to(directory))] = json_string(expected)
    for kind, reference in selection["selected"].items():
        if reference not in history or reference["attempt"] != max(
            attempt for recorded_kind, attempt in seen if recorded_kind == kind
        ):
            raise ValueError("A selected proof must be the latest retained attempt.")
        manifest_path = referenced_file(directory, reference["manifest"])
        proof = read_object(manifest_path)
        validate_proof(
            proof, manifest_path.parent, kind=kind, commit=commit, image=candidate["image"]
        )
        if kind == "system":
            system = read_object(
                referenced_file(
                    manifest_path.parent, json_string(json_object(proof["reports"])["system"])
                )
            )
            runtime_ids = {
                key: json_string(item) for key, item in json_object(system["image_ids"]).items()
            }
        if kind == "load":
            load = read_object(
                referenced_file(
                    manifest_path.parent, json_string(json_object(proof["reports"])["workload"])
                )
            )
            load_ids = dict(
                pair.split("=", 1)
                for pair in json_string(json_object(load["provenance"])["image_ids"]).split(",")
            )
    if any(
        runtime_ids.get(service) != runtime_ids["web"]
        for service in ("migrate", "maintenance", "proxy-web")
    ) or any(load_ids.get(service) != runtime_ids["web"] for service in ("web", "maintenance")):
        raise ValueError("System and load proofs ran different runtime images.")
    candidate["evidence"] = evidence
    candidate["proofs"] = selection["selected"]
    candidate["attempts"] = history
    candidate["status"] = "validated"
    retained = directory / "release-candidate.json"
    if retained.exists() and read_object(retained) != candidate:
        raise ValueError("Retained promotion record does not match its evidence checksums.")
    return candidate


def check_registry_digest(output: str, image: str) -> None:
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", output, re.MULTILINE)
    if not match or image.rsplit("@", 1)[1] != match[1]:
        raise ValueError("Promoted version tag does not resolve to the tested candidate digest.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--version", action="store_true")
    action.add_argument("--record", metavar="IMAGE")
    action.add_argument("--verify", type=Path, metavar="DIRECTORY")
    action.add_argument("--find-run", metavar="TAG")
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--collect-run", type=int)
    action.add_argument("--proof", choices=KINDS)
    parser.add_argument("--directory", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--candidate-image", default="")
    parser.add_argument("--proof-status", default="failure")
    action.add_argument("--check-digest", nargs=2, metavar=("INSPECTION_FILE", "IMAGE"))
    parser.add_argument("--source-run-id", type=int)
    args = parser.parse_args()
    try:
        if args.check_digest:
            check_registry_digest(Path(args.check_digest[0]).read_text(), args.check_digest[1])
            return 0
        version = candidate_version()
        if args.version:
            print(version)
            return 0
        commit = git_commit()
        if args.proof:
            record = record_proof(
                args.proof,
                args.directory,
                commit=commit,
                image=args.candidate_image,
                status=args.proof_status,
            )
            return (
                0
                if record["status"] in ("passed", "diagnostic") or args.proof_status != "success"
                else 1
            )
        if args.collect_run:
            collect_evidence(os.environ["GITHUB_REPOSITORY"], args.collect_run, args.directory)
            candidate = verify_evidence(args.directory, commit=commit, version=version)
            (args.directory / "release-candidate.json").write_text(
                json.dumps(candidate, indent=2) + "\n"
            )
        elif args.record:
            candidate = {
                "version": version,
                "source_commit": commit,
                "image": args.record,
                "platform": "linux/amd64",
                "source_validation_run_id": args.source_run_id,
            }
            validate_candidate(candidate, commit=commit, version=version)
            directory = ROOT / "artifacts"
            directory.mkdir(exist_ok=True)
            (directory / "candidate-image.json").write_text(json.dumps(candidate, indent=2) + "\n")
        elif args.verify:
            candidate = verify_evidence(args.verify, commit=commit, version=version)
            (args.verify / "release-candidate.json").write_text(
                json.dumps(candidate, indent=2) + "\n"
            )
        elif args.find_run or args.prepare:
            if args.find_run and args.find_run != version:
                raise ValueError("Release tag must match VERSION.")
            repository = os.environ["GITHUB_REPOSITORY"]
            if args.prepare:
                outputs = prepare(repository, commit, version)
            else:
                run_id = validated_run(repository, commit)
                if run_id is None:
                    raise ValueError(
                        "No retained validated candidate exists for this exact commit."
                    )
                outputs = {
                    "run_id": run_id,
                    "artifact_name": json_string(retained_artifact(repository, run_id)["name"]),
                }
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as destination:
                for name, value in outputs.items():
                    destination.write(f"{name}={value}\n")
            print(f"Candidate selection for {commit}: {outputs}")
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Candidate evidence failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
