"""Reject key-shaped literals from repository content without reading local secret files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "staticfiles", "__pycache__"}
EXCLUDED_NAMES = {".env", ".env.tutorial", "pdm.lock", "package-lock.json"}
EXPORT_EXCLUDED_DIRECTORIES = EXCLUDED_PARTS | {
    ".hypothesis",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".playwright-cli",
    "artifacts",
    "output",
    "playwright-report",
    "test-results",
    "dist",
    "build",
    "htmlcov",
    "media",
    "backups",
    "archive-data",
    "secrets",
    "live-agent-results",
}
EXPORT_EXCLUDED_NAMES = {"release.json", "release-notes.md"}
EXPORT_EXCLUDED_SUFFIXES = {".env", ".key", ".pem", ".p12", ".pfx", ".dump", ".backup", ".log"}
PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "StarTunnel key": re.compile(r"\bst(?:a)?_[A-Za-z0-9_-]{43}\b"),
}


def candidate_files(root: Path) -> list[Path]:
    """List tracked and nonignored source, or source files in a Git-free export."""

    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        git_files = [
            root / Path(os.fsdecode(raw_path))
            for raw_path in result.stdout.split(b"\0")
            if raw_path
        ]
        return [path for path in git_files if path.is_file()]

    files: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name not in EXPORT_EXCLUDED_DIRECTORIES]
        for name in filenames:
            if (
                name in EXPORT_EXCLUDED_NAMES
                or (name.startswith(".env") and not name.endswith(".example"))
                or name.startswith("production.env")
                or Path(name).suffix in EXPORT_EXCLUDED_SUFFIXES
            ):
                continue
            path = Path(directory) / name
            if path.is_file():
                files.append(path)
    return files


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    roots = arguments.roots or [ROOT]
    findings: list[str] = []
    for root in roots:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            print(f"Secret scan root does not exist: {root}", file=sys.stderr)
            return 1
        paths = (
            candidate_files(resolved_root) if arguments.roots is None else resolved_root.rglob("*")
        )
        for path in sorted(paths):
            if not path.is_file() or (arguments.roots is None and path.name in EXCLUDED_NAMES):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(text):
                    relative = path.relative_to(resolved_root)
                    findings.append(f"{resolved_root.name}/{relative} contains a {label} literal.")
    if findings:
        print(
            "Rule: No secret can enter Git, shipped source, or selected reports.", file=sys.stderr
        )
        print("Source of truth: docs/security.md", file=sys.stderr)
        for finding in findings:
            print(f"Problem: {finding}", file=sys.stderr)
        print(
            "Recovery: Remove the value, rotate it if it was real, and run "
            "`pdm run python scripts/check_secrets.py`.",
            file=sys.stderr,
        )
        return 1
    print("Repository secret-shape check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
