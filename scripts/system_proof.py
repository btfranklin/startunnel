"""Run the clean-stack system scenario and collect safe release evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        ROOT,
        IsolatedComposeProject,
        LaneError,
        required_output,
    )
else:
    from compose_isolation import (
        ROOT,
        IsolatedComposeProject,
        LaneError,
        required_output,
    )

SENSITIVE_LOG_PATTERNS = (
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("StarTunnel key", re.compile(r"\bst_[A-Za-z0-9_-]{43}\b")),
    ("glyph address", re.compile(r"[\U0001f700-\U0001f73f]")),
    ("receipt field", re.compile(r"\breceipt_handle\b")),
    ("snapshot cursor field", re.compile(r"\bsnapshot_cursor\b")),
    ("page cursor field", re.compile(r"\bpage_cursor\b")),
    ("activity cursor field", re.compile(r"\bactivity_cursor\b")),
    ("text payload field", re.compile(r"\btext_payload\b")),
    ("JSON payload field", re.compile(r"\bjson_payload\b")),
)
ACTIVITY_WAITING_MARKER = "STARTUNNEL_ACTIVITY_READING"
ACTIVITY_INTERRUPTED_MARKER = "STARTUNNEL_ACTIVITY_INTERRUPTED_SAFE"
ACTIVITY_COMPLETE_MARKER = "STARTUNNEL_ACTIVITY_RECOVERY_COMPLETE"
ACTIVITY_FAILED_MARKER = "STARTUNNEL_ACTIVITY_PROOF_FAILED"
ACTIVITY_FAILURE_STAGE_PREFIX = "STARTUNNEL_ACTIVITY_FAILURE_STAGE_"
ACTIVITY_FAILURE_STAGES = frozenset({"CONFIGURATION", "PROVISIONING", "EXCHANGE", "REVOCATION"})
SYSTEM_SERVICES = (
    "postgres",
    "web",
    "maintenance",
    "example-tests",
    "agent-e2e",
    "browser-tests",
)


def _web_health_script(expected_status: int, *, path: str = "/health/ready") -> str:
    if path not in {"/health/live", "/health/ready"}:
        raise ValueError("The health proof path is not allowed.")
    return (
        "import urllib.error, urllib.request; "
        f"request=urllib.request.Request('http://127.0.0.1:8000{path}', "
        "headers={'Host':'web','X-Forwarded-Proto':'https'}); "
        "status=None; "
        "\ntry:\n response=urllib.request.urlopen(request,timeout=10); status=response.status"
        "\nexcept urllib.error.HTTPError as error:\n status=error.code"
        f"\nassert status == {expected_status}, status"
    )


def _runtime_select_script() -> str:
    return (
        'PGPASSWORD="$STARTUNNEL_DB_RUNTIME_PASSWORD" exec psql '
        "--set=ON_ERROR_STOP=1 --tuples-only --no-align "
        '--username startunnel --dbname startunnel --command "'
        "SELECT count(*) FROM django_migrations; "
        "BEGIN; "
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('system_proof', 'runtime_dml', now()); "
        "DELETE FROM django_migrations "
        "WHERE app = 'system_proof' AND name = 'runtime_dml'; "
        'ROLLBACK;"'
    )


def _coordinated_restore_proof(stack: IsolatedComposeProject) -> None:
    """Back up and restore the PostgreSQL database."""

    with tempfile.TemporaryDirectory(prefix="startunnel-system-backup-") as directory:
        private_directory = Path(directory)
        private_directory.chmod(0o700)
        backup = private_directory / "system-proof.backup"
        environment = dict(stack.environment)
        environment.update(
            {
                "COMPOSE_FILE": (
                    ":".join(stack.compose_files())
                    if getattr(stack, "candidate_image", None)
                    else "compose.yaml:compose.test.yaml"
                ),
                "COMPOSE_PROJECT_NAME": stack.project_name,
                "COMPOSE_DISABLE_ENV_FILE": "true",
            }
        )
        for label, command in (
            ("coordinated backup", ["scripts/backup-startunnel.sh", str(backup)]),
            (
                "coordinated restore verification",
                ["scripts/verify-startunnel-restore.sh", str(backup)],
            ),
        ):
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    timeout=900,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise LaneError(label, f"The {label} did not finish.") from error
            if result.returncode:
                raise LaneError(label, f"The {label} failed with exit code {result.returncode}.")


def _runtime_restriction_script() -> str:
    return """
deny_operation() {
  label="$1"
  query="$2"
  if PGPASSWORD="$STARTUNNEL_DB_RUNTIME_PASSWORD" psql \
    --set=ON_ERROR_STOP=1 --username startunnel --dbname startunnel \
    --command "$query" >/dev/null 2>&1; then
    echo "The runtime role accepted forbidden ${label}." >&2
    exit 1
  fi
}
deny_operation "schema changes" \
  "CREATE TABLE startunnel_forbidden_system_proof (id integer);"
deny_operation "temporary tables" \
  "CREATE TEMP TABLE startunnel_forbidden_system_temp (id integer);"
deny_operation "database creation" \
  "CREATE DATABASE startunnel_forbidden_system_database;"
deny_operation "immutable history deletion" "DELETE FROM tunnels_message;"
deny_operation "audit history deletion" "DELETE FROM tunnels_auditevent;"
""".strip()


def _runtime_session_dml_script() -> str:
    return """
PGPASSWORD="$STARTUNNEL_DB_RUNTIME_PASSWORD" psql \
  --set=ON_ERROR_STOP=1 --username startunnel --dbname startunnel <<'SQL'
BEGIN;
INSERT INTO django_session (session_key, session_data, expire_date)
VALUES ('system-proof-session', 'system-proof-data', now() + interval '5 minutes');
UPDATE django_session
SET session_data = 'system-proof-updated'
WHERE session_key = 'system-proof-session';
DELETE FROM django_session WHERE session_key = 'system-proof-session';
ROLLBACK;
SQL
""".strip()


def _recorded_step(
    evidence: list[dict[str, int | str]],
    name: str,
    action: Callable[[], None],
) -> None:
    started = time.monotonic()
    action()
    evidence.append(
        {
            "name": name,
            "duration_seconds": round(time.monotonic() - started),
        }
    )


def _system_image_ids(stack: IsolatedComposeProject) -> dict[str, str]:
    result: dict[str, str] = {}
    for service in SYSTEM_SERVICES:
        identifiers = stack.capture("system image IDs", "images", "--quiet", service).splitlines()
        if not identifiers:
            raise LaneError("system image IDs", f"Docker reported no image for {service}.")
        identifier = required_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", identifiers[0]],
            environment=stack.environment,
            label=f"the {service} image ID",
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise LaneError(
                "system image IDs", f"Docker returned an invalid image ID for {service}."
            )
        result[service] = identifier
    return result


def _assert_reusable_images(stack: IsolatedComposeProject) -> None:
    """Require every system-proof service image before a build-free diagnostic run."""

    try:
        model = json.loads(
            stack.capture(
                "reusable image model",
                "config",
                "--format",
                "json",
                timeout_seconds=60,
            )
        )
    except json.JSONDecodeError as error:
        raise LaneError(
            "reusable image inspection",
            "Docker returned an invalid Compose model.",
        ) from error
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict):
        raise LaneError(
            "reusable image inspection",
            "The Compose model does not contain services.",
        )
    for service in SYSTEM_SERVICES:
        definition = services.get(service)
        image = definition.get("image") if isinstance(definition, dict) else None
        if not isinstance(image, str) or not image:
            raise LaneError(
                "reusable image inspection",
                f"The Compose model does not name an image for {service}.",
            )
        identifier = required_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            environment=stack.environment,
            label=f"the reusable {service} image",
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier):
            raise LaneError(
                "reusable image inspection",
                f"Docker returned an invalid reusable image ID for {service}.",
            )


def _system_host_profile(stack: IsolatedComposeProject) -> dict[str, int | str]:
    fields = {
        "architecture": "{{.Architecture}}",
        "memory_bytes": "{{.MemTotal}}",
        "operating_system": "{{.OperatingSystem}}",
        "processor_count": "{{.NCPU}}",
    }
    values = {
        name: required_output(
            ["docker", "info", "--format", template],
            environment=stack.environment,
            label=f"Docker host {name}",
        )
        for name, template in fields.items()
    }
    if not values["memory_bytes"].isdigit() or not values["processor_count"].isdigit():
        raise LaneError("system host profile", "Docker returned an invalid capacity value.")
    for name in ("architecture", "operating_system"):
        if not re.fullmatch(r"[A-Za-z0-9 ._()/+-]{1,128}", values[name]):
            raise LaneError("system host profile", f"Docker returned an invalid {name} value.")
    return {
        "architecture": values["architecture"],
        "memory_bytes": int(values["memory_bytes"]),
        "operating_system": values["operating_system"],
        "processor_count": int(values["processor_count"]),
    }


def _assert_safe_runtime_logs(logs: str) -> None:
    if not logs.strip():
        raise LaneError("runtime log inspection", "The application produced no logs to inspect.")
    for label, pattern in SENSITIVE_LOG_PATTERNS:
        if pattern.search(logs):
            raise LaneError(
                "runtime log inspection",
                f"The application logs contain a forbidden {label} value.",
            )


def _proof_client_logs(stack: IsolatedComposeProject, container_name: str) -> str:
    logs = required_output(
        ["docker", "logs", container_name],
        environment=stack.environment,
        label="the activity proof client logs",
    )
    for label, pattern in SENSITIVE_LOG_PATTERNS:
        if pattern.search(logs):
            raise LaneError(
                "activity restart proof",
                f"The proof client output contains a forbidden {label} value.",
            )
    lines = logs.splitlines()
    if ACTIVITY_FAILED_MARKER in lines:
        stage = next(
            (
                line.removeprefix(ACTIVITY_FAILURE_STAGE_PREFIX)
                for line in lines
                if line.startswith(ACTIVITY_FAILURE_STAGE_PREFIX)
                and line.removeprefix(ACTIVITY_FAILURE_STAGE_PREFIX) in ACTIVITY_FAILURE_STAGES
            ),
            None,
        )
        stage_message = f" during {stage.casefold()}" if stage else ""
        raise LaneError(
            "activity restart proof",
            f"The proof client reported failure{stage_message}.",
        )
    return logs


def _proof_client_state(
    stack: IsolatedComposeProject,
    container_name: str,
) -> tuple[bool, int]:
    value = required_output(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}} {{.State.ExitCode}}",
            container_name,
        ],
        environment=stack.environment,
        label="the activity proof client state",
    )
    match = re.fullmatch(r"(true|false) ([0-9]+)", value)
    if match is None:
        raise LaneError("activity restart proof", "Docker returned an invalid client state.")
    return match.group(1) == "true", int(match.group(2))


def _wait_for_proof_marker(
    stack: IsolatedComposeProject,
    container_name: str,
    marker: str,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        logs = _proof_client_logs(stack, container_name)
        if marker in logs.splitlines():
            return
        running, exit_code = _proof_client_state(stack, container_name)
        if not running:
            _proof_client_logs(stack, container_name)
            raise LaneError(
                "activity restart proof",
                f"The proof client exited early with code {exit_code}.",
            )
        time.sleep(0.25)
    raise LaneError("activity restart proof", "The proof client marker timed out.")


def _wait_for_proof_exit(
    stack: IsolatedComposeProject,
    container_name: str,
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        running, exit_code = _proof_client_state(stack, container_name)
        if not running:
            return exit_code
        time.sleep(0.25)
    raise LaneError("activity restart proof", "The proof client did not exit in time.")


def _assert_proof_marker_order(logs: str) -> None:
    lines = logs.splitlines()
    markers = (
        ACTIVITY_WAITING_MARKER,
        ACTIVITY_INTERRUPTED_MARKER,
        ACTIVITY_COMPLETE_MARKER,
    )
    if any(lines.count(marker) != 1 for marker in markers):
        raise LaneError("activity restart proof", "The proof markers are incomplete.")
    if [lines.index(marker) for marker in markers] != sorted(
        lines.index(marker) for marker in markers
    ):
        raise LaneError("activity restart proof", "The proof markers are out of order.")


def _run_activity_restart_proof(stack: IsolatedComposeProject) -> None:
    container_name = f"{stack.project_name}-activity-proof"
    stack.capture(
        "Start activity proof client",
        "run",
        "--detach",
        "--no-deps",
        "--name",
        container_name,
        "example-tests",
        "pdm",
        "run",
        "python",
        "-m",
        "scripts.prove_activity_restart",
        timeout_seconds=120,
    )
    _wait_for_proof_marker(
        stack,
        container_name,
        ACTIVITY_WAITING_MARKER,
        timeout_seconds=30,
    )
    time.sleep(2)
    logs = _proof_client_logs(stack, container_name)
    running, exit_code = _proof_client_state(stack, container_name)
    if not running or ACTIVITY_INTERRUPTED_MARKER in logs.splitlines():
        raise LaneError(
            "activity restart proof",
            f"The tree reader was not active before web stopped; client code {exit_code}.",
        )
    stack.run(
        "Stop web during active tree reads",
        "stop",
        "--timeout",
        "2",
        "web",
        timeout_seconds=30,
    )
    _wait_for_proof_marker(
        stack,
        container_name,
        ACTIVITY_INTERRUPTED_MARKER,
        timeout_seconds=30,
    )
    stack.run(
        "Restart web after tree-read interruption",
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "300",
        "web",
        timeout_seconds=600,
    )
    _wait_for_proof_marker(
        stack,
        container_name,
        ACTIVITY_COMPLETE_MARKER,
        timeout_seconds=180,
    )
    exit_code = _wait_for_proof_exit(stack, container_name, timeout_seconds=60)
    if exit_code:
        raise LaneError(
            "activity restart proof",
            f"The recovery proof client exited with code {exit_code}.",
        )
    _assert_proof_marker_order(_proof_client_logs(stack, container_name))


def write_system_report(report: Mapping[str, Any]) -> Path:
    directory = ROOT / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"system-proof-{timestamp}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return destination


def run_system_proof(
    stack: IsolatedComposeProject,
    *,
    build_images: bool = True,
) -> dict[str, Any]:
    """Run the current bounded clean-stack release proof."""

    evidence: list[dict[str, int | str]] = []
    started_at = datetime.now(UTC)

    def step(name: str, action: Callable[[], None]) -> None:
        _recorded_step(evidence, name, action)

    def prove_healthy_stack() -> None:
        for label, path in (
            ("Verify live health", "/health/live"),
            ("Verify ready health", "/health/ready"),
        ):
            stack.run(
                label,
                "exec",
                "-T",
                "web",
                "/app/.venv/bin/python",
                "-c",
                _web_health_script(200, path=path),
                timeout_seconds=60,
            )

    def prove_postgres_loss() -> None:
        stack.run(
            "Stop PostgreSQL",
            "stop",
            "--timeout",
            "30",
            "postgres",
            timeout_seconds=90,
        )
        stack.run(
            "Verify readiness during PostgreSQL loss",
            "exec",
            "-T",
            "web",
            "/app/.venv/bin/python",
            "-c",
            _web_health_script(503),
            timeout_seconds=60,
        )

    def prove_database_restore() -> None:
        _coordinated_restore_proof(stack)
        # The backup helper restarts writers asynchronously. Establish a healthy
        # application before testing PostgreSQL loss, rather than racing startup.
        stack.run(
            "Wait for services after backup and restore",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "web",
            "maintenance",
            timeout_seconds=600,
        )
        prove_healthy_stack()

    def prove_postgres_recovery() -> None:
        stack.run(
            "Recover PostgreSQL",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "postgres",
            timeout_seconds=600,
        )
        stack.run(
            "Recover application services",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "maintenance",
            "web",
            timeout_seconds=600,
        )

    def prove_application_restart() -> None:
        stack.run(
            "Restart application services",
            "restart",
            "--timeout",
            "30",
            "web",
            "maintenance",
            timeout_seconds=180,
        )
        stack.run(
            "Wait for restarted services",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "web",
            "maintenance",
            timeout_seconds=600,
        )

    candidate_image = getattr(stack, "candidate_image", None)
    if candidate_image:
        step("pull immutable candidate", stack.pull_candidate)
        step(
            "proof helper image build",
            lambda: stack.run(
                "Build proof helper images",
                "build",
                "--no-cache",
                "example-tests",
                "agent-e2e",
                "browser-tests",
                timeout_seconds=3_600,
            ),
        )
    elif build_images:
        step(
            "no-cache image build",
            lambda: stack.run(
                "No-cache system image build",
                "build",
                "--no-cache",
                "web",
                "maintenance",
                "example-tests",
                "agent-e2e",
                "browser-tests",
                timeout_seconds=3_600,
            ),
        )
    else:
        step(
            "verified current images",
            lambda: _assert_reusable_images(stack),
        )
    step(
        "healthy clean-stack startup",
        lambda: stack.run(
            "Start clean system stack",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "web",
            timeout_seconds=600,
        ),
    )
    step(
        "proof-service creation",
        lambda: stack.run(
            "Create proof service containers",
            "create",
            "example-tests",
            "agent-e2e",
            "browser-tests",
            timeout_seconds=300,
        ),
    )
    step("live and ready health", prove_healthy_stack)
    step(
        "clean-stack examples",
        lambda: stack.run(
            "Run clean-stack examples",
            "run",
            "--rm",
            "--no-deps",
            "example-tests",
            timeout_seconds=2_400,
        ),
    )
    step(
        "deterministic external agents",
        lambda: stack.run(
            "Run deterministic external agents",
            "run",
            "--rm",
            "--no-deps",
            "agent-e2e",
            timeout_seconds=2_400,
        ),
    )
    step("active tree-reader web restart", lambda: _run_activity_restart_proof(stack))
    step(
        "three-browser application flow",
        lambda: stack.run(
            "Run three-browser application flow",
            "run",
            "--rm",
            "--no-deps",
            "browser-tests",
            timeout_seconds=4_800,
        ),
    )
    step(
        "runtime database connection",
        lambda: stack.run(
            "Verify runtime database connection",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            _runtime_select_script(),
            timeout_seconds=60,
        ),
    )
    step(
        "runtime DDL denial",
        lambda: stack.run(
            "Verify runtime DDL denial",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            _runtime_restriction_script(),
            timeout_seconds=60,
        ),
    )
    step(
        "runtime session DML",
        lambda: stack.run(
            "Verify runtime session DML",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            _runtime_session_dml_script(),
            timeout_seconds=60,
        ),
    )
    step("PostgreSQL backup and restore", prove_database_restore)
    step("PostgreSQL loss", prove_postgres_loss)
    step("PostgreSQL recovery", prove_postgres_recovery)
    step("application restart", prove_application_restart)
    step(
        "post-restart deterministic exchange",
        lambda: stack.run(
            "Repeat deterministic external exchange",
            "run",
            "--rm",
            "--no-deps",
            "agent-e2e",
            timeout_seconds=2_400,
        ),
    )
    step(
        "sensitive runtime log inspection",
        lambda: _assert_safe_runtime_logs(
            stack.capture(
                "application runtime logs",
                "logs",
                "--no-color",
                "--no-log-prefix",
                "web",
                "maintenance",
                "migrate",
                timeout_seconds=60,
            )
        ),
    )

    if candidate_image:
        step("candidate image identity", stack.verify_candidate_services)
    docker_version = required_output(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        environment=stack.environment,
        label="the Docker server version",
    )
    report = {
        "proof": "clean-stack system",
        "status": "passed",
        "build_mode": (
            "immutable-candidate"
            if candidate_image
            else "no-cache"
            if build_images
            else "reused-current-images"
        ),
        "candidate_image": candidate_image,
        "release_evidence_eligible": build_images,
        "project": stack.project_name,
        "started_at": started_at.isoformat(),
        "docker_server_version": docker_version,
        "host": _system_host_profile(stack),
        "image_ids": _system_image_ids(stack),
        "steps": evidence,
        "retained_data": "safe evidence only",
    }
    return report
