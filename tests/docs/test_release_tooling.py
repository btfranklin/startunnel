"""Keep the public tooling map limited to current repository source."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

ROOT = Path(__file__).resolve().parents[2]


def test_tooling_map_paths_are_relative_and_refer_to_current_source() -> None:
    document = (ROOT / "docs/release-tooling.md").read_text()
    source_roots = {"scripts", "tests", ".github", ".agents", "docker", "releases", "src"}
    for value in re.findall(r"`([^`\n]+)`", document):
        if "/" not in value and "\\" not in value:
            continue
        assert not PurePosixPath(value).is_absolute(), "The public map contains a host path."
        assert not PureWindowsPath(value).is_absolute(), "The public map contains a host path."
        assert not value.startswith("~"), "The public map contains a personal path."
        path = PurePosixPath(value)
        assert ".." not in path.parts, "The public map names a path outside the repository."
        if path.parts[0] in source_roots:
            assert (ROOT / value).exists(), f"The public map names missing source: {value}"
