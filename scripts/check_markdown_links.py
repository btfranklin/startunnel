"""Check local Markdown links in repository source and Git-free exports."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_secrets import candidate_files

ROOT = Path(__file__).resolve().parents[1]


def check(root: Path) -> int:
    files = sorted(path for path in candidate_files(root) if path.suffix.casefold() == ".md")
    if not files:
        return 0
    return subprocess.run(
        ["linkbust", "--no-color", *(str(path.relative_to(root)) for path in files)],
        cwd=root,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(check(ROOT))
