"""The entry point disables file metrics when its directory is empty or absent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("present", [False, True])
def test_empty_metrics_directory_uses_memory_counters(tmp_path: Path, present: bool) -> None:
    environment = {"PATH": os.environ["PATH"]}
    if present:
        environment["PROMETHEUS_MULTIPROC_DIR"] = ""
    result = subprocess.run(
        [
            "/bin/sh",
            str(ROOT / "docker/app/entrypoint.sh"),
            sys.executable,
            "-c",
            "import os; assert 'PROMETHEUS_MULTIPROC_DIR' not in os.environ; "
            "from prometheus_client import Counter; Counter('entrypoint_probe', 'Probe').inc()",
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("*.db")) == []
