"""Regenerate OpenAPI in a temporary directory and compare the committed artifact."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "generated/openapi.json"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="startunnel-openapi-") as directory:
        generated = Path(directory) / "openapi.json"
        result = subprocess.run(
            [
                sys.executable,
                "manage.py",
                "export_openapi",
                "--output",
                str(generated),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            print("Rule: The OpenAPI schema must generate successfully.", file=sys.stderr)
            print("Source of truth: docs/api.md", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            print(
                "Recovery: Run `pdm run openapi` and correct the reported schema error.",
                file=sys.stderr,
            )
            return 1
        if not COMMITTED.exists() or COMMITTED.read_bytes() != generated.read_bytes():
            print(
                "Rule: The committed OpenAPI artifact must match the API routes.", file=sys.stderr
            )
            print("Source of truth: generated/openapi.json", file=sys.stderr)
            print("Problem: API schema output has changed.", file=sys.stderr)
            print(
                "Recovery: Run `pdm run openapi`, inspect the diff, and update docs/api.md.",
                file=sys.stderr,
            )
            return 1
    print("OpenAPI drift check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
