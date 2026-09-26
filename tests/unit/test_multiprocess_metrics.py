"""Prometheus output combines metrics from all web worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_multiprocess_metrics_aggregate_two_worker_shards(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    increment = (
        "from core.metrics import API_REQUESTS; "
        "API_REQUESTS.labels(operation='read_tree', status_class='2xx').inc({amount})"
    )

    first = _run(increment.format(amount=1), environment)
    second = _run(increment.format(amount=2), environment)
    rendered = _run(
        "from core.metrics import render_metrics; print(render_metrics().decode(), end='')",
        environment,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert rendered.returncode == 0, rendered.stderr
    assert (
        'startunnel_api_requests_total{operation="read_tree",status_class="2xx"} 3.0'
        in rendered.stdout
    )
