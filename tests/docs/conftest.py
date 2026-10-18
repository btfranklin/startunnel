"""Test locations for executable clients outside the noexec temporary mount."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def executable_dir() -> Iterator[Path]:
    artifacts = PROJECT_ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="cli-test-", dir=artifacts) as directory:
        yield Path(directory)
