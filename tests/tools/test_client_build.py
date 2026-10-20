"""Standalone client metadata in source and release images."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.stamp_client import stamp_client

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli" / "star_tunnel.py"


@pytest.mark.parametrize(
    ("version", "source_revision"),
    [
        ("v0.1.0", "a" * 40),
        ("custom 'quoted' release\nname", 'revision"\\value'),
        ("unreleased", ""),
    ],
)
def test_stamped_client_reports_metadata_outside_the_checkout(
    tmp_path: Path, version: str, source_revision: str
) -> None:
    client = tmp_path / "startunnel"
    client.write_bytes(CLI.read_bytes())
    stamp_client(client, version, source_revision)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
    }
    result = subprocess.run(
        [sys.executable, str(client), "version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "version": version,
        "source_revision": source_revision or None,
        "command_path": str(client.resolve()),
        "sha256": hashlib.sha256(client.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    "source", ["print('no marker')\n", "CLIENT_BUILD = {}\nCLIENT_BUILD = {}\n"]
)
def test_stamping_rejects_a_missing_or_duplicate_build_literal(tmp_path: Path, source: str) -> None:
    client = tmp_path / "startunnel"
    client.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="one CLIENT_BUILD assignment"):
        stamp_client(client, "v0.1.0", "a" * 40)
    assert client.read_text(encoding="utf-8") == source
