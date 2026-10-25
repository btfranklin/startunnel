"""Check manual current-release references against the release specification."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from . import release_record
else:
    import release_record

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/btfranklin/startunnel/blob"
RELEASE_VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def _read(relative_path: str) -> str:
    path = ROOT / relative_path
    try:
        return path.read_text()
    except OSError as error:
        raise ValueError(f"{relative_path}: could not read file ({error}).") from error


def _require_unique_version_statement(
    relative_path: str, content: str, statement: str, version: str
) -> None:
    versions = re.findall(rf"{re.escape(statement)}\s+({RELEASE_VERSION.pattern})\.", content)
    if versions != [version]:
        raise ValueError(
            f"{relative_path}: expected one {statement!r} statement for {version}; "
            f"found {versions}."
        )


def _check_supported_predecessors(content: str, predecessors: list[str]) -> None:
    normalized = " ".join(content.split())
    matches = re.findall(r"The supported published predecessors are (.+?)\.(?=\s|$)", normalized)
    empty_count = normalized.count("No published predecessor is supported.")
    if not predecessors:
        if empty_count != 1 or matches:
            raise ValueError(
                "docs/setup/maintain.md: expected one 'No published predecessor is "
                "supported.' statement for an empty supported_from list."
            )
        return

    actual = [RELEASE_VERSION.findall(match) for match in matches]
    if empty_count or actual != [predecessors]:
        raise ValueError(
            "docs/setup/maintain.md: supported published predecessors must match "
            f"release specification {predecessors}; found {actual}."
        )


def check_references() -> None:
    """Check current-release guide text and links without rejecting historical versions."""
    version = release_record.candidate_version()
    specification = release_record.specification(version)
    readme = _read("README.md")
    _require_unique_version_statement("README.md", readme, "These guides describe", version)

    setup = _read("docs/setup/README.md")
    _require_unique_version_statement(
        "docs/setup/README.md", setup, "These guides describe", version
    )

    maintain = _read("docs/setup/maintain.md")
    _require_unique_version_statement(
        "docs/setup/maintain.md",
        maintain,
        "These steps update the server and its team's clients to",
        version,
    )
    _check_supported_predecessors(maintain, specification["supported_from"])
    _require_unique_link(
        "docs/setup/maintain.md",
        maintain,
        f"{REPOSITORY}/{version}/releases/{version}.md",
    )
    _require_unique_link(
        "docs/setup/maintain.md",
        maintain,
        f"https://github.com/btfranklin/startunnel/releases/tag/{version}",
    )

    policy = _read("docs/releases.md")
    _require_unique_version_statement(
        "docs/releases.md", policy, "The current release specification is", version
    )
    current_path_versions = re.findall(
        rf"The standard released path to\s+`({RELEASE_VERSION.pattern})`", policy
    )
    if current_path_versions != [version]:
        raise ValueError(
            "docs/releases.md: the standard released path must name the current release "
            f"{version}; found {current_path_versions}."
        )

    notes_path = f"releases/{version}.md"
    notes = _read(notes_path)
    headings = re.findall(r"^# StarTunnel (v\S+)\s*$", notes, flags=re.MULTILINE)
    if headings != [version]:
        raise ValueError(
            f"{notes_path}: expected one release heading for {version}; found {headings}."
        )
    for target in (
        f"{REPOSITORY}/{version}/docs/setup/maintain.md#update-the-application",
        f"{REPOSITORY}/{version}/docs/setup/README.md",
    ):
        _require_unique_link(notes_path, notes, target)


def _require_unique_link(relative_path: str, content: str, target: str) -> None:
    if content.count(target) != 1:
        raise ValueError(f"{relative_path}: expected one current release link to {target!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        check_references()
    except (ValueError, OSError) as error:
        print(f"Release reference check failed: {error}", file=sys.stderr)
        return 1
    print("Current release references match VERSION and the release specification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
