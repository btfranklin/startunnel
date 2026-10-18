"""Run the fail-closed StarTunnel release security and supply-chain lane."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .check_secrets import candidate_files
else:
    from check_secrets import candidate_files  # type: ignore[import-not-found,no-redef]

ROOT = Path(__file__).resolve().parents[1]


class SecurityGateError(RuntimeError):
    """A release security tool or assertion failed."""


def _run(
    label: str,
    command: list[str],
    *,
    output: Path | None = None,
) -> None:
    if output is None:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            check=False,
        )
    else:
        with output.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                stdout=stream,
                check=False,
            )
    if result.returncode:
        raise SecurityGateError(f"{label} failed (exit status {result.returncode}).")


def _candidate_tree(destination: Path) -> None:
    for source in candidate_files(ROOT):
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _scan_secrets(source: Path, report: Path, *, archive_depth: int = 0) -> None:
    command = [
        "gitleaks",
        "detect",
        "--no-git",
        "--source",
        str(source),
        "--redact=100",
        "--no-banner",
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    if archive_depth:
        command.extend(["--max-archive-depth", str(archive_depth)])
    _run("Secret scan", command)


def _validate_runtime_image(image: str) -> None:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise SecurityGateError("Docker could not inspect the application image.")
    document = json.loads(result.stdout)
    environment = document[0].get("Config", {}).get("Env", [])
    if any(value.startswith(("OPENAI_API_KEY=", "OPENAI_MODEL=")) for value in environment):
        raise SecurityGateError("The runtime image contains an OpenAI environment setting.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="startunnel-app:security")
    parser.add_argument("--skip-build", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _parser().parse_args(argv)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_directory = ROOT / "artifacts" / "security" / timestamp
    report_directory.mkdir(parents=True, mode=0o700)

    try:
        _run("PDM lock validation", ["pdm", "lock", "--check"])
        _run("npm lock validation", ["npm", "ci", "--ignore-scripts"])
        _run(
            "Bandit",
            [
                "pdm",
                "run",
                "bandit",
                "-q",
                "--severity-level",
                "medium",
                "--confidence-level",
                "medium",
                "-r",
                "src",
                "scripts",
                "examples",
                "-f",
                "json",
                "-o",
                str(report_directory / "bandit.json"),
            ],
        )
        _run(
            "npm dependency audit",
            ["npm", "audit", "--audit-level=high", "--json"],
            output=report_directory / "npm-audit.json",
        )
        with tempfile.TemporaryDirectory(prefix="startunnel-security-") as temporary_name:
            temporary = Path(temporary_name)
            candidate = temporary / "candidate"
            candidate.mkdir()
            _candidate_tree(candidate)
            _run(
                "Candidate key-shape scan",
                [
                    "pdm",
                    "run",
                    "python",
                    "scripts/check_secrets.py",
                    "--root",
                    str(candidate),
                ],
            )
            _scan_secrets(candidate, report_directory / "candidate-secrets.json", archive_depth=2)

            if not arguments.skip_build:
                _run(
                    "Application image build",
                    [
                        "docker",
                        "build",
                        "--target",
                        "runtime",
                        "--tag",
                        arguments.image,
                        ".",
                    ],
                )
            _validate_runtime_image(arguments.image)
            # Resolve immutable candidates directly from the registry. Diagnostic
            # builds use the local daemon. Both lanes retain all vulnerability findings.
            scan_source = "remote" if "@sha256:" in arguments.image else "docker"
            _run(
                "Application image SBOM",
                [
                    "trivy",
                    "image",
                    "--image-src",
                    scan_source,
                    "--timeout",
                    "15m",
                    "--scanners",
                    "vuln",
                    "--format",
                    "cyclonedx",
                    "--output",
                    str(report_directory / "image-sbom.cdx.json"),
                    arguments.image,
                ],
            )
            _run(
                "Application image vulnerability scan",
                [
                    "trivy",
                    "image",
                    "--image-src",
                    scan_source,
                    "--timeout",
                    "15m",
                    "--scanners",
                    "vuln",
                    "--severity",
                    "CRITICAL,HIGH",
                    "--exit-code",
                    "1",
                    "--format",
                    "sarif",
                    "--output",
                    str(report_directory / "image-vulnerabilities.sarif"),
                    arguments.image,
                ],
            )
            image_archive = temporary / "application-image.tar"
            _run(
                "Application image export",
                ["docker", "save", "--output", str(image_archive), arguments.image],
            )
            _scan_secrets(
                image_archive,
                report_directory / "image-layer-secrets.json",
                archive_depth=4,
            )
            history = temporary / "image-history.txt"
            _run(
                "Application image history inspection",
                ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", arguments.image],
                output=history,
            )
            _scan_secrets(history, report_directory / "image-history-secrets.json")

            report_copy = temporary / "current-security-reports"
            shutil.copytree(report_directory, report_copy)
            report_scan = temporary / "report-secrets.json"
            _scan_secrets(report_copy, report_scan)
            shutil.copy2(report_scan, report_directory / "report-secrets.json")
    except (OSError, SecurityGateError, subprocess.SubprocessError, ValueError) as error:
        print(f"Security release gate failed: {error}")
        return 1

    (report_directory / "validated-image.json").write_text(
        json.dumps({"status": "passed", "image": arguments.image}) + "\n", encoding="utf-8"
    )
    print(f"Security release gate passed. Reports: {report_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
