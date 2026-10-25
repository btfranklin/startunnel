"""Validate a release specification and write its immutable deployment record."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

if TYPE_CHECKING or __package__:
    from .json_data import (
        JsonValue,
        json_object,
        json_string,
    )
else:
    from json_data import (
        JsonValue,
        json_object,
        json_string,
    )


if TYPE_CHECKING or __package__:
    from .start_production import validate_image_reference
else:
    from start_production import validate_image_reference

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
PLATFORM = re.compile(r"linux/[a-z0-9_]+(?:/[a-z0-9_]+)?")


class ReleaseSpecification(TypedDict):
    version: str
    supported_from: list[str]
    configuration_changes: list[JsonValue]
    database: str
    rollback: str


class ReleaseRecord(ReleaseSpecification):
    schema: int
    source_commit: str
    image: str
    platform: str
    validation_run_id: NotRequired[int]


def specification(version: str) -> ReleaseSpecification:
    if not VERSION.fullmatch(version):
        raise ValueError("Release version must be vMAJOR.MINOR.PATCH.")
    data = json_object(json.loads((ROOT / "releases" / f"{version}.json").read_text()))
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
        if not isinstance(data.get(field), str) or not json_string(data[field]).strip():
            raise ValueError(f"Release specification requires {field} instructions.")
    if not (ROOT / "releases" / f"{version}.md").read_text().strip():
        raise ValueError("Release notes must not be empty.")
    # All required fields and their element types were checked above.
    return cast(ReleaseSpecification, data)


def candidate_version() -> str:
    version = "v" + (ROOT / "VERSION").read_text().strip()
    specification(version)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", metavar="VERSION")
    action.add_argument("--write", nargs=2, metavar=("VERSION", "IMAGE"))
    parser.add_argument(
        "--validation-run", type=int, help="Successful CI run that validated this image"
    )
    parser.add_argument("--platform", default="linux/amd64", help="Image platform")
    args = parser.parse_args()
    try:
        version = args.check or args.write[0]
        data = specification(version)
        if version != candidate_version():
            raise ValueError("Release tag must match VERSION.")
        if args.write:
            image = args.write[1]
            validate_image_reference(image)
            if not PLATFORM.fullmatch(args.platform):
                raise ValueError("Platform must be a Linux image platform.")
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip()
            record: ReleaseRecord = {
                "version": data["version"],
                "supported_from": data["supported_from"],
                "configuration_changes": data["configuration_changes"],
                "database": data["database"],
                "rollback": data["rollback"],
                "schema": 1,
                "source_commit": commit,
                "image": image,
                "platform": args.platform,
            }
            if args.validation_run is not None:
                if args.validation_run <= 0:
                    raise ValueError("Validation run ID must be positive.")
                record["validation_run_id"] = args.validation_run
            Path("release.json").write_text(json.dumps(record, indent=2) + "\n")
            notes = (ROOT / "releases" / f"{version}.md").read_text()
            notes += f"\n## Deployment record\n\nSource commit: `{commit}`\n\nImage: `{image}`\n"
            Path("release-notes.md").write_text(notes)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Release record failed: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
