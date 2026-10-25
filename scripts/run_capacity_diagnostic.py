"""Run one short capacity diagnostic against an unchanged application image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING or __package__:
    from scripts.capacity_probe_overlay import OverlayRecord, apply_overlay
    from scripts.json_data import JsonObject, json_boolean, json_object, json_string
else:
    from capacity_probe_overlay import OverlayRecord, apply_overlay
    from json_data import JsonObject, json_boolean, json_object, json_string

ROOT = Path(__file__).resolve().parents[1]


class CapacityDiagnosticRecord(TypedDict):
    application_source: str
    application_image: str
    driver_source: str
    diagnostic_source: str
    release_evidence_eligible: bool
    traffic_seconds: int
    host_cpu_count: int | None
    host_clock_ticks_per_second: int
    host_page_bytes: int
    status: str
    host_memory_bytes: NotRequired[int]
    overlay: NotRequired[OverlayRecord]
    driver_profile_sha256: NotRequired[str]
    diagnostic_dockerfile_sha256: NotRequired[str]
    workload_process_exit_code: NotRequired[int]
    workload_passed: NotRequired[bool]
    results: NotRequired[JsonObject]
    failure_type: NotRequired[str]


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, text=True)  # nosec B603


def validate_budget(report: Mapping[str, object]) -> None:
    expected = {
        "duration_seconds": 120,
        "sends_per_second": 50,
        "concurrent_activity_readers": 100,
        "credential_count": 500,
        "web_workers": "4",
        "web_database_pool_max_size": "18",
        "max_in_flight_sends": 256,
        "drain_seconds": 30,
        "tunnel_count": 10,
    }
    if report.get("configuration") != expected:
        raise ValueError("The diagnostic workload or resource budget differs.")
    if report.get("disposable_volumes_removed") is not True:
        raise ValueError("The diagnostic stack cleanup did not complete.")
    if report.get("release_evidence_eligible") is not False:
        raise ValueError("The diagnostic must be ineligible for promotion.")
    observations = report.get("observations")
    if not isinstance(observations, dict) or observations.get("complete") is not True:
        raise ValueError("The diagnostic observations did not complete.")


def mark_diagnostic_helper(dockerfile: Path) -> str:
    source = dockerfile.read_text()
    label = 'LABEL net.startunnel.source="${STARTUNNEL_SOURCE_REVISION}"'
    if source.count(label) != 1:
        raise ValueError("The diagnostic helper label anchor is missing or ambiguous.")
    dockerfile.write_text(
        source.replace(label, label + ' net.startunnel.capacity-diagnostic="true"', 1)
    )
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-source", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--project-name", required=True)
    arguments = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", arguments.application_source):
        parser.error("Use the full application source commit.")
    if not re.fullmatch(
        r"ghcr\.io/btfranklin/startunnel@sha256:[0-9a-f]{64}", arguments.candidate_image
    ):
        parser.error("Use the immutable StarTunnel application digest.")
    if not re.fullmatch(r"stload-capacity-[a-z0-9-]{1,40}", arguments.project_name):
        parser.error("Use a new stload-capacity- project name.")
    os.umask(0o077)
    output = ROOT / "artifacts" / arguments.project_name
    output.mkdir(parents=True, exist_ok=False)
    record: CapacityDiagnosticRecord = {
        "application_source": arguments.application_source,
        "application_image": arguments.candidate_image,
        "driver_source": arguments.application_source,
        "diagnostic_source": subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "release_evidence_eligible": False,
        "traffic_seconds": 120,
        "host_cpu_count": os.cpu_count(),
        "host_clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "host_page_bytes": os.sysconf("SC_PAGE_SIZE"),
        "status": "incomplete",
    }
    try:
        if os.cpu_count() != 4:
            raise ValueError("The matched diagnostic requires a four-CPU hosted runner.")
        memory = (
            int(
                next(
                    line.split()[1]
                    for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemTotal:")
                )
            )
            * 1024
        )
        record["host_memory_bytes"] = memory
        if not 15 * 1024**3 <= memory <= 17 * 1024**3:
            raise ValueError("The matched diagnostic requires the recorded 16-GiB host class.")
        with tempfile.TemporaryDirectory(prefix="startunnel-capacity-") as temporary:
            checkout = Path(temporary) / "source"
            run(
                ["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(checkout)],
                cwd=ROOT,
            )
            run(
                ["git", "checkout", "--quiet", "--detach", arguments.application_source],
                cwd=checkout,
            )
            driver_profile = Path(temporary) / "profile.py"
            driver_profile.write_bytes(
                subprocess.check_output(  # nosec B603 B607
                    ["git", "show", f"{arguments.application_source}:tests/load/profile.py"],
                    cwd=ROOT,
                )
            )
            record["driver_profile_sha256"] = hashlib.sha256(
                driver_profile.read_bytes()
            ).hexdigest()
            record["overlay"] = apply_overlay(checkout, driver_profile)
            record["diagnostic_dockerfile_sha256"] = mark_diagnostic_helper(checkout / "Dockerfile")
            try:
                completed = subprocess.run(  # nosec B603
                    [
                        sys.executable,
                        "scripts/run_full_load_profile.py",
                        "--project-name",
                        arguments.project_name,
                        "--candidate-image",
                        arguments.candidate_image,
                    ],
                    cwd=checkout,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    check=False,
                    timeout=1800,
                )
                record["workload_process_exit_code"] = completed.returncode
            finally:
                artifacts = checkout / "artifacts"
                if artifacts.exists():
                    for path in artifacts.iterdir():
                        if path.is_file() and path.suffix in {".json", ".jsonl"}:
                            shutil.copyfile(path, output / path.name)
            report = json_object(
                json.loads((output / f"load-run-{arguments.project_name}.json").read_text())
            )
            validate_budget(report)
            workload = json_object(
                json.loads((output / json_string(report["workload_report"])).read_text())
            )
            if workload.get("fixture_cleanup_complete") is not True:
                raise ValueError("The diagnostic fixture cleanup did not complete.")
            record["workload_passed"] = json_boolean(workload["passed"])
            record["results"] = json_object(workload["results"])
            record["status"] = "collected"
            print("The capacity diagnostic finished. Its workload result is not release evidence.")
            print(json.dumps(record["results"], sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record["failure_type"] = type(error).__name__
        print(
            "The capacity diagnostic did not complete. See the retained records.", file=sys.stderr
        )
        return 1
    finally:
        (output / "capacity-diagnostic.json").write_text(json.dumps(record, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
