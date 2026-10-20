"""Validate the identity required by the restored v0.2.0 upgrade proof."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import prove_v020_upgrade as proof


def release_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    data = {
        "version": "v0.2.0",
        "platform": "linux/amd64",
        "source_commit": "a" * 40,
        "image": "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64,
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_published_record_requires_tagged_immutable_v020_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    assert proof.published_record(path) == {
        "version": data["version"],
        "source_commit": data["source_commit"],
        "image": data["image"],
    }


def test_candidate_image_requires_exact_source_and_version() -> None:
    labels = {
        "org.opencontainers.image.revision": "a" * 40,
        "org.opencontainers.image.version": "v0.3.0",
    }
    proof.assert_candidate_identity(labels, "a" * 40, "v0.3.0")
    with pytest.raises(ValueError, match="source differs"):
        proof.assert_candidate_identity(labels, "c" * 40, "v0.3.0")
    with pytest.raises(ValueError, match="version differs"):
        proof.assert_candidate_identity(labels, "a" * 40, "v0.4.0")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "v0.1.0", "must be v0.2.0"),
        ("platform", "linux/arm64", "linux/amd64"),
        ("source_commit", "c" * 40, "differs from its tag"),
        ("image", "ghcr.io/btfranklin/startunnel:latest", "pinned"),
    ],
)
def test_published_record_rejects_an_unverified_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        proof.published_record(path)
