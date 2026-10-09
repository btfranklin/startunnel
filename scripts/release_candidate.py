"""Record tested images and select exact-commit CI evidence for release promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
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


def successful_run(response: dict[str, Any], commit: str) -> int:
    for run in response.get("workflow_runs", []):
        if (
            run.get("head_sha") == commit
            and run.get("head_branch") == "main"
            and run.get("event") == "push"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ):
            return int(run["id"])
    raise ValueError("No completed successful CI push run on main exists for this exact commit.")


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
    action.add_argument("--check-digest", nargs=2, metavar=("INSPECTION_FILE", "IMAGE"))
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
        elif args.find_run:
            if args.find_run != version:
                raise ValueError("Release tag must match VERSION.")
            repository = os.environ["GITHUB_REPOSITORY"]
            output = subprocess.check_output(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/actions/workflows/ci.yml/runs"
                    f"?branch=main&event=push&head_sha={commit}&status=success&per_page=100",
                ],
                text=True,
            )
            run_id = successful_run(json.loads(output), commit)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as destination:
                destination.write(f"run_id={run_id}\n")
            print(f"Using successful CI run {run_id} for {commit}.")
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Candidate evidence failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
