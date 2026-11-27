"""Prove the operator restore path on a fresh isolated production stack."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import ROOT, IsolationError, LaneError, generated_project_name
    from scripts.prove_production_release import (
        APP_SERVICES,
        ProductionComposeProject,
        available_proxy_prefix,
        command_json,
        object_value,
        prepare_stack,
        set_production_environment,
        string_value,
    )
else:
    from compose_isolation import ROOT, IsolationError, LaneError, generated_project_name
    from prove_production_release import (
        APP_SERVICES,
        ProductionComposeProject,
        available_proxy_prefix,
        command_json,
        object_value,
        prepare_stack,
        set_production_environment,
        string_value,
    )


def run_backup_command(
    stack: ProductionComposeProject,
    script: str,
    backup: Path,
    *,
    expected_refusal: str = "",
) -> None:
    environment = {
        **stack.environment,
        "COMPOSE_FILE": ":".join(stack.compose_files()),
        "COMPOSE_PROJECT_NAME": stack.project_name,
        "COMPOSE_DISABLE_ENV_FILE": "true",
    }
    try:
        result = subprocess.run(
            [str(ROOT / "scripts" / script), str(backup)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaneError("replacement_restore", f"{script} did not finish.") from error
    if expected_refusal:
        if not result.returncode or expected_refusal not in result.stderr:
            raise LaneError(
                "replacement_restore", f"{script} did not refuse the selected database."
            )
    elif result.returncode:
        raise LaneError(
            "replacement_restore", f"{script} failed with exit code {result.returncode}."
        )


def prove_replacement_restore(
    source: ProductionComposeProject,
    private_directory: Path,
    source_url: str,
    image: str,
) -> None:
    """Recover a known message and use its existing agent key for a new write."""

    client = private_directory / "star_tunnel.py"
    credentials = {
        "STARTUNNEL_BASE_URL": source_url,
        "STARTUNNEL_AGENT_KEY_FILE": str(private_directory / "agent-one.key"),
    }
    create_arguments = ["create", "--idempotency-key"]
    message = {
        "label": "Replacement restore proof",
        "cycle": {"root": {"content": {"type": "text", "text": "synthetic recovery message"}}},
    }
    initial = command_json(client, [*create_arguments, uuid4().hex], credentials, message)
    tunnel = object_value(initial.get("tunnel"), "replacement_restore")
    cycle = object_value(initial.get("cycle"), "replacement_restore")
    root = object_value(cycle.get("root"), "replacement_restore")
    address = string_value(tunnel, "address", "replacement_restore")
    root_id = string_value(root, "id", "replacement_restore")

    backup = private_directory / "replacement.backup"
    run_backup_command(source, "backup-startunnel.sh", backup)
    replacement_directory = private_directory / "replacement"
    replacement_directory.mkdir(mode=0o700)
    # Attach the same private fixtures to recreate the state supplied by the archive.
    replacement, replacement_url, secret_directory, _ = prepare_stack(
        replacement_directory,
        image=image,
        project_name=generated_project_name("replacement-restore"),
        secret_directory=Path(source.environment["STARTUNNEL_SECRETS_DIR"]),
    )
    with replacement:
        set_production_environment(
            replacement.environment,
            image=image,
            base_url=replacement_url,
            secret_directory=secret_directory,
            proxy_prefix=available_proxy_prefix(replacement.environment),
        )
        run_backup_command(replacement, "restore-startunnel-replacement.sh", backup)
        replacement.run(
            "start recovered services",
            "up",
            "--detach",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "180",
            "postgres",
            *APP_SERVICES,
            timeout_seconds=240,
        )
        credentials["STARTUNNEL_BASE_URL"] = replacement_url
        restored = command_json(client, ["read-tree"], credentials, {"address": address})
        if restored.get("root_id") != root_id:
            raise LaneError("replacement_restore", "The restored message identity differs.")
        command_json(client, [*create_arguments, uuid4().hex], credentials, message)
        replacement.run(
            "stop recovered writers",
            "stop",
            "web",
            "maintenance",
            timeout_seconds=60,
        )
        run_backup_command(
            replacement,
            "restore-startunnel-replacement.sh",
            backup,
            expected_refusal="differs from a fresh bootstrap database",
        )
    if not replacement.cleanup_complete:
        raise IsolationError("The replacement proof did not remove its Compose resources.")
