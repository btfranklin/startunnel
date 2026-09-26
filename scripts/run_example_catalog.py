"""Run every executable non-OpenAI example against one live StarTunnel API."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXAMPLES = (
    ("tutorial_exchange.py", "Your first StarTunnel exchange is complete."),
    ("text_message.py", "The stable address still identifies the tunnel."),
    ("structured_json.py", "The correlated exchange is complete."),
    ("cycle_closure.py", "Cycle closure is complete."),
    ("idempotent_retry.py", "409 idempotency_conflict"),
    ("concurrent_replies.py", "Every concurrent reply has a unique sequence value."),
    ("agent_navigation.py", "Agent navigation and progress recovery are complete."),
    ("search_and_participants.py", "Search and participant discovery are complete."),
    ("cycle_rollover.py", "One stable address now contains two bounded cycles."),
)
KEY_SHAPE = re.compile(r"st_[A-Za-z0-9_-]{43}")


def _safe_run(command: list[str], *, expected: str, timeout: int = 120) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(
            f"{Path(command[-1]).name} failed with exit code {result.returncode}. "
            "Run it in the protected example-test container for a direct recovery message."
        )
    if expected not in combined:
        raise RuntimeError(f"{Path(command[-1]).name} did not print its expected completion label.")
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

    for filename, expected in PYTHON_EXAMPLES:
        _safe_run(
            [sys.executable, str(ROOT / "examples/python" / filename)],
            expected=expected,
        )
        print(f"{filename} passed.")

    _safe_run(
        ["/bin/sh", str(ROOT / "examples/curl/tunnel_exchange.sh")],
        expected="The curl tree exchange is complete.",
    )
    print("tunnel_exchange.sh passed.")
    print("Every non-OpenAI example passed against the public API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
