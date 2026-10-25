"""Compare cold and warm proof helper builds without application traffic."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        required_output,
        termination_signals,
        validate_project_name,
    )
    from scripts.proof_timing import PhaseTimings
else:
    from compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        required_output,
        termination_signals,
        validate_project_name,
    )
    from proof_timing import PhaseTimings

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("full_cold", "tests", True),
    ("focused_cold", "load-profile", True),
    ("focused_warm", "load-profile", False),
)
FOCUSED_PREFLIGHT = (
    "import importlib.util,os; import tests.load.run_profile; "
    "assert os.getuid()!=0; "
    "assert all(importlib.util.find_spec(name) is None for name in "
    "('pytest','ruff','mypy','playwright','openapi_spec_validator')); "
    "assert all(not os.path.exists(path) for path in "
    "('/usr/local/bin/pdm','/usr/local/bin/node','/usr/local/bin/perfect-doc'))"
)


def _image(stack: IsolatedComposeProject, image: str) -> dict[str, object]:
    raw = required_output(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{.Id}}|{{.Size}}|{{index .Config.Labels "net.startunnel.source"}}|'
            "{{.Os}}/{{.Architecture}}",
            image,
        ],
        environment=stack.environment,
        label="the benchmark helper image",
    ).split("|")
    if (
        len(raw) != 4
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw[0])
        or not raw[1].isdigit()
        or not re.fullmatch(r"[0-9a-f]{40,64}", raw[2])
        or not re.fullmatch(r"[a-z0-9_]+/[a-z0-9_]+", raw[3])
    ):
        raise LaneError("benchmark image inspection", "The helper identity is invalid.")
    return {
        "image_id": raw[0],
        "size_bytes": int(raw[1]),
        "source_revision": raw[2],
        "platform": raw[3],
    }


def _preflight(stack: IsolatedComposeProject) -> None:
    # Select the just-built service only after its image ID and source checks.
    # Compose owns the one-off container, so its final cleanup also covers interruption.
    stack.run(
        "Check focused helper imports and tools",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "/app/.venv/bin/python",
        "load-profile",
        "-c",
        FOCUSED_PREFLIGHT,
        timeout_seconds=60,
    )


def benchmark(
    stack: IsolatedComposeProject, report: dict[str, object], timings: PhaseTimings
) -> None:
    """Keep source, inputs, and daemon fixed across all three build cases."""
    cases: dict[str, dict[str, object]] = {}
    report["cases"] = cases
    reference: dict[str, object] | None = None
    for name, service, no_cache in CASES:
        with timings.phase(name):
            provenance = stack.build_services((service,), no_cache=no_cache, timeout_seconds=2400)
            identity = {
                key: provenance.get(key) for key in ("source_revision", "platform", "hashes")
            }
            if reference is None:
                reference = identity
            elif identity != reference:
                raise LaneError("benchmark inputs", "The helper build inputs changed.")
            image_ids = provenance.get("image_ids")
            image_id = image_ids.get(service) if isinstance(image_ids, dict) else None
            if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise LaneError("benchmark identity", "The helper build did not record its image.")
            inspected = _image(stack, image_id)
            if (
                inspected["source_revision"] != identity["source_revision"]
                or inspected["platform"] != identity["platform"]
            ):
                raise LaneError("benchmark identity", "The helper image differs from its build.")
            provenance_file = provenance.get("provenance_file")
            if not isinstance(provenance_file, str) or not re.fullmatch(
                r"proof-build-[A-Za-z0-9.]+Z\.json", provenance_file
            ):
                raise LaneError("benchmark provenance", "The build report name is invalid.")
            case = {
                **inspected,
                "build": provenance,
                "build_provenance_file": provenance_file,
                "tool_preflight_passed": None,
            }
            cases[name] = case
            if service == "load-profile":
                _preflight(stack)
                case["tool_preflight_passed"] = True
    report["inputs"] = reference
    report["comparison"] = {
        "focused_image_smaller": int(str(cases["focused_cold"]["size_bytes"]))
        < int(str(cases["full_cold"]["size_bytes"])),
        "warm_cache_hits": cases["focused_warm"]["build"]["cache_hits"],  # type: ignore[index]
        "timing_scope": "Buildx wall time on this Docker daemon; no application traffic",
    }
    warm = cases["focused_warm"]["build"]
    if not isinstance(warm, dict) or int(str(warm.get("cache_hits", 0))) <= 0:
        raise LaneError("benchmark warm cache", "The warm build did not record a cache hit.")
    if not report["comparison"]["focused_image_smaller"]:  # type: ignore[index]
        raise LaneError("benchmark image size", "The focused helper image is not smaller.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name")
    arguments = parser.parse_args(argv)
    os.umask(0o077)
    try:
        project = validate_project_name(
            arguments.project_name or generated_project_name("helper-benchmark")
        )
    except IsolationError:
        print("Use an approved disposable project name.", file=sys.stderr)
        return 2
    directory = ROOT / "artifacts" / f"proof-helper-benchmark-{project}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    report_path = directory / "benchmark-summary.json"
    timings = PhaseTimings()
    report: dict[str, object] = {
        "status": "failed",
        "release_evidence_eligible": False,
        "project": project,
        "phases": timings.records,
        "disposable_volumes_removed": False,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
    }
    stack: IsolatedComposeProject | None = None
    complete = False
    try:
        with termination_signals():
            contexts = ExitStack()
            try:
                with timings.phase("isolation"):
                    stack = contexts.enter_context(
                        IsolatedComposeProject(project, profiles=("load",))
                    )
                benchmark(stack, report, timings)
                complete = True
            finally:
                with timings.phase("cleanup"):
                    contexts.close()
                report["disposable_volumes_removed"] = bool(stack and stack.cleanup_complete)
    except (
        IsolationError,
        LaneError,
        InterruptedError,
        KeyboardInterrupt,
        OSError,
        ValueError,
    ) as error:
        complete = False
        report["failure"] = error.step if isinstance(error, LaneError) else type(error).__name__
        print("The helper benchmark failed. See the safe summary.", file=sys.stderr)
    finally:
        report["status"] = (
            "passed" if complete and report["disposable_volumes_removed"] else "failed"
        )
        cases = report.get("cases")
        if isinstance(cases, dict):
            for case in cases.values():
                name = case.get("build_provenance_file") if isinstance(case, dict) else None
                if not isinstance(name, str):
                    continue
                source = ROOT / "artifacts" / name
                try:
                    if source.is_symlink():
                        raise OSError("Invalid build report.")
                    shutil.copyfile(source, directory / name)
                except OSError:
                    report["status"] = "failed"
                    report["build_provenance_copy_failed"] = True
        with report_path.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
    print(f"Helper benchmark summary: {report_path}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
