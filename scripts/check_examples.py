"""Check that public examples compile and do not expose bearer values."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TUTORIAL_RUNNER = ROOT / "scripts/run_tutorial.py"
AGENT_CLIENT = ROOT / "cli/star_tunnel.py"


def main() -> int:
    failures: list[str] = []
    if not TUTORIAL_RUNNER.is_file():
        failures.append("scripts/run_tutorial.py does not exist.")
    else:
        try:
            ast.parse(
                TUTORIAL_RUNNER.read_text(encoding="utf-8"),
                filename=str(TUTORIAL_RUNNER),
            )
        except SyntaxError as error:
            failures.append(f"scripts/run_tutorial.py does not compile: {error}")
    if not AGENT_CLIENT.is_file():
        failures.append("cli/star_tunnel.py does not exist.")
    for path in sorted(EXAMPLES.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            failures.append(f"{path.relative_to(ROOT)} does not compile: {error}")
    tutorial = (EXAMPLES / "python/tutorial_exchange.py").read_text(encoding="utf-8")
    env_paths = [EXAMPLES / ".env.tutorial.example"]
    if (ROOT / ".env.tutorial.example").exists():
        env_paths.append(ROOT / ".env.tutorial.example")
    for env_path in env_paths:
        text = env_path.read_text(encoding="utf-8")
        if re.search(r"\bst_[A-Za-z0-9_-]{43}\b", text) or re.search(
            r"\bsk-[A-Za-z0-9_-]{20,}\b", text
        ):
            failures.append(
                f"{env_path.relative_to(ROOT)} contains a value that looks like a real key."
            )
    if re.search(
        r"print\([^\n]*(?:SENDER_KEY|RECEIVER_KEY|snapshot_cursor|page_cursor|activity_cursor)",
        tutorial,
    ):
        failures.append("The tutorial script prints a key or private cursor expression.")
    if failures:
        print(
            "Rule: Example programs must compile and keep keys private.",
            file=sys.stderr,
        )
        print("Source of truth: examples/README.md", file=sys.stderr)
        for failure in failures:
            print(f"Problem: {failure}", file=sys.stderr)
        print(
            "Recovery: Correct the named file, then run "
            "`pdm run python scripts/check_examples.py`.",
            file=sys.stderr,
        )
        return 1
    print("Example source check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
