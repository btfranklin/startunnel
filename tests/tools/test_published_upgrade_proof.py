"""Validate the identity used by the restored published-runtime proof."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import prove_admin_upgrade as proof


def release_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    data = {
        "version": "v0.1.0",
        "platform": "linux/amd64",
        "source_commit": "a" * 40,
        "image": "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64,
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(data))
    return path, data


def test_published_baseline_uses_recorded_immutable_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    assert proof.published_record(path) == data


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "v0.2.0", "must be v0.1.0"),
        ("platform", "linux/arm64", "linux/amd64"),
        ("source_commit", "c" * 40, "differs from"),
        ("image", "ghcr.io/btfranklin/startunnel:latest", "pinned"),
    ],
)
def test_published_baseline_rejects_an_unverified_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message):
        proof.published_record(path)
