"""Check each Django template without a process pool."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOTS = (ROOT / "templates", ROOT / "website" / "templates")


def main() -> int:
    template_paths = sorted(path for root in TEMPLATE_ROOTS for path in root.rglob("*.html"))
    if not template_paths:
        print("No HTML templates found.", file=sys.stderr)
        return 1
    result = subprocess.run(
        ["djlint", "--check", "--lint", *(str(path.relative_to(ROOT)) for path in template_paths)],
        cwd=ROOT,
        env={**os.environ, "PYTHON_CPU_COUNT": "1"},
        check=False,
    )
    if result.returncode:
        print("Rule: Each Django template must pass the configured djLint format and lint checks.")
        print("Source of truth: docs/design.md")
        print(
            "Recovery: Reformat the named templates, correct lint findings, and rerun "
            "`pdm run python scripts/check_templates.py`.",
            file=sys.stderr,
        )
        return 1
    print(f"Template format and lint checks passed for {len(template_paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
