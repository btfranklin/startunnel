"""The maintenance health check is local, quiet, and deterministic."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.maintenance_liveness import publish_local_maintenance_heartbeat

ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK = ROOT / "docker/app/maintenance-healthcheck.sh"


def _run_healthcheck(
    heartbeat: Path,
    *,
    modified_at: int | None,
    current_time: int = 1_000,
    max_age_seconds: int = 20,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if modified_at is not None:
        heartbeat.touch()
        os.utime(heartbeat, (modified_at, modified_at))
    return subprocess.run(
        [
            "sh",
            str(HEALTHCHECK),
            str(heartbeat),
            str(max_age_seconds),
            str(current_time),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_local_heartbeat_creates_and_refreshes_file(tmp_path: Path) -> None:
    heartbeat = tmp_path / "maintenance-heartbeat"

    publish_local_maintenance_heartbeat(heartbeat)
    assert heartbeat.is_file()

    os.utime(heartbeat, (100, 100))
    publish_local_maintenance_heartbeat(heartbeat)
    assert heartbeat.stat().st_mtime > 100


def test_local_heartbeat_file_failure_is_not_hidden(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")

    with pytest.raises(OSError):
        publish_local_maintenance_heartbeat(parent_file / "heartbeat")


@pytest.mark.parametrize(
    "modified_at,expected_return_code",
    [
        (1_000, 0),
        (980, 0),
        (979, 1),
        (1_001, 1),
        (None, 1),
    ],
)
def test_healthcheck_accepts_only_a_current_file(
    tmp_path: Path,
    modified_at: int | None,
    expected_return_code: int,
) -> None:
    result = _run_healthcheck(
        tmp_path / "maintenance-heartbeat",
        modified_at=modified_at,
    )

    assert result.returncode == expected_return_code
    assert result.stdout == ""
    assert result.stderr == ""


def test_healthcheck_does_not_touch_prometheus_files(tmp_path: Path) -> None:
    metrics_directory = tmp_path / "metrics"
    metrics_directory.mkdir()
    sentinel = metrics_directory / "worker.db"
    sentinel.write_text("keep", encoding="utf-8")
    environment = os.environ.copy()
    environment["PROMETHEUS_MULTIPROC_DIR"] = str(metrics_directory)

    result = _run_healthcheck(
        tmp_path / "maintenance-heartbeat",
        modified_at=1_000,
        environment=environment,
    )

    assert result.returncode == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
