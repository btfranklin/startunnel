"""Create or recover a draft without changing a published release or its image."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def check_existing_record(old: dict[str, Any], new: dict[str, Any]) -> None:
    for name in ("version", "source_commit", "image", "platform"):
        if old.get(name) != new.get(name):
            raise ValueError(
                "Existing draft uses a different deployment record; do not replace it."
            )


def existing_draft(tag: str, record: dict[str, Any]) -> bool:
    repository = os.environ["GH_REPO"]
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/releases/tags/{tag}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if "(HTTP 404)" in result.stderr:
            return False
        raise ValueError("Could not inspect the existing release; refusing to proceed.")
    release = json.loads(result.stdout)
    if release.get("draft") is not True:
        raise ValueError("A published release must not be changed.")
    if any(asset["name"] == "release.json" for asset in release.get("assets", [])):
        with tempfile.TemporaryDirectory(prefix="startunnel-draft-") as directory:
            subprocess.run(
                ["gh", "release", "download", tag, "--pattern", "release.json", "--dir", directory],
                check=True,
            )
            old = json.loads((Path(directory) / "release.json").read_text())
            check_existing_record(old, record)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument(
        "--check", action="store_true", help="Check state before registry promotion"
    )
    args = parser.parse_args()
    try:
        record = json.loads(Path("release.json").read_text())
        if args.tag != record.get("version"):
            raise ValueError("Draft tag must match the deployment record.")
        exists = existing_draft(args.tag, record)
        if args.check:
            return 0
        if exists:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "edit",
                    args.tag,
                    "--draft",
                    "--title",
                    f"StarTunnel {args.tag}",
                    "--notes-file",
                    "release-notes.md",
                ],
                check=True,
            )
            subprocess.run(
                ["gh", "release", "upload", args.tag, "release.json", "--clobber"],
                check=True,
            )
        else:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    args.tag,
                    "release.json",
                    "--verify-tag",
                    "--draft",
                    "--title",
                    f"StarTunnel {args.tag}",
                    "--notes-file",
                    "release-notes.md",
                ],
                check=True,
            )
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Draft release failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
