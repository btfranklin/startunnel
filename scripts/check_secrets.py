"""Reject key-shaped literals from repository content without reading local secret files."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "staticfiles", "__pycache__"}
EXCLUDED_NAMES = {".env", ".env.tutorial", "pdm.lock", "package-lock.json"}
PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "StarTunnel key": re.compile(r"\bst_[A-Za-z0-9_-]{43}\b"),
}
OCR_PATTERNS = {
    "OpenAI key-like OCR": re.compile(
        r"(?i)\bs[\t ]*k[\t ]*[-_][\t ]*"
        r"(?:p[\t ]*r[\t ]*o[\t ]*j[\t ]*[-_][\t ]*)?"
        r"(?:[a-z0-9_-][\t ]*){20,}"
    ),
    "StarTunnel key-like OCR": re.compile(
        r"(?i)\bs[\t ]*t[\t ]*[_-][\t ]*(?:[a-z0-9_-][\t ]*){30,}"
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--ocr", action="store_true", help="Also detect spaced OCR key shapes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    roots = arguments.roots or [ROOT]
    patterns = {**PATTERNS, **OCR_PATTERNS} if arguments.ocr else PATTERNS
    findings: list[str] = []
    for root in roots:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            print(f"Secret scan root does not exist: {root}", file=sys.stderr)
            return 1
        for path in sorted(resolved_root.rglob("*")):
            if (
                not path.is_file()
                or path.name in EXCLUDED_NAMES
                or EXCLUDED_PARTS.intersection(path.parts)
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError, OSError:
                continue
            for label, pattern in patterns.items():
                if pattern.search(text):
                    relative = path.relative_to(resolved_root)
                    findings.append(f"{resolved_root.name}/{relative} contains a {label} literal.")
    if findings:
        print(
            "Rule: No secret can enter Git, examples, static assets, or reports.", file=sys.stderr
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
