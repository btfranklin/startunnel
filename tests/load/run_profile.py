"""Provision, run, clean, and write the opt-in StarTunnel load proof."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run provider-neutral HTTP load against a local StarTunnel stack."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("STARTUNNEL_BASE_URL", ""))
    parser.add_argument("--host-header", default=os.getenv("STARTUNNEL_HTTP_HOST", ""))
    parser.add_argument("--metrics-token", default=os.getenv("STARTUNNEL_METRICS_TOKEN", ""))
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sends-per-second", type=float, default=50.0)
    parser.add_argument("--concurrent-activity-readers", type=int, default=100)
    parser.add_argument("--drain-seconds", type=float, default=30.0)
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--credentials", type=int, default=500)
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.getenv("STARTUNNEL_LOAD_SEED", "20260810")),
    )
    parser.add_argument(
        "--skip-cleanup-lag-check",
        action="store_true",
        help="Allow a small focused run without the internal cleanup-lag metric.",
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


def _provision(path: Path, arguments: argparse.Namespace) -> None:
    result = _run_fixture_command(
        [
            "--output",
            str(path),
            "--users",
            str(arguments.users),
            "--credentials",
            str(arguments.credentials),
        ]
    )
    if result.returncode != 0:
        raise LoadProfileError("Load fixture provisioning failed. Inspect the service logs.")


def _cleanup(path: Path) -> bool:
    try:
        result = _run_fixture_command(["--cleanup-manifest", str(path)])
    except subprocess.SubprocessError:
        return False
    return result.returncode == 0 and not path.exists()


def _config(arguments: argparse.Namespace) -> LoadConfig:
    return LoadConfig(
        duration_seconds=arguments.duration_seconds,
        sends_per_second=arguments.sends_per_second,
        concurrent_activity_readers=arguments.concurrent_activity_readers,
        drain_seconds=arguments.drain_seconds,
        require_cleanup_lag=not arguments.skip_cleanup_lag_check,
        seed=arguments.seed,
    )


def main(raw_arguments: list[str] | None = None) -> int:
    arguments = _parser().parse_args(raw_arguments)
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
    report: dict[str, object] | None = None
    cleanup_complete = False
    exit_code = 1
    report_write_failed = False
    try:
        config = _config(arguments)
        config.validate()
        _provision(manifest_path, arguments)
        manifest = load_credential_manifest(manifest_path)
        report = asyncio.run(
            run_load_profile(
                base_url=arguments.base_url,
                host_header=arguments.host_header or None,
                metrics_token=arguments.metrics_token or None,
                manifest=manifest,
                config=config,
            )
        )
        exit_code = 0 if bool(report["passed"]) else 1
    except (LoadProfileError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"Load proof failed: {error}", file=sys.stderr)
    finally:
        if manifest_path.exists():
            cleanup_complete = _cleanup(manifest_path)
        if report is not None:
            finalize_fixture_cleanup(report, complete=cleanup_complete)
            try:
                write_safe_report(arguments.report, report)
            except OSError as error:
                print(f"The safe report could not be written: {error}", file=sys.stderr)
                report_write_failed = True
            else:
                exit_code = 0 if bool(report["passed"]) else 1
    if report_write_failed:
        return 1
    if report is not None:
        outcome = "passed" if report["passed"] else "failed"
        print(f"The load proof {outcome}. Safe report: {arguments.report}")
    elif not cleanup_complete and manifest_path.exists():
        print("Fixture cleanup failed. Inspect the service logs.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
