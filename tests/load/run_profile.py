"""Provision, run, clean, and write the opt-in StarTunnel load proof."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.compose_isolation import termination_signals
from scripts.proof_timing import PhaseTimings

from tests.load.profile import (
    LoadConfig,
    LoadProfileError,
    default_manifest_path,
    finalize_fixture_cleanup,
    load_credential_manifest,
    run_load_profile,
    write_safe_report,
)

ROOT = Path(__file__).resolve().parents[2]


class _Arguments(argparse.Namespace):
    report: Path
    base_url: str
    host_header: str
    duration_seconds: float
    sends_per_second: float
    concurrent_activity_readers: int
    drain_seconds: float
    credentials: int
    diagnostic_lock_seconds: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run provider-neutral HTTP load against a local StarTunnel stack."
    )
    base_url_default: str = os.getenv("STARTUNNEL_BASE_URL", "")
    host_header_default: str = os.getenv("STARTUNNEL_HTTP_HOST", "")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default=base_url_default)
    parser.add_argument("--host-header", default=host_header_default)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sends-per-second", type=float, default=50.0)
    parser.add_argument("--concurrent-activity-readers", type=int, default=100)
    parser.add_argument("--drain-seconds", type=float, default=30.0)
    parser.add_argument("--credentials", type=int, default=500)
    parser.add_argument(
        "--diagnostic-lock-seconds",
        type=int,
        default=int(os.getenv("STARTUNNEL_LOAD_DIAGNOSTIC_LOCK_SECONDS", "0")),
    )
    return parser


def _run_fixture_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["STARTUNNEL_LOAD_TESTS"] = "1"
    return subprocess.run(
        [sys.executable, "manage.py", "provision_load_profile", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )


def _provision(path: Path, arguments: _Arguments) -> None:
    result = _run_fixture_command(
        [
            "--output",
            str(path),
            "--credentials",
            str(arguments.credentials),
        ]
    )
    if result.returncode != 0:
        raise LoadProfileError("Load fixture provisioning failed. Inspect the service logs.")


def _cleanup(path: Path) -> bool:
    try:
        result = _run_fixture_command(["--cleanup-manifest", str(path)])
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0 and not path.exists()


def _config(arguments: _Arguments) -> LoadConfig:
    return LoadConfig(
        duration_seconds=arguments.duration_seconds,
        sends_per_second=arguments.sends_per_second,
        concurrent_activity_readers=arguments.concurrent_activity_readers,
        drain_seconds=arguments.drain_seconds,
    )


def _main(raw_arguments: list[str] | None = None) -> int:
    arguments = _parser().parse_args(raw_arguments, namespace=_Arguments())
    if os.getenv("STARTUNNEL_LOAD_TESTS") != "1":
        print("Set STARTUNNEL_LOAD_TESTS=1 to run the opt-in load proof.", file=sys.stderr)
        return 2
    if not arguments.base_url:
        print("Set STARTUNNEL_BASE_URL or use --base-url.", file=sys.stderr)
        return 2
    if arguments.report.exists() or arguments.report.is_symlink():
        print("The report path already exists. Use a new path.", file=sys.stderr)
        return 2
    manifest_path = default_manifest_path()
    phases = PhaseTimings()
    report: dict[str, object] = {"passed": False, "results": {}, "phases": phases.records}
    driver_path = arguments.report.with_suffix(".driver.jsonl")
    cleanup_complete = False
    exit_code = 1
    report_write_failed = False
    try:
        config = _config(arguments)
        config.validate()
        if not 0 <= arguments.diagnostic_lock_seconds <= 60:
            raise ValueError("Invalid diagnostic lock duration.")
        print("Provisioning disposable load fixtures.", flush=True)
        with phases.phase("fixture_setup"):
            _provision(manifest_path, arguments)
            manifest = load_credential_manifest(manifest_path)
        print("Running the fixed-duration load workload.", flush=True)
        driver_path.parent.mkdir(parents=True, exist_ok=True)
        with driver_path.open("x", encoding="utf-8") as driver_output:

            def observe_driver(sample: dict[str, object]) -> None:
                driver_output.write(json.dumps(sample, sort_keys=True) + "\n")
                driver_output.flush()

            asyncio.run(
                run_load_profile(
                    base_url=arguments.base_url,
                    host_header=arguments.host_header or None,
                    manifest=manifest,
                    config=config,
                    diagnostic_lock_seconds=arguments.diagnostic_lock_seconds,
                    phases=phases,
                    partial_report=report,
                    driver_observer=observe_driver,
                )
            )
        exit_code = 0 if bool(report["passed"]) else 1
    except (
        LoadProfileError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        InterruptedError,
        KeyboardInterrupt,
    ) as error:
        report["failure"] = type(error).__name__
        print("Load proof failed. See the safe report.", file=sys.stderr)
    finally:
        with phases.phase("fixture_cleanup"):
            if manifest_path.exists():
                print("Cleaning disposable load fixtures.", flush=True)
                cleanup_complete = _cleanup(manifest_path)
        if not cleanup_complete:
            phases.records[-1]["status"] = "failed"
        finalize_fixture_cleanup(report, complete=cleanup_complete)
        report["diagnostic_lock_seconds"] = arguments.diagnostic_lock_seconds
        report["release_evidence_eligible"] = (
            bool(report["passed"]) and not arguments.diagnostic_lock_seconds
        )
        report["driver_observations_file"] = driver_path.name
        try:
            write_safe_report(arguments.report, report)
        except OSError:
            print("The safe report could not be written.", file=sys.stderr)
            report_write_failed = True
        else:
            exit_code = 0 if bool(report["passed"]) else 1
    if report_write_failed:
        return 1
    if report:
        outcome = "passed" if report["passed"] else "failed"
        print("Safe load results: " + json.dumps(report["results"], sort_keys=True), flush=True)
        print(f"The load proof {outcome}. Safe report: {arguments.report}")
    elif not cleanup_complete and manifest_path.exists():
        print("Fixture cleanup failed. Inspect the service logs.", file=sys.stderr)
    return exit_code


def main(raw_arguments: list[str] | None = None) -> int:
    with termination_signals():
        return _main(raw_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
