"""Record tested images and select exact-commit CI evidence for release promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .release_record import ROOT, candidate_version
else:
    from release_record import ROOT, candidate_version  # type: ignore[import-not-found,no-redef]

OFFICIAL_IMAGE = re.compile(r"ghcr\.io/btfranklin/startunnel@sha256:[0-9a-f]{64}")


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def validate_candidate(candidate: dict[str, Any], *, commit: str, version: str) -> None:
    if candidate.get("source_commit") != commit or candidate.get("version") != version:
        raise ValueError("Candidate must match the exact source commit and VERSION.")
    if not OFFICIAL_IMAGE.fullmatch(candidate.get("image", "")):
        raise ValueError("Candidate must use the official repository and an immutable digest.")
    if candidate.get("platform") != "linux/amd64":
        raise ValueError("Candidate platform must be linux/amd64.")
    source_run = candidate.get("source_validation_run_id")
    if type(source_run) is not int or source_run <= 0:
        raise ValueError("Candidate must record its successful source CI run.")


def successful_run(
    run: dict[str, Any], commit: str, workflow: str, *, events: tuple[str, ...]
) -> bool:
    return (
        run.get("head_sha") == commit
        and run.get("head_branch") == "main"
        and run.get("path") == f".github/workflows/{workflow}"
        and run.get("event") in events
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def github_api(repository: str, path: str, *, paginate: bool = False) -> Any:
    command = ["gh", "api", f"repos/{repository}/{path}"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    return json.loads(subprocess.check_output(command, text=True))


def source_run(repository: str, commit: str) -> int:
    response = github_api(
        repository,
        f"actions/workflows/ci.yml/runs?branch=main&head_sha={commit}&status=success&per_page=100",
    )
    for run in response["workflow_runs"]:
        if successful_run(run, commit, "ci.yml", events=("push", "workflow_dispatch")):
            return int(run["id"])
    raise ValueError(
        "Wait for successful source CI on main for this exact commit, or rerun ci.yml."
    )


def validated_run(repository: str, commit: str) -> int | None:
    pages = github_api(
        repository, "actions/artifacts?name=release-candidate&per_page=100", paginate=True
    )
    for page in pages:
        for artifact in page["artifacts"]:
            source = artifact.get("workflow_run", {})
            if (
                artifact.get("name") != "release-candidate"
                or artifact.get("expired") is not False
                or source.get("head_sha") != commit
                or source.get("head_branch") != "main"
            ):
                continue
            run_id = int(source["id"])
            run = github_api(repository, f"actions/runs/{run_id}")
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
                    "release-candidate",
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


def one_report(directory: Path, pattern: str) -> tuple[Path, dict[str, Any]]:
    paths = list(directory.rglob(pattern))
    if len(paths) != 1:
        raise ValueError(f"Expected exactly one {pattern} report in the candidate evidence.")
    data = json.loads(paths[0].read_text())
    if not isinstance(data, dict):
        raise ValueError("Evidence report must be a JSON object.")
    return paths[0], data


def verify_evidence(directory: Path, *, commit: str, version: str) -> dict[str, Any]:
    _, candidate = one_report(directory, "candidate-image.json")
    validate_candidate(candidate, commit=commit, version=version)
    security_path, security = one_report(directory, "validated-image.json")
    system_path, system = one_report(directory, "system-proof-*.json")
    load_image_path, load_image = one_report(directory, "validated-load-image.json")
    load_path, load = one_report(directory, "load-profile-*.json")
    for report in (security, load_image):
        if report.get("status") != "passed" or report.get("image") != candidate["image"]:
            raise ValueError("Security and load evidence must validate the candidate digest.")
    if (
        system.get("status") != "passed"
        or system.get("build_mode") != "immutable-candidate"
        or system.get("candidate_image") != candidate["image"]
        or system.get("release_evidence_eligible") is not True
        or system.get("disposable_volumes_removed") is not True
    ):
        raise ValueError("System proof must pass against the candidate and clean up its volumes.")
    if (
        load.get("passed") is not True
        or load.get("fixture_cleanup_complete") is not True
        or load.get("provenance", {}).get("build_mode") != "immutable-candidate"
    ):
        raise ValueError("Load proof must pass against the candidate with fixture cleanup.")
    web_image = system.get("image_ids", {}).get("web", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", web_image):
        raise ValueError("System proof must record the actual runtime image ID.")
    load_ids = dict(pair.split("=", 1) for pair in load["provenance"]["image_ids"].split(","))
    if any(load_ids.get(service) != web_image for service in ("web", "maintenance")):
        raise ValueError("System and load proofs ran different runtime images.")
    candidate["evidence"] = {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (security_path, system_path, load_image_path, load_path)
    }
    candidate["status"] = "validated"
    retained = directory / "release-candidate.json"
    if retained.exists():
        old = json.loads(retained.read_text())
        if old != candidate:
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
        if args.record:
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
                outputs = {"run_id": run_id}
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
