"""Run the fixed load proof in a new, disposable Compose project."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        required_output,
        termination_signals,
    )
else:
    from compose_isolation import (
        IsolatedComposeProject,
        IsolationError,
        LaneError,
        generated_project_name,
        required_output,
        termination_signals,
    )

SERVICES = ("postgres", "web", "maintenance", "load-profile")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-name",
        help="Use an unused disposable project name with an approved test prefix.",
    )
    parser.add_argument("--seed", type=int, default=20260810)
    parser.add_argument(
        "--reuse-current-images",
        action="store_true",
        help=(
            "Skip the image build after verifying every required image. "
            "The safe report records this build mode."
        ),
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


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _parser().parse_args(argv)
    project_name = arguments.project_name or generated_project_name("load")
    try:
        with (
            IsolatedComposeProject(project_name, profiles=("load",)) as stack,
            termination_signals(),
        ):
            # Record the fixed capacity explicitly so the report proves the
            # profile against a reproducible worker and database budget.
            stack.environment.update(
                {
                    "STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE": "6",
                    "STARTUNNEL_WEB_WORKERS": "12",
                }
            )
            docker_version = required_output(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                environment=stack.environment,
                label="the Docker server version",
            )
            if not re.fullmatch(r"[A-Za-z0-9.+_-]+", docker_version):
                raise LaneError(
                    "Docker version inspection",
                    "Docker returned an invalid server version value.",
                )
            if arguments.reuse_current_images:
                _assert_reusable_images(stack)
            else:
                stack.run(
                    "Build load images",
                    "build",
                    "web",
                    "load-profile",
                    timeout_seconds=2_400,
                )
            stack.run(
                "Start isolated load stack",
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "300",
                "web",
                timeout_seconds=600,
            )
            stack.run(
                "Create load service",
                "create",
                "load-profile",
                timeout_seconds=120,
            )
            image_ids = ",".join(f"{service}={_image_id(stack, service)}" for service in SERVICES)
            load_environment = dict(stack.environment)
            load_environment.update(
                {
                    "STARTUNNEL_LOAD_DOCKER_VERSION": docker_version,
                    "STARTUNNEL_LOAD_IMAGE_DIGESTS": image_ids,
                    "STARTUNNEL_LOAD_BUILD_MODE": (
                        "reused-current-images"
                        if arguments.reuse_current_images
                        else "built-current-run"
                    ),
                    "STARTUNNEL_LOAD_SEED": str(arguments.seed),
                }
            )
            stack.run(
                "Run fixed five-minute load profile",
                "run",
                "--rm",
                "--no-deps",
                "load-profile",
                timeout_seconds=2_400,
                environment=load_environment,
            )
    except (InterruptedError, IsolationError, LaneError) as error:
        print(f"Isolated load proof failed: {error}", file=sys.stderr)
        return 1
    print("The isolated load proof passed. The safe report is under artifacts/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
