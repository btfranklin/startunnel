"""Run the fail-closed StarTunnel release security and supply-chain lane."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT_NAMES = ("artifacts", "output", "playwright-report", "test-results")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})
MAX_EVIDENCE_FILE_BYTES = 100 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class SecurityGateError(RuntimeError):
    """A release security tool or assertion failed."""


def _run(
    label: str,
    command: list[str],
    *,
    output: Path | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    if output is None:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment or os.environ.copy(),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        with output.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment or os.environ.copy(),
                text=True,
                stdout=stream,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    if result.returncode:
        raise SecurityGateError(f"{label} failed. Run the named tool directly for details.")


def _candidate_tree(destination: Path) -> list[Path]:
    images: list[Path] = []
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        source = ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if target.suffix.lower() in IMAGE_SUFFIXES:
            images.append(target)
    return images


def _evidence_tree(destination: Path, *, current_report: Path) -> list[Path]:
    """Copy bounded ignored evidence so reports and captures cannot escape scans."""

    images: list[Path] = []
    total_bytes = 0
    current_report = current_report.resolve()
    for root_name in EVIDENCE_ROOT_NAMES:
        source_root = ROOT / root_name
        if not source_root.exists():
            continue
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise SecurityGateError(f"Evidence contains a symbolic link: {source}")
            if not source.is_file() or current_report in source.resolve().parents:
                continue
            size = source.stat().st_size
            if size > MAX_EVIDENCE_FILE_BYTES:
                raise SecurityGateError(f"Evidence file is too large for review: {source}")
            total_bytes += size
            if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                raise SecurityGateError("Ignored evidence exceeds the two-GiB review boundary.")
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if target.suffix.lower() in IMAGE_SUFFIXES:
                images.append(target)
    return images


def _extract_image_text(images: list[Path], destination: Path) -> None:
    """Use OCR so a secret rendered in a screenshot remains machine-checkable."""

    destination.mkdir(parents=True, exist_ok=True)
    if not images:
        return
    if shutil.which("tesseract") is None:
        raise SecurityGateError("Screenshot OCR needs the tesseract command.")
    for index, source in enumerate(sorted(images)):
        output_base = destination / f"capture-{index:04d}"
        _run(
            "Screenshot OCR",
            ["tesseract", str(source), str(output_base), "-l", "eng"],
        )


def _png_chunk(name: bytes, data: bytes) -> bytes:
    payload = name + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def _write_scanner_test_png(path: Path, text: str) -> None:
    """Render a small secret-shape calibration image with no image dependency."""

    glyphs = {
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    }
    scale = 10
    margin = 30
    gap = scale * 2
    width = margin * 2 + len(text) * (5 * scale + gap)
    height = margin * 2 + 7 * scale
    pixels = [bytearray([255] * width) for _ in range(height)]
    x = margin
    for character in text:
        for row_index, row in enumerate(glyphs[character]):
            for column_index, enabled in enumerate(row):
                if enabled != "1":
                    continue
                for y_offset in range(scale):
                    start = x + column_index * scale
                    pixels[margin + row_index * scale + y_offset][start : start + scale] = bytes(
                        [0] * scale
                    )
        x += 5 * scale + gap
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _expect_secret_shape_rejection(root: Path, *, ocr: bool = False) -> None:
    command = ["pdm", "run", "python", "scripts/check_secrets.py", "--root", str(root)]
    if ocr:
        command.append("--ocr")
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        raise SecurityGateError("The secret scanner failed its release calibration.")


def _verify_evidence_scanners(temporary: Path) -> None:
    sentinel = "s" + "k-" + "A" * 32
    report = temporary / "ignored-report-test"
    report.mkdir()
    (report / "report.json").write_text(f'{{"value":"{sentinel}"}}\n', encoding="utf-8")
    _expect_secret_shape_rejection(report)

    capture = temporary / "rendered-secret-test.png"
    _write_scanner_test_png(capture, sentinel.upper())
    ocr = temporary / "rendered-secret-ocr"
    _extract_image_text([capture], ocr)
    _expect_secret_shape_rejection(ocr, ocr=True)


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


def _has_git_history() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


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
            "Python dependency audit",
            [
                "pdm",
                "run",
                "pip-audit",
                "--strict",
                "--format",
                "json",
                "--output",
                str(report_directory / "pip-audit.json"),
            ],
        )
        _run(
            "npm dependency audit",
            ["npm", "audit", "--audit-level=high", "--json"],
            output=report_directory / "npm-audit.json",
        )
        _run(
            "Python SBOM",
            [
                "pdm",
                "run",
                "cyclonedx-py",
                "environment",
                ".venv/bin/python",
                "--pyproject",
                "pyproject.toml",
                "--output-reproducible",
                "--output-format",
                "JSON",
                "--output-file",
                str(report_directory / "python-sbom.cdx.json"),
            ],
        )
        _run("Repository secret-shape check", ["pdm", "run", "python", "scripts/check_secrets.py"])

        with tempfile.TemporaryDirectory(prefix="startunnel-security-") as temporary_name:
            temporary = Path(temporary_name)
            _verify_evidence_scanners(temporary)
            candidate = temporary / "candidate"
            candidate.mkdir()
            candidate_images = _candidate_tree(candidate)
            evidence = temporary / "ignored-evidence"
            evidence.mkdir()
            evidence_images = _evidence_tree(evidence, current_report=report_directory)
            ocr = temporary / "capture-ocr"
            _extract_image_text(candidate_images + evidence_images, ocr)
            _run(
                "Candidate and evidence key-shape scan",
                [
                    "pdm",
                    "run",
                    "python",
                    "scripts/check_secrets.py",
                    "--root",
                    str(candidate),
                    "--root",
                    str(evidence),
                ],
            )
            _run(
                "Screenshot OCR key-shape scan",
                [
                    "pdm",
                    "run",
                    "python",
                    "scripts/check_secrets.py",
                    "--root",
                    str(ocr),
                    "--ocr",
                ],
            )
            _scan_secrets(candidate, report_directory / "candidate-secrets.json", archive_depth=2)
            _scan_secrets(evidence, report_directory / "evidence-secrets.json", archive_depth=2)
            _scan_secrets(ocr, report_directory / "capture-ocr-secrets.json")

            if _has_git_history():
                _run(
                    "Git history secret scan",
                    [
                        "gitleaks",
                        "detect",
                        "--source",
                        str(ROOT),
                        "--redact=100",
                        "--no-banner",
                        "--report-format",
                        "json",
                        "--report-path",
                        str(report_directory / "git-history-secrets.json"),
                    ],
                )
            else:
                (report_directory / "git-history-secrets.json").write_text(
                    '{"status":"not-applicable","reason":"no Git commit exists"}\n',
                    encoding="utf-8",
                )

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
