"""Run every executable non-OpenAI example against one live StarTunnel API."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXAMPLES = (
    "tutorial_exchange.py",
    "text_message.py",
    "structured_json.py",
    "cycle_closure.py",
    "idempotent_retry.py",
    "concurrent_replies.py",
    "agent_navigation.py",
    "search_and_participants.py",
    "cycle_rollover.py",
    "beacon_discovery.py",
)
KEY_SHAPE = re.compile(r"st_[A-Za-z0-9_-]{43}")


def _safe_run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(
            f"{Path(command[-1]).name} failed with exit code {result.returncode}. "
            "Run it in the protected example-test container for a direct recovery message."
        )
    if KEY_SHAPE.search(combined):
        raise RuntimeError(f"{Path(command[-1]).name} printed a complete agent key.")


def main() -> int:
    required = {
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
        "STARTUNNEL_RECEIVER_KEYS",
    }
    if missing := sorted(name for name in required if not os.getenv(name)):
        raise SystemExit("Missing protected example setting names: " + ", ".join(missing))

    for filename in PYTHON_EXAMPLES:
        _safe_run([sys.executable, str(ROOT / "examples/python" / filename)])
        print(f"{filename} passed.")

    _safe_run([sys.executable, str(ROOT / "cli/star_tunnel.py"), "tutorial"])
    print("CLI tutorial passed.")
    print("Every non-OpenAI example passed against the public API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
