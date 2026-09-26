"""Run one StarTunnel test lane in a new, disposable Compose project."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        termination_signals,
    )
    from scripts.run_resource_measurement import main as run_resource_measurement
    from scripts.system_proof import run_system_proof, write_system_report
else:
    from compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        termination_signals,
    )
    from run_resource_measurement import main as run_resource_measurement
    from system_proof import run_system_proof, write_system_report


@dataclass(frozen=True, slots=True)
class LaneSpec:
    profiles: tuple[str, ...]
    command: tuple[str, ...]
    timeout_seconds: int


LANES = {
    "canonical": LaneSpec((), ("run", "--build", "--rm", "tests"), 2_400),
    "postgres": LaneSpec(
        (),
        (
            "run",
            "--build",
            "--rm",
            "tests",
            "pdm",
            "run",
            "pytest",
            "tests/integration",
            "-m",
            "postgres",
        ),
        2_400,
    ),
    "examples": LaneSpec(
        ("examples",),
        ("run", "--build", "--rm", "example-tests"),
        2_400,
    ),
    "agents": LaneSpec(
        ("agents",),
        ("run", "--build", "--rm", "agent-e2e"),
        2_400,
    ),
    "browser": LaneSpec(
        ("browser",),
        ("run", "--build", "--rm", "browser-tests"),
        4_800,
    ),
    "live-agents": LaneSpec(
        ("live-agents",),
        ("run", "--build", "--rm", "live-agent-e2e"),
        3_600,
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=(*LANES, "system", "resources"))
    parser.add_argument("--project-name")
    parser.add_argument(
        "--allow-openai-cost",
        action="store_true",
        help="Permit the live-agents lane to receive the two OpenAI settings.",
    )
    parser.add_argument(
        "--reuse-current-images",
        action="store_true",
        help=(
            "Skip the system lane's no-cache build after verifying all required "
            "images. This diagnostic result is not fresh-build release evidence."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _parser().parse_args(argv)
    if arguments.lane == "resources":
        if arguments.project_name or arguments.allow_openai_cost or arguments.reuse_current_images:
            print("The resources lane does not accept other lane options.", file=sys.stderr)
            return 2
        return run_resource_measurement([])
    project_name = arguments.project_name or generated_project_name(arguments.lane)
    if arguments.lane == "live-agents" and not arguments.allow_openai_cost:
        print("The live-agents lane needs --allow-openai-cost.", file=sys.stderr)
        return 2
    if arguments.allow_openai_cost and arguments.lane != "live-agents":
        print("--allow-openai-cost is valid only for the live-agents lane.", file=sys.stderr)
        return 2
    if arguments.reuse_current_images and arguments.lane != "system":
        print("--reuse-current-images is valid only for the system lane.", file=sys.stderr)
        return 2
    include_openai = arguments.lane == "live-agents"
    if include_openai:
        missing = [name for name in ("OPENAI_API_KEY", "OPENAI_MODEL") if not os.getenv(name)]
        if missing:
            print("The live-agents lane is missing: " + ", ".join(missing), file=sys.stderr)
            return 2

    system_report: dict[str, Any] | None = None
    try:
        profiles: tuple[str, ...] = (
            ("browser", "examples", "agents") if arguments.lane == "system" else ()
        )
        if arguments.lane in LANES:
            profiles = LANES[arguments.lane].profiles
        with (
            IsolatedComposeProject(
                project_name,
                profiles=profiles,
                include_openai=include_openai,
            ) as stack,
            termination_signals(),
        ):
            if arguments.lane == "system":
                system_report = run_system_proof(
                    stack,
                    build_images=not arguments.reuse_current_images,
                )
            else:
                specification = LANES[arguments.lane]
                stack.run(
                    f"Run {arguments.lane} lane",
                    *specification.command,
                    timeout_seconds=specification.timeout_seconds,
                )
                print(f"The {arguments.lane} lane passed.")
        if system_report is not None:
            system_report["finished_at"] = datetime.now(UTC).isoformat()
            system_report["disposable_volumes_removed"] = True
            report_path = write_system_report(system_report)
            if system_report.get("release_evidence_eligible") is False:
                print(
                    "System behavior proof passed with current images. "
                    "This is not fresh-build release evidence. "
                    f"Safe report: {report_path}"
                )
            else:
                print(f"System proof passed. Safe report: {report_path}")
    except (InterruptedError, IsolationError, LaneError) as error:
        print(f"Isolated lane failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
