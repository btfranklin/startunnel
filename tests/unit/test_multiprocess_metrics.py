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
    pool_sample = (
        "import os; from core.pool_metrics import sample_pool_stats; "
        "sample_pool_stats({{'pool_size': {size}, 'pool_available': 1, "
        "'requests_waiting': {waiting}, 'requests_queued': {queued}, "
        "'requests_wait_ms': 10, 'requests_errors': 1}}); print(os.getpid())"
    )
    first_pool = _run(pool_sample.format(size=3, waiting=2, queued=4), environment)
    second_pool = _run(pool_sample.format(size=5, waiting=1, queued=6), environment)
    rendered = _run(
        "from core.metrics import render_metrics; print(render_metrics().decode(), end='')",
        environment,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first_pool.returncode == 0, first_pool.stderr
    assert second_pool.returncode == 0, second_pool.stderr
    assert rendered.returncode == 0, rendered.stderr
    assert (
        'startunnel_api_requests_total{operation="read_tree",status_class="2xx"} 3.0'
        in rendered.stdout
    )
    assert "startunnel_database_pool_size 8.0" in rendered.stdout
    assert "startunnel_database_pool_waiting 3.0" in rendered.stdout
    assert "startunnel_database_pool_queued 10.0" in rendered.stdout

    mark_dead = (
        "from prometheus_client import multiprocess; "
        "multiprocess.mark_process_dead({pid})"
    )
    first_dead = _run(mark_dead.format(pid=first_pool.stdout.strip()), environment)
    after_one_worker = _run(
        "from core.metrics import render_metrics; print(render_metrics().decode(), end='')",
        environment,
    )
    assert first_dead.returncode == 0, first_dead.stderr
    assert "startunnel_database_pool_size 5.0" in after_one_worker.stdout
    assert "startunnel_database_pool_waiting 1.0" in after_one_worker.stdout

    second_dead = _run(mark_dead.format(pid=second_pool.stdout.strip()), environment)
    after_all_workers = _run(
        "from core.metrics import render_metrics; print(render_metrics().decode(), end='')",
        environment,
    )
    assert second_dead.returncode == 0, second_dead.stderr
    assert "startunnel_database_pool_size 5.0" not in after_all_workers.stdout
