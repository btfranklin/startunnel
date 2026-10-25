"""Run typed steps used by the release promotion workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from . import release_candidate, release_record
else:
    import release_candidate
    import release_record


def record_candidate(
    evidence_directory: Path,
    validation_run_id: int,
    *,
    output_directory: Path = Path("."),
    github_output: Path,
) -> str:
    """Verify retained evidence, then write the record and workflow image output."""
    if validation_run_id <= 0:
        raise ValueError("Validation run ID must be positive.")
    candidate = release_candidate.verify_evidence(
        evidence_directory,
        commit=release_candidate.git_commit(),
        version=release_record.candidate_version(),
    )
    (evidence_directory / "release-candidate.json").write_text(
        json.dumps(candidate, indent=2) + "\n"
    )
    image = candidate["image"]
    release_record.write_record(
        candidate["version"],
        image,
        validation_run_id=validation_run_id,
        platform=candidate["platform"],
        output_directory=output_directory,
    )
    with github_output.open("a") as output:
        output.write(f"image={image}\n")
    return image


def merge_release_notes(
    generated_summary: str,
    repository: str,
    validation_run_id: int,
    *,
    notes_path: Path = Path("release-notes.md"),
) -> None:
    """Add an optional generated summary and exact validation run URL."""
    if validation_run_id <= 0:
        raise ValueError("Validation run ID must be positive.")
    if not repository or "/" not in repository:
        raise ValueError("GitHub repository must be owner/repository.")
    reviewed = notes_path.read_text().rstrip()
    summary = generated_summary.strip()
    if summary:
        reviewed = summary + "\n\n" + reviewed
    run_url = f"https://github.com/{repository}/actions/runs/{validation_run_id}"
    notes_path.write_text(reviewed + "\n\nValidation: " + run_url + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    record = actions.add_parser("record", help="verify the candidate and write its record")
    record.add_argument("--evidence", type=Path, required=True)
    record.add_argument("--validation-run", type=int, required=True)
    notes = actions.add_parser("notes", help="merge generated notes with reviewed instructions")
    notes.add_argument("--validation-run", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.action == "record":
            output = os.environ.get("GITHUB_OUTPUT")
            if not output:
                raise ValueError("GITHUB_OUTPUT is required for the record command.")
            record_candidate(
                args.evidence,
                args.validation_run,
                github_output=Path(output),
            )
        else:
            repository = os.environ.get("GITHUB_REPOSITORY", "")
            generated_summary = os.environ.get("GENERATED_NOTES", "")
            merge_release_notes(generated_summary, repository, args.validation_run)
    except (KeyError, OSError, subprocess.CalledProcessError, TypeError, ValueError) as error:
        print(f"Release workflow failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
