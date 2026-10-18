"""Test collection requires PostgreSQL only when its tests are selected."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("postgres", "expression", "status", "selected"),
    [
        (False, "", 0, ["test_plain"]),
        (False, "not postgres", 0, ["test_plain"]),
        (False, "postgres", 4, ["test_postgres"]),
        (True, "postgres", 0, ["test_postgres"]),
    ],
)
def test_backend_collection_selection(
    tmp_path: Path, postgres: bool, expression: str, status: int, selected: list[str]
) -> None:
    tests_root = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text(
        (tests_root / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = tmp_path / "pytest.ini"
    config.write_text(
        "[pytest]\nDJANGO_SETTINGS_MODULE = startunnel.settings.test\n"
        "markers = postgres: requires PostgreSQL\n",
        encoding="utf-8",
    )
    module = tmp_path / "test_backend.py"
    module.write_text(
        "import pytest\ndef test_plain(): pass\n@pytest.mark.postgres\ndef test_postgres(): pass\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tests_root.parent / "src")
    environment["TEST_DATABASE_URL"] = (
        "postgresql://test:test@127.0.0.1:1/collection_only" if postgres else ""
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(config),
        "--collect-only",
        "-q",
        str(module),
    ]
    if expression:
        command.extend(["-m", expression])
    result = subprocess.run(
        command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == status, result.stderr
    nodes = [line.split("::")[-1] for line in result.stdout.splitlines() if "::test_" in line]
    assert nodes == selected
