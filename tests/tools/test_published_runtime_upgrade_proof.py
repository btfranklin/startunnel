"""Validate the identity required by the restored published upgrade proof."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import prove_published_upgrade as proof


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


@pytest.mark.parametrize("version", ["v0.2.0", "v0.3.0"])
def test_published_record_requires_tagged_immutable_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    data["version"] = version
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []

    def tagged_revision(command: list[str], **kwargs: Any) -> str:
        calls.append(command)
        return "a" * 40

    monkeypatch.setattr(subprocess, "check_output", tagged_revision)
    assert proof.published_record(path) == {
        "version": data["version"],
        "source_commit": data["source_commit"],
        "image": data["image"],
    }
    assert calls == [["git", "rev-parse", f"{version}^{{commit}}"]]


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
        ("version", "v00.3.0", "must be vMAJOR.MINOR.PATCH"),
        ("version", "v0.3.0-rc1", "must be vMAJOR.MINOR.PATCH"),
        ("version", "--help", "must be vMAJOR.MINOR.PATCH"),
        ("version", None, "must be vMAJOR.MINOR.PATCH"),
        ("platform", "linux/arm64", "linux/amd64"),
        ("source_commit", "c" * 40, "differs from its tag"),
        ("image", "ghcr.io/btfranklin/startunnel:latest", "pinned"),
    ],
)
def test_published_record_rejects_an_unverified_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | None,
    message: str,
) -> None:
    path, data = release_record(tmp_path, monkeypatch)
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        proof.published_record(path)


def test_published_record_requires_an_object(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        proof.published_record(path)
