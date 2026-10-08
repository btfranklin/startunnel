"""Validate a release specification and write its immutable deployment record."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
IMAGE = re.compile(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}")


def specification(version: str) -> dict[str, Any]:
    if not VERSION.fullmatch(version):
        raise ValueError("Release version must be vMAJOR.MINOR.PATCH.")
    data = json.loads((ROOT / "releases" / f"{version}.json").read_text())
    if not isinstance(data, dict):
        raise ValueError("Release specification must be an object.")
    if data.get("version") != version:
        raise ValueError("Release specification version does not match the tag.")
    predecessors = data.get("supported_from")
    if not isinstance(predecessors, list) or any(
        not isinstance(item, str) or not VERSION.fullmatch(item) or item == version
        for item in predecessors
    ):
        raise ValueError("supported_from must list explicit previous release versions.")
    if not isinstance(data.get("configuration_changes"), list):
        raise ValueError("configuration_changes must be a list.")
    for field in ("database", "rollback"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"Release specification requires {field} instructions.")
    if not (ROOT / "releases" / f"{version}.md").read_text().strip():
        raise ValueError("Release notes must not be empty.")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", metavar="VERSION")
    action.add_argument("--write", nargs=2, metavar=("VERSION", "IMAGE"))
    args = parser.parse_args()
    try:
        version = args.check or args.write[0]
        data = specification(version)
        if args.write:
            image = args.write[1]
            if not IMAGE.fullmatch(image):
                raise ValueError("Official image must be a GHCR reference pinned by digest.")
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip()
            data.update(schema=1, source_commit=commit, image=image, platform="linux/amd64")
            Path("release.json").write_text(json.dumps(data, indent=2) + "\n")
            notes = (ROOT / "releases" / f"{version}.md").read_text()
            notes += f"\n## Deployment record\n\nSource commit: `{commit}`\n\nImage: `{image}`\n"
            Path("release-notes.md").write_text(notes)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release record failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
