"""Secret scans use candidate source and explicitly selected reports."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import check_secrets, run_security_release
from tests.structure.workflows import load_workflow


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


def test_failed_security_tool_shows_diagnostics_and_keeps_report(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('report data'); print('tool diagnostic', file=sys.stderr); sys.exit(7)",
    ]
    with pytest.raises(run_security_release.SecurityGateError, match="exit status 7"):
        run_security_release._run("Example scan", command)
    normal = capfd.readouterr()
    assert "report data" in normal.out
    assert "tool diagnostic" in normal.err

    report = tmp_path / "report.json"
    with pytest.raises(run_security_release.SecurityGateError, match="exit status 7"):
        run_security_release._run("Example scan", command, output=report)
    redirected = capfd.readouterr()
    assert redirected.out == ""
    assert "tool diagnostic" in redirected.err
    assert report.read_text(encoding="utf-8") == "report data\n"


def test_security_artifact_upload_runs_after_gate_failure() -> None:
    security = load_workflow("release-validation.yml")["jobs"]["security"]
    upload = next(
        step
        for step in security["steps"]
        if step.get("uses", "").split("@")[0] == "actions/upload-artifact"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["path"].splitlines() == [
        "artifacts/security/",
        "artifacts/proof-security.json",
    ]
    assert upload["with"]["if-no-files-found"] == "warn"


def test_source_failure_stops_before_image_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_security_release, "ROOT", tmp_path)
    monkeypatch.setattr(run_security_release, "_candidate_tree", lambda path: None)
    image_actions: list[str] = []

    def run(label: str, command: list[str], *, output: Path | None = None) -> None:
        del command, output
        if label == "PDM lock validation":
            raise run_security_release.SecurityGateError("lock mismatch")
        image_actions.append(label)

    monkeypatch.setattr(run_security_release, "_run", run)
    monkeypatch.setattr(
        run_security_release,
        "_validate_runtime_image",
        lambda value: image_actions.append("runtime inspection"),
    )
    assert run_security_release.main(["source"]) == 1
    assert "lock mismatch" in capsys.readouterr().out
    assert image_actions == []


@pytest.mark.parametrize("report_scan_fails", [False, True])
def test_source_lane_scans_generated_reports_before_passing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report_scan_fails: bool,
) -> None:
    scans: list[str] = []
    monkeypatch.setattr(run_security_release, "ROOT", tmp_path)
    monkeypatch.setattr(run_security_release, "_candidate_tree", lambda path: None)

    def run(label: str, command: list[str], *, output: Path | None = None) -> None:
        del label, command
        if output:
            output.write_text("Safe source report")

    def scan_secrets(source: Path, report: Path, *, archive_depth: int = 0) -> None:
        del source, archive_depth
        scans.append(report.name)
        if report.name == "report-secrets.json" and report_scan_fails:
            raise run_security_release.SecurityGateError("generated report secret")
        report.write_text("[]")

    monkeypatch.setattr(run_security_release, "_run", run)
    monkeypatch.setattr(run_security_release, "_scan_secrets", scan_secrets)
    result = run_security_release.main(["source"])
    assert result == (1 if report_scan_fails else 0)
    assert scans == ["candidate-secrets.json", "report-secrets.json"]
    output = capsys.readouterr().out
    if report_scan_fails:
        assert "generated report secret" in output
    else:
        assert "Security source gate passed" in output


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "runtime inspection",
        "Application image SBOM",
        "Application image vulnerability scan",
        "Application image export",
        "Application image history inspection",
        "image-layer-secrets.json",
        "image-history-secrets.json",
        "report-secrets.json",
    ],
)
def test_image_lane_is_independent_and_validates_only_after_all_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    image = "registry.example/startunnel@sha256:" + "a" * 64
    commands: list[list[str]] = []
    labels: list[str] = []
    secret_scans: list[tuple[str, int]] = []
    monkeypatch.setattr(run_security_release, "ROOT", tmp_path)
    monkeypatch.setattr(
        run_security_release,
        "_candidate_tree",
        lambda path: pytest.fail("source candidate created"),
    )

    def validate_runtime_image(value: str) -> None:
        del value
        labels.append("runtime inspection")
        if failure == "runtime inspection":
            raise run_security_release.SecurityGateError("invalid runtime environment")

    monkeypatch.setattr(run_security_release, "_validate_runtime_image", validate_runtime_image)

    def scan_secrets(source: Path, report: Path, *, archive_depth: int = 0) -> None:
        secret_scans.append((report.name, archive_depth))
        if failure == report.name:
            raise run_security_release.SecurityGateError("image secret scan failed")
        report.write_text("[]")

    def run(label: str, command: list[str], *, output: Path | None = None) -> None:
        labels.append(label)
        source_tools = {
            "PDM lock validation",
            "npm lock validation",
            "Bandit",
            "npm dependency audit",
        }
        if label in source_tools:
            pytest.fail(f"source action ran in image lane: {label}")
        commands.append(command)
        if failure == label:
            raise run_security_release.SecurityGateError("injected image failure")
        if output:
            output.write_text("Safe test report")

    monkeypatch.setattr(run_security_release, "_scan_secrets", scan_secrets)
    monkeypatch.setattr(run_security_release, "_run", run)
    result = run_security_release.main(["image", "--skip-build", "--image", image])

    assert result == (0 if failure is None else 1)
    assert "runtime inspection" in labels
    if failure is None:
        scan = next(command for command in commands if "--severity" in command)
        assert scan[-1] == image
        assert scan[scan.index("--image-src") + 1] == "remote"
        assert scan[scan.index("--severity") + 1] == "CRITICAL,HIGH"
        assert scan[scan.index("--exit-code") + 1] == "1"
        assert next(command for command in commands if "cyclonedx" in command)[-1] == image
    evidence = list((tmp_path / "artifacts" / "security").glob("*/validated-image.json"))
    assert bool(evidence) is (failure is None)
    if failure is None:
        assert secret_scans == [
            ("image-layer-secrets.json", 4),
            ("image-history-secrets.json", 0),
            ("report-secrets.json", 0),
        ]
