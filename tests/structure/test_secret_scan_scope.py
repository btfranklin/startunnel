"""Secret scans use candidate source and explicitly selected reports."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import check_secrets, run_security_release


@pytest.mark.skipif(shutil.which("git") is None, reason="Git-checkout scan needs the Git CLI.")
def test_ignored_private_inputs_do_not_block_candidate_but_source_and_reports_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("artifacts/\n*.key\n")
    secret_shape = "s" + "k-" + "A" * 32
    report = tmp_path / "artifacts" / "security" / "current"
    report.mkdir(parents=True)
    (report / "report.json").write_text(secret_shape)
    (tmp_path / "operator.key").write_text(secret_shape)
    source = tmp_path / "module.py"
    source.write_text("SAFE = True\n")
    monkeypatch.setattr(check_secrets, "ROOT", tmp_path)

    assert check_secrets.main([]) == 0
    assert check_secrets.main(["--root", str(report)]) == 1
    assert "report.json" in capsys.readouterr().err

    source.write_text(f'VALUE = "{secret_shape}"\n')
    assert check_secrets.main([]) == 1
    assert "module.py" in capsys.readouterr().err


def test_git_free_source_export_scans_source_without_ignored_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / ".env.example").write_text("STARTUNNEL_APP_IMAGE=example\n")
    (export / "module.py").write_text("SAFE = True\n")
    ignored = export / "artifacts" / "security"
    ignored.mkdir(parents=True)
    (ignored / "report.json").write_text("s" + "k-" + "A" * 32)
    monkeypatch.setattr(check_secrets, "ROOT", export)
    monkeypatch.setattr(run_security_release, "ROOT", export)

    assert check_secrets.main([]) == 0
    candidate = tmp_path / "candidate"
    run_security_release._candidate_tree(candidate)
    assert (candidate / "module.py").exists()
    assert (candidate / ".env.example").exists()
    assert not (candidate / "artifacts").exists()

    (export / "module.py").write_text('VALUE = "' + "s" + "k-" + "A" * 32 + '"\n')
    assert check_secrets.main([]) == 1
