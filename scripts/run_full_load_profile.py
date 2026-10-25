"""Run the fixed load proof in a new, disposable Compose project."""

from __future__ import annotations

import argparse
import json
import os
import re
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
    from scripts.load_observations import LoadObservations
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
    from load_observations import LoadObservations
    from proof_timing import PhaseTimings

SERVICES = ("postgres", "web", "maintenance", "load-profile")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-name",
        help="Use an unused disposable project name with an approved test prefix.",
    )
    parser.add_argument("--candidate-image", help="Load-test the immutable release image.")
    parser.add_argument(
        "--reuse-current-images",
        action="store_true",
        help=(
            "Skip the image build after verifying every required image. "
            "The safe report records this build mode."
        ),
    )
    parser.add_argument(
        "--diagnostic-lock-seconds",
        type=int,
        default=0,
        help="Hold a database lock for 1 to 60 seconds. This run cannot be release evidence.",
    )
    return parser


def _image_id(stack: IsolatedComposeProject, service: str) -> str:
    image_references = stack.capture(
        f"the {service} image",
        "images",
        "--quiet",
        service,
    ).splitlines()
    if not image_references:
        raise LaneError("load image IDs", f"Docker reported no image for {service}.")
    identifier = required_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_references[0]],
        environment=stack.environment,
        label=f"the {service} image ID",
    )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
        raise LaneError("load image IDs", f"Docker returned an invalid image ID for {service}.")
    return identifier


def _assert_reusable_images(stack: IsolatedComposeProject) -> None:
    try:
        model = json.loads(
            stack.capture(
                "the reusable load image model",
                "config",
                "--format",
                "json",
            )
        )
    except json.JSONDecodeError as error:
        raise LaneError(
            "load image inspection",
            "Docker returned an invalid Compose model.",
        ) from error
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict):
        raise LaneError(
            "load image inspection",
            "The Compose model does not contain services.",
        )
    for service in SERVICES:
        definition = services.get(service)
        image = definition.get("image") if isinstance(definition, dict) else None
        if not isinstance(image, str) or not image:
            raise LaneError(
                "load image inspection",
                f"The Compose model does not name an image for {service}.",
            )
        identifier = required_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            environment=stack.environment,
            label=f"the reusable {service} image",
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise LaneError(
                "load image inspection",
                f"Docker returned an invalid reusable image ID for {service}.",
            )


def _verify_helper(stack: IsolatedComposeProject, commit: str, platform: str) -> dict[str, object]:
    identifier = _image_id(stack, "load-profile")
    identity = required_output(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "net.startunnel.source"}}|{{.Os}}/{{.Architecture}}',
            identifier,
        ],
        environment=stack.environment,
        label="the load helper source and platform",
    )
    if identity != f"{commit}|{platform}":
        raise LaneError("load helper identity", "The load helper source or platform differs.")
    stack.run(
        "Check load helper imports",
        "run",
        "--rm",
        "--no-deps",
        "load-profile",
        "/app/.venv/bin/python",
        "-c",
        "import tests.load.run_profile; import django,httpx,psycopg",
        timeout_seconds=60,
    )
    return {
        "image_id": identifier,
        "source_commit": commit,
        "platform": platform,
        "imports_passed": True,
    }


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _parser().parse_args(argv)
    if arguments.candidate_image and arguments.reuse_current_images:
        print("Candidate proof cannot use --reuse-current-images.", file=sys.stderr)
        return 2
    if not 0 <= arguments.diagnostic_lock_seconds <= 60:
        print(
            "The diagnostic lock must be from 1 through 60 seconds, or 0 to disable it.",
            file=sys.stderr,
        )
        return 2
    try:
        project_name = validate_project_name(
            arguments.project_name or generated_project_name("load")
        )
    except IsolationError:
        print("Use an approved disposable project name.", file=sys.stderr)
        return 2
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    artifacts.mkdir(mode=0o700, exist_ok=True)
    workload_name = f"load-profile-{project_name}.json"
    observations_name = f"load-observations-{project_name}.jsonl"
    run_path = artifacts / f"load-run-{project_name}.json"
    if any(
        path.exists() or path.is_symlink()
        for path in (run_path, artifacts / workload_name, artifacts / observations_name)
    ):
        print(
            "A load report already exists for this project. Use a new project name.",
            file=sys.stderr,
        )
        return 2
    timings = PhaseTimings()
    report: dict[str, object] = {
        "status": "failed",
        "candidate_image": arguments.candidate_image,
        "project": project_name,
        "release_evidence_eligible": False,
        "diagnostic_lock_seconds": arguments.diagnostic_lock_seconds,
        "disposable_volumes_removed": False,
        "workload_report": workload_name,
        "observations_file": observations_name,
        "source_commit": "unknown",
        "platform": "unknown",
        "phases": timings.records,
    }
    observations: LoadObservations | None = None
    stack: IsolatedComposeProject | None = None
    completed = False
    try:
        with termination_signals():
            contexts = ExitStack()
            try:
                with timings.phase("startup_isolation"):
                    stack = contexts.enter_context(
                        IsolatedComposeProject(
                            project_name,
                            profiles=("load",),
                            **(
                                {"candidate_image": arguments.candidate_image}
                                if arguments.candidate_image
                                else {}
                            ),
                        )
                    )
                stack.environment.update(
                    {
                        "STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE": "18",
                        "STARTUNNEL_WEB_WORKERS": "4",
                        "STARTUNNEL_PROOF_METRICS": "1",
                        "STARTUNNEL_PROOF_METRICS_DIRECTORY": "/tmp/startunnel-proof-metrics",
                        "STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT": "500",
                        "STARTUNNEL_LOAD_DURATION_SECONDS": "300",
                        "STARTUNNEL_LOAD_SENDS_PER_SECOND": "50",
                        "STARTUNNEL_LOAD_ACTIVITY_READERS": "100",
                        "STARTUNNEL_LOAD_DRAIN_SECONDS": "30",
                        "STARTUNNEL_LOAD_CREDENTIALS": "500",
                        "STARTUNNEL_LOAD_REPORT_FILENAME": workload_name,
                        "STARTUNNEL_LOAD_DIAGNOSTIC_LOCK_SECONDS": str(
                            arguments.diagnostic_lock_seconds
                        ),
                    }
                )
                docker_version = required_output(
                    ["docker", "version", "--format", "{{.Server.Version}}"],
                    environment=stack.environment,
                    label="the Docker server version",
                )
                if not re.fullmatch(r"[A-Za-z0-9.+_-]+", docker_version):
                    raise LaneError("Docker version inspection", "Invalid Docker server version.")
                commit = required_output(
                    ["git", "rev-parse", "HEAD"],
                    environment=stack.environment,
                    label="the source commit",
                )
                if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                    raise LaneError("source inspection", "Invalid source commit.")
                report["source_commit"] = commit
                with timings.phase("build"):
                    if arguments.candidate_image:
                        stack.pull_candidate()
                        stack.build_services(("load-profile",), timeout_seconds=2400)
                    elif arguments.reuse_current_images:
                        _assert_reusable_images(stack)
                    else:
                        stack.build_services(("web",), timeout_seconds=2400)
                        stack.build_services(("load-profile",), timeout_seconds=2400)
                with timings.phase("startup"):
                    stack.run(
                        "Start isolated load stack",
                        "up",
                        "--detach",
                        "--wait",
                        "--wait-timeout",
                        "300",
                        "web",
                        "maintenance",
                        timeout_seconds=600,
                    )
                    stack.run("Create load service", "create", "load-profile", timeout_seconds=120)
                if arguments.candidate_image:
                    with timings.phase("pre_fixture_candidate_verification"):
                        stack.verify_candidate_services()
                image_ids = ",".join(
                    f"{service}={_image_id(stack, service)}" for service in SERVICES
                )
                platform_value = required_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Os}}/{{.Architecture}}",
                        arguments.candidate_image or image_ids.split(",")[1].split("=", 1)[1],
                    ],
                    environment=stack.environment,
                    label="the runtime platform",
                )
                if not re.fullmatch(r"[a-z0-9_]+/[a-z0-9_]+", platform_value):
                    raise LaneError("platform inspection", "Invalid runtime platform.")
                report["platform"] = platform_value
                with timings.phase("load_helper_verification"):
                    report["helper"] = _verify_helper(stack, commit, platform_value)
                build_mode = (
                    "immutable-candidate"
                    if arguments.candidate_image
                    else "reused-current-images"
                    if arguments.reuse_current_images
                    else "built-current-run"
                )
                report["provenance"] = {
                    "build_mode": build_mode,
                    "image_ids": image_ids,
                    "docker_version": docker_version,
                }
                load_environment = dict(stack.environment)
                load_environment.update(
                    {
                        "STARTUNNEL_LOAD_DOCKER_VERSION": docker_version,
                        "STARTUNNEL_LOAD_IMAGE_DIGESTS": image_ids,
                        "STARTUNNEL_LOAD_BUILD_MODE": build_mode,
                    }
                )
                observations = LoadObservations(stack, artifacts / observations_name)
                with observations, timings.phase("workload"):
                    stack.run(
                        "Run fixed five-minute load profile",
                        "run",
                        "--rm",
                        "--no-deps",
                        "load-profile",
                        timeout_seconds=2400,
                        environment=load_environment,
                    )
                if observations.failed or not observations.summary()["complete"]:
                    raise LaneError(
                        "load observations", "The observation collector did not complete."
                    )
                if arguments.candidate_image:
                    with timings.phase("final_candidate_verification"):
                        stack.verify_candidate_services()
                completed = True
            finally:
                with timings.phase("cleanup"):
                    contexts.close()
                report["disposable_volumes_removed"] = bool(
                    stack is not None and stack.cleanup_complete
                )
    except (
        InterruptedError,
        KeyboardInterrupt,
        IsolationError,
        LaneError,
        OSError,
        ValueError,
    ) as error:
        completed = False
        report["failure"] = error.step if isinstance(error, LaneError) else type(error).__name__
        print("The isolated load proof failed. See the safe run report.", file=sys.stderr)
    finally:
        if observations is not None:
            report["observations"] = observations.summary()
        try:
            workload = json.loads((artifacts / workload_name).read_text(encoding="utf-8"))
            report["configuration"] = workload.get("configuration", {})
            driver_name = workload.get("driver_observations_file")
            if (
                not isinstance(driver_name, str)
                or Path(driver_name).name != driver_name
                or not driver_name.endswith(".driver.jsonl")
            ):
                raise ValueError("The driver observations name is invalid.")
            driver_path = artifacts / driver_name
            if driver_path.is_symlink() or not driver_path.is_file():
                raise ValueError("The driver observations file is missing.")
            report["driver_observations_file"] = driver_name
            completed = (
                completed
                and workload.get("passed") is True
                and workload.get("fixture_cleanup_complete") is True
            )
        except OSError, ValueError, AttributeError:
            completed = False
        passed = completed and report["disposable_volumes_removed"] is True
        report["status"] = "passed" if passed else "failed"
        report["release_evidence_eligible"] = (
            passed
            and arguments.candidate_image is not None
            and arguments.diagnostic_lock_seconds == 0
        )
        # Write the completed result only after stack cleanup. Failed and interrupted runs remain.
        with run_path.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, sort_keys=True)
            output.write("\n")
    if report["status"] != "passed":
        return 1
    print(f"The isolated load proof passed. Safe report: {run_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
