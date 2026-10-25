"""Check private production secret creation and refusal to replace existing files."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from scripts import generate_production_secrets as secrets_tool


def test_generator_creates_independent_private_secrets_without_printing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "private"
    monkeypatch.setattr(sys, "argv", ["generate_production_secrets", "--directory", str(directory)])
    assert secrets_tool.main() == 0
    values = {path.name: path.read_text().strip() for path in directory.iterdir()}
    assert set(values) == {
        "startunnel_secret_key",
        "startunnel_api_key_pepper",
        "startunnel_idempotency_secret",
        "startunnel_metrics_token",
        "postgres_admin_password",
        "postgres_runtime_password",
    }
    assert len(set(values.values())) == len(values)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    for name in (
        "startunnel_api_key_pepper",
        "startunnel_idempotency_secret",
        "startunnel_metrics_token",
    ):
        value = values[name]
        assert len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))) == 32
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in values.values())


def test_generator_refuses_existing_inputs_before_writing_any_new_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir()
    existing = directory / "postgres_runtime_password"
    existing.write_text("existing private input\n")
    monkeypatch.setattr(sys, "argv", ["generate_production_secrets", "--directory", str(directory)])
    with pytest.raises(SystemExit) as stopped:
        secrets_tool.main()
    assert stopped.value.code == 2
    assert list(directory.iterdir()) == [existing]
    assert existing.read_text() == "existing private input\n"


@pytest.mark.parametrize("symlink", [False, True])
def test_secret_writer_never_replaces_an_existing_file_or_symlink(
    tmp_path: Path, symlink: bool
) -> None:
    target = tmp_path / "existing"
    target.write_text("keep this input\n")
    destination = tmp_path / "secret" if symlink else target
    if symlink:
        destination.symlink_to(target)
    with pytest.raises(OSError):
        secrets_tool.write_secret(destination, "replacement")
    assert target.read_text() == "keep this input\n"
    assert destination.is_symlink() is symlink
