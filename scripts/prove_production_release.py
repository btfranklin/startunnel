"""Prove one digest-pinned StarTunnel image with production Compose settings."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import socket
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast
from urllib.parse import urlsplit
from uuid import uuid4

if TYPE_CHECKING or __package__:
    from .compose_isolation import (
        IsolatedComposeProject,
        LaneError,
        available_proxy_prefix,
        generated_project_name,
        required_output,
        safe_environment,
        termination_signals,
        validate_project_name,
    )
    from .generate_production_secrets import random_urlsafe, write_secret
    from .json_data import JsonObject, json_integer, json_object
    from .proof_timing import PhaseRecord, PhaseTimings
    from .release_record import candidate_version
    from .start_production import validate_image_reference
else:
    from compose_isolation import (
        IsolatedComposeProject,
        LaneError,
        available_proxy_prefix,
        generated_project_name,
        required_output,
        safe_environment,
        termination_signals,
        validate_project_name,
    )
    from generate_production_secrets import random_urlsafe, write_secret
    from json_data import JsonObject, json_integer, json_object
    from proof_timing import PhaseRecord, PhaseTimings
    from release_record import candidate_version
    from start_production import validate_image_reference

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
APP_SERVICES = ("web", "migrate", "maintenance")
SECRET_NAMES = (
    "startunnel_secret_key",
    "startunnel_api_key_pepper",
    "startunnel_idempotency_secret",
    "startunnel_metrics_token",
    "postgres_admin_password",
    "postgres_runtime_password",
)
CHECK_NAMES = (
    "source_identity",
    "runtime_images",
    "production_settings",
    "downloaded_cli",
    "admin_authentication",
    "admin_receipt_audit",
    "agent_authentication",
    "two_agent_exchange",
    "health_live",
    "health_ready",
    "metrics_files_preserved",
)
PHASE_NAMES = (
    "prepare",
    "render_compose",
    "pull_candidate",
    "start_services",
    "runtime_identity",
    "health",
    "bootstrap_admin",
    "download_cli",
    "cleanup",
)
FAILURE_STEPS = frozenset((*PHASE_NAMES, *CHECK_NAMES, "downloaded_cli", "render_compose", "proof"))


class ReleaseReport(TypedDict):
    status: str
    source_commit: str
    version: str
    candidate_image: str
    platform: str
    image_ids: dict[str, str]
    checks: dict[str, bool]
    disposable_volumes_removed: bool
    private_files_removed: bool
    phases: list[PhaseRecord]
    failure_type: str | None
    failed_step: str | None
    release_evidence_eligible: bool


class ProductionComposeProject(IsolatedComposeProject):
    """Use only production Compose files and the private smoke override."""

    def __init__(
        self,
        project_name: str,
        *,
        candidate_image: str,
        source_environment: Mapping[str, str],
        smoke_overlay: Path,
    ) -> None:
        super().__init__(
            project_name,
            candidate_image=candidate_image,
            source_environment=source_environment,
        )
        self.smoke_overlay = smoke_overlay

    def compose_files(self) -> tuple[str, ...]:
        return (
            str(ROOT / "compose.yaml"),
            str(ROOT / "compose.prod.yaml"),
            str(self.smoke_overlay),
        )


class CaptureStack(Protocol):
    def capture(
        self,
        step: str,
        *arguments: str,
        timeout_seconds: int = 60,
    ) -> str: ...


def new_report(commit: str, version: str, image: str) -> ReleaseReport:
    return {
        "status": "failed",
        "source_commit": commit,
        "version": version,
        "candidate_image": image,
        "platform": PLATFORM,
        "image_ids": {},
        "checks": {name: False for name in CHECK_NAMES},
        "disposable_volumes_removed": False,
        "private_files_removed": False,
        "phases": [],
        "failure_type": None,
        "failed_step": None,
        "release_evidence_eligible": False,
    }


def validate_report(report: Mapping[str, object], *, commit: str, image: str, version: str) -> None:
    """Reject any incomplete smoke report or identity mismatch."""

    if (
        report.get("status") != "passed"
        or report.get("source_commit") != commit
        or report.get("candidate_image") != image
        or report.get("version") != version
        or report.get("platform") != PLATFORM
    ):
        raise ValueError("Production release smoke identity or status is invalid.")
    checks = report.get("checks")
    if not isinstance(checks, Mapping) or any(checks.get(name) is not True for name in CHECK_NAMES):
        raise ValueError("Production release smoke has incomplete checks.")
    image_ids = report.get("image_ids")
    if not isinstance(image_ids, Mapping) or any(
        not isinstance(image_ids.get(service), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_ids[service]) is None
        for service in APP_SERVICES
    ):
        raise ValueError("Production release smoke has incomplete runtime image IDs.")
    if len({image_ids[service] for service in APP_SERVICES}) != 1:
        raise ValueError("Production release smoke services ran different image IDs.")
    if (
        report.get("disposable_volumes_removed") is not True
        or report.get("private_files_removed") is not True
        or report.get("release_evidence_eligible") is not True
    ):
        raise ValueError("Production release smoke cleanup or evidence eligibility is invalid.")


def validate_candidate_metadata(
    labels: Mapping[str, str], *, platform: str, commit: str, version: str
) -> None:
    if platform != PLATFORM:
        raise LaneError("source_identity", "Candidate image platform is not linux/amd64.")
    if labels.get("org.opencontainers.image.revision") != commit:
        raise LaneError("source_identity", "Candidate image source does not match this checkout.")
    if labels.get("org.opencontainers.image.version") != version:
        raise LaneError("source_identity", "Candidate image version does not match the release.")


def verify_runtime_image_ids(expected: str, actual: Mapping[str, str]) -> None:
    if (
        re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is None
        or set(actual) != set(APP_SERVICES)
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", actual[service]) is None
            for service in APP_SERVICES
        )
        or any(actual[service] != expected for service in APP_SERVICES)
    ):
        raise LaneError("runtime_images", "An application service did not use the candidate image.")


def set_production_environment(
    environment: dict[str, str],
    *,
    image: str,
    base_url: str,
    secret_directory: Path,
    proxy_prefix: str,
) -> None:
    environment.update(
        {
            "STARTUNNEL_APP_IMAGE": image,
            "STARTUNNEL_DOMAIN": "127.0.0.1",
            "STARTUNNEL_ALLOWED_HOSTS": "127.0.0.1,localhost,web",
            "STARTUNNEL_BASE_URL": base_url,
            "STARTUNNEL_SECRETS_DIR": str(secret_directory),
            "STARTUNNEL_PROXY_IPV4_PREFIX": proxy_prefix,
        }
    )


def write_private_file(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def make_secrets(directory: Path) -> None:
    directory.mkdir(mode=0o700)
    values = {
        "startunnel_secret_key": secrets.token_urlsafe(64),
        "startunnel_api_key_pepper": random_urlsafe(),
        "startunnel_idempotency_secret": random_urlsafe(),
        "startunnel_metrics_token": random_urlsafe(),
        "postgres_admin_password": secrets.token_urlsafe(32),
        "postgres_runtime_password": secrets.token_urlsafe(32),
    }
    for name, value in values.items():
        write_secret(directory / name, value)


def object_value(value: object, step: str) -> dict[str, object]:
    try:
        return cast(dict[str, object], json_object(value))
    except ValueError:
        raise LaneError(step, "The service returned an invalid response shape.") from None


def string_value(value: Mapping[str, object], field: str, step: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise LaneError(step, "The service response is missing a required field.")
    return result


def command_json(
    client: Path,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    body: object | None = None,
) -> dict[str, object]:
    payload = None if body is None else json.dumps(body, separators=(",", ":"))
    try:
        result = subprocess.run(
            [sys.executable, str(client), *arguments],
            cwd=ROOT,
            env=dict(environment),
            input=payload,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaneError("download_cli", "Downloaded CLI command did not finish.") from error
    if result.returncode or result.stderr:
        raise LaneError("download_cli", "Downloaded CLI command failed.")
    try:
        return object_value(json.loads(result.stdout), "download_cli")
    except json.JSONDecodeError as error:
        raise LaneError("download_cli", "Downloaded CLI returned invalid JSON.") from error


def verify_compose_model(
    model: Mapping[str, object], *, image: str, port: int, secret_directory: Path
) -> None:
    services = object_value(model.get("services"), "render_compose")
    configured_secrets = object_value(model.get("secrets"), "render_compose")
    for name in SECRET_NAMES:
        secret = object_value(configured_secrets.get(name), "render_compose")
        file_path = secret.get("file")
        if (
            not isinstance(file_path, str)
            or Path(file_path).resolve() != (secret_directory / name).resolve()
        ):
            raise LaneError("render_compose", "A secret file is outside private storage.")
    for service_name in APP_SERVICES:
        service = object_value(services.get(service_name), "render_compose")
        environment = object_value(service.get("environment"), "render_compose")
        if (
            service.get("image") != image
            or service.get("platform") != PLATFORM
            or environment.get("DJANGO_SETTINGS_MODULE") != "startunnel.settings.production"
            or environment.get("STARTUNNEL_ENV") != "production"
        ):
            raise LaneError("render_compose", "Production service configuration is invalid.")
    contracts = {
        "postgres": {"postgres_admin_password", "postgres_runtime_password"},
        "migrate": set(SECRET_NAMES) - {"postgres_runtime_password"},
        "web": set(SECRET_NAMES) - {"postgres_admin_password"},
        "maintenance": set(SECRET_NAMES) - {"postgres_runtime_password"},
    }
    for service_name, expected in contracts.items():
        service = object_value(services.get(service_name), "render_compose")
        value = service.get("secrets")
        if not isinstance(value, list):
            raise LaneError("render_compose", "A production service has no secret mounts.")
        mounted: set[str] = set()
        for item in value:
            source = (
                item
                if isinstance(item, str)
                else object_value(item, "render_compose").get("source")
            )
            if not isinstance(source, str):
                raise LaneError("render_compose", "A production secret mount has no source.")
            mounted.add(source)
        if mounted != expected:
            raise LaneError("render_compose", "A production service has invalid secret mounts.")
    web = object_value(services.get("web"), "render_compose")
    volumes = web.get("volumes")
    if not isinstance(volumes, list) or len(volumes) != 1:
        raise LaneError("render_compose", "Web must mount only the checked runtime probe.")
    probe = object_value(volumes[0], "render_compose")
    if any(
        probe.get(name) != expected
        for name, expected in {
            "type": "bind",
            "source": str(ROOT / "scripts/proof_runtime.py"),
            "target": "/proof/runtime.py",
            "read_only": True,
        }.items()
    ):
        raise LaneError("render_compose", "The runtime probe mount is invalid.")
    ports = web.get("ports")
    if not isinstance(ports, list) or len(ports) != 1:
        raise LaneError("render_compose", "Web must publish one loopback port.")
    published = object_value(ports[0], "render_compose")
    if (
        published.get("target") != 8000
        or published.get("host_ip") != "127.0.0.1"
        or str(published.get("published")) != str(port)
    ):
        raise LaneError("render_compose", "Web is not bound to the selected loopback port.")
    postgres = object_value(services.get("postgres"), "render_compose")
    if postgres.get("ports"):
        raise LaneError("render_compose", "PostgreSQL must not publish a host port.")


def metrics_preserved(before: Mapping[str, int], after: Mapping[str, int]) -> bool:
    return all(after.get(name) == inode for name, inode in before.items())


def admin_key_from_stack(stack: CaptureStack, private_directory: Path) -> Path:
    # The command creates this file exclusively in the disposable container's tmpfs.
    container_path = "/tmp/startunnel-release-admin.key"  # nosec B108
    stack.capture(
        "bootstrap_admin",
        "exec",
        "-T",
        "web",
        "env",
        "-u",
        "PROMETHEUS_MULTIPROC_DIR",
        "/usr/local/bin/startunnel-entrypoint",
        "/app/.venv/bin/python",
        "manage.py",
        "create_instance_admin",
        "release-proof-admin",
        "--key-file",
        container_path,
    )
    key = stack.capture("bootstrap_admin", "exec", "-T", "web", "cat", container_path)
    if not re.fullmatch(r"sta_[A-Za-z0-9_-]{43}", key):
        raise LaneError("bootstrap_admin", "The private admin key transfer was invalid.")
    path = private_directory / "admin.key"
    write_private_file(path, key + "\n")
    stack.capture("bootstrap_admin", "exec", "-T", "web", "rm", "-f", container_path)
    return path


def metrics_inodes(stack: IsolatedComposeProject) -> dict[str, int]:
    raw = stack.capture(
        "bootstrap_admin",
        "exec",
        "-T",
        "web",
        "/app/.venv/bin/python",
        "/proof/runtime.py",
        "metrics-inodes",
    )
    try:
        value: JsonObject = json_object(json.loads(raw))
        result = {name: json_integer(inode) for name, inode in value.items()}
    except (json.JSONDecodeError, ValueError) as error:
        raise LaneError("bootstrap_admin", "Metrics inode snapshot was invalid.") from error
    return result


def checked_proof_url(url: str) -> str:
    """Permit only the proof's HTTP loopback origin and fixed read endpoints."""
    parsed = urlsplit(url)
    port = parsed.port
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/health/live", "/health/ready", "/downloads/star_tunnel.py"}
    ):
        raise ValueError("The proof URL must use its HTTP loopback origin and a fixed endpoint.")
    return url


def health_ok(url: str) -> bool:
    request_url = checked_proof_url(url)
    try:
        # checked_proof_url rejects file, remote, and credential-bearing URLs.
        with urllib.request.urlopen(request_url, timeout=5) as response:  # nosec B310
            return cast(int, response.status) == 200
    except OSError, urllib.error.URLError:
        return False


def runtime_image_ids(stack: IsolatedComposeProject) -> dict[str, str]:
    result: dict[str, str] = {}
    for service in APP_SERVICES:
        containers = stack.capture(
            "runtime container", "ps", "--all", "--quiet", service
        ).splitlines()
        if len(containers) != 1:
            raise LaneError(
                "runtime_images", "An application service has an unexpected container count."
            )
        result[service] = required_output(
            ["docker", "inspect", "--format", "{{.Image}}", containers[0]],
            environment=stack.environment,
            label="runtime image ID",
        )
    return result


def run_cli_proof(
    private_directory: Path,
    admin_key: Path,
    base_url: str,
    expected_version: str,
    expected_commit: str,
    checks: dict[str, bool],
) -> None:
    client = private_directory / "star_tunnel.py"
    download_url = checked_proof_url(base_url + "/downloads/star_tunnel.py")
    try:
        # This is a checked, unauthenticated HTTP loopback endpoint.
        with urllib.request.urlopen(download_url, timeout=20) as response:  # nosec B310
            content = response.read(1_000_001)
            if response.status != 200 or len(content) > 1_000_000:
                raise LaneError(
                    "downloaded_cli", "The server did not provide a bounded CLI download."
                )
            if "attachment" not in response.headers.get("Content-Disposition", ""):
                raise LaneError("downloaded_cli", "The CLI download disposition is invalid.")
    except (OSError, urllib.error.URLError) as error:
        raise LaneError("downloaded_cli", "The CLI download failed.") from error
    write_private_file(client, content.decode("utf-8"))
    client_env = safe_environment(os.environ)
    client_env["STARTUNNEL_BASE_URL"] = base_url
    version = command_json(client, ["version"], client_env)
    if (
        version.get("version") != expected_version
        or version.get("source_revision") != expected_commit
    ):
        raise LaneError("downloaded_cli", "Downloaded CLI identity does not match the candidate.")
    checks["downloaded_cli"] = True

    admin_env = dict(client_env)
    admin_env["STARTUNNEL_ADMIN_KEY_FILE"] = str(admin_key)
    command_json(client, ["admin", "config"], admin_env)
    admin_me = command_json(client, ["admin", "me"], admin_env)
    admin = object_value(admin_me.get("admin"), "admin_authentication")
    admin_id = string_value(admin, "id", "admin_authentication")
    string_value(admin_me, "credential_id", "admin_authentication")
    string_value(admin_me, "authority", "admin_authentication")
    checks["admin_authentication"] = bool(admin_id)

    first_key = private_directory / "agent-one.key"
    created_agent = command_json(
        client,
        [
            "admin",
            "agents",
            "create",
            "--idempotency-key",
            "release-proof-" + uuid4().hex,
            "--secret-output",
            str(first_key),
        ],
        admin_env,
        {"name": "release-proof-one"},
    )
    resource = object_value(created_agent.get("resource"), "admin_receipt_audit")
    operation = object_value(created_agent.get("operation"), "admin_receipt_audit")
    first_credential_id = string_value(resource, "id", "admin_receipt_audit")
    receipt_id = string_value(operation, "id", "admin_receipt_audit")
    audit_id = string_value(operation, "audit_event_id", "admin_receipt_audit")
    if not first_key.is_file() or first_key.stat().st_mode & 0o077:
        raise LaneError("admin_receipt_audit", "The agent key file is not private.")
    receipt = command_json(client, ["admin", "operations", "get", receipt_id], admin_env)
    record_result = object_value(receipt.get("result"), "admin_receipt_audit")
    receipt_resource = object_value(record_result.get("resource"), "admin_receipt_audit")
    receipt_operation = object_value(record_result.get("operation"), "admin_receipt_audit")
    audit_page = command_json(
        client,
        ["admin", "audit", "list", "--target-id", first_credential_id, "--limit", "100"],
        admin_env,
    )
    items = audit_page.get("items")
    if not isinstance(items, list):
        raise LaneError("admin_receipt_audit", "The audit response has an invalid shape.")
    events = [object_value(item, "admin_receipt_audit") for item in items]
    if (
        receipt.get("id") != receipt_id
        or receipt_resource.get("id") != first_credential_id
        or receipt_operation.get("audit_event_id") != audit_id
        or not any(
            event.get("id") == audit_id
            and event.get("actor_id") == admin_id
            and event.get("target_id") == first_credential_id
            and object_value(event.get("metadata"), "admin_receipt_audit").get("outcome")
            == "success"
            for event in events
        )
    ):
        raise LaneError("admin_receipt_audit", "The admin receipt and audit event do not match.")
    checks["admin_receipt_audit"] = True

    second_key = private_directory / "agent-two.key"
    second_agent_result = command_json(
        client,
        [
            "admin",
            "agents",
            "create",
            "--idempotency-key",
            "release-proof-" + uuid4().hex,
            "--secret-output",
            str(second_key),
        ],
        admin_env,
        {"name": "release-proof-two"},
    )
    if not second_key.is_file() or second_key.stat().st_mode & 0o077:
        raise LaneError("agent_authentication", "The second agent key file is not private.")
    second_credential = object_value(second_agent_result.get("resource"), "agent_authentication")
    second_credential_id = string_value(second_credential, "id", "agent_authentication")
    first_env = dict(client_env)
    first_env["STARTUNNEL_AGENT_KEY_FILE"] = str(first_key)
    second_env = dict(client_env)
    second_env["STARTUNNEL_AGENT_KEY_FILE"] = str(second_key)
    command_json(client, ["config"], first_env)
    command_json(client, ["config"], second_env)
    first_me = command_json(client, ["me"], first_env)
    second_me = command_json(client, ["me"], second_env)
    first_agent = object_value(first_me.get("agent"), "agent_authentication")
    second_agent = object_value(second_me.get("agent"), "agent_authentication")
    first_agent_id = string_value(first_agent, "id", "agent_authentication")
    second_agent_id = string_value(second_agent, "id", "agent_authentication")
    if (
        first_agent_id == second_agent_id
        or first_agent_id != first_credential_id
        or second_agent_id != second_credential_id
    ):
        raise LaneError("agent_authentication", "The two agent credentials are not distinct.")
    checks["agent_authentication"] = True

    created_tunnel = command_json(
        client,
        ["create", "--idempotency-key", "release-proof-" + uuid4().hex],
        first_env,
        {
            "label": "Release proof",
            "cycle": {"root": {"content": {"type": "text", "text": "proof root"}}},
        },
    )
    tunnel = object_value(created_tunnel.get("tunnel"), "two_agent_exchange")
    cycle = object_value(created_tunnel.get("cycle"), "two_agent_exchange")
    root = object_value(cycle.get("root"), "two_agent_exchange")
    address = string_value(tunnel, "address", "two_agent_exchange")
    root_id = string_value(root, "id", "two_agent_exchange")
    reply = command_json(
        client,
        ["reply", "--idempotency-key", "release-proof-" + uuid4().hex],
        second_env,
        {
            "address": address,
            "parent_id": root_id,
            "content": {"type": "text", "text": "proof reply"},
        },
    )
    reply_message = object_value(reply.get("message"), "two_agent_exchange")
    tree = command_json(client, ["read-tree"], second_env, {"address": address})
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 2:
        raise LaneError("two_agent_exchange", "The second agent did not read both messages.")
    messages = [object_value(node, "two_agent_exchange") for node in nodes]
    first_sender = object_value(messages[0].get("sender"), "two_agent_exchange")
    second_sender = object_value(messages[1].get("sender"), "two_agent_exchange")
    if (
        tree.get("root_id") != root_id
        or reply_message.get("parent_id") != root_id
        or reply_message.get("id") not in {message.get("id") for message in messages}
        or messages[0].get("parent_id") is not None
        or first_sender.get("id") != first_agent_id
        or second_sender.get("id") != second_agent_id
    ):
        raise LaneError("two_agent_exchange", "The two-agent root and reply did not match.")
    checks["two_agent_exchange"] = True


def output_path(args_value: str | None, project_name: str) -> Path:
    if args_value:
        return Path(args_value).expanduser().absolute()
    return ROOT / "artifacts" / f"production-release-proof-{project_name}.json"


def write_report(path: Path, report: ReleaseReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    write_private_file(path, encoded)


def safe_failure(report: ReleaseReport, error: BaseException, step: str) -> None:
    report["status"] = "failed"
    report["release_evidence_eligible"] = False
    if report["failure_type"] is not None:
        return
    root_error = error.__cause__ or error
    report["failure_type"] = type(root_error).__name__
    if isinstance(error, LaneError):
        step = error.step
    report["failed_step"] = step if step in FAILURE_STEPS else "proof"


def finish_report(
    report: ReleaseReport,
    *,
    cleanup_complete: bool,
    private_files_removed: bool,
) -> None:
    report["disposable_volumes_removed"] = cleanup_complete
    report["private_files_removed"] = private_files_removed
    passed_checks = report["failure_type"] is None and all(
        report["checks"].get(name) is True for name in CHECK_NAMES
    )
    if not cleanup_complete or not private_files_removed or not passed_checks:
        report["status"] = "failed"
        report["release_evidence_eligible"] = False
        if report["failure_type"] is None:
            failed_cleanup = not cleanup_complete or not private_files_removed
            report["failure_type"] = "cleanup_failed" if failed_cleanup else "incomplete_checks"
            report["failed_step"] = "cleanup" if failed_cleanup else "proof"
    else:
        report["status"] = "passed"
        report["failure_type"] = None
        report["failed_step"] = None
        report["release_evidence_eligible"] = True


def run_proof(
    *,
    image: str,
    commit: str,
    version: str,
    project_name: str,
    report_path: Path,
) -> ReleaseReport:
    report = new_report(commit, version, image)
    timings = PhaseTimings()
    private = tempfile.TemporaryDirectory(prefix="startunnel-production-release-")
    private_directory = Path(private.name)
    private_directory.chmod(0o700)
    stack: ProductionComposeProject | None = None
    contexts = contextlib.ExitStack()
    current_step = "prepare"
    try:
        with timings.phase("prepare"):
            secret_directory = private_directory / "secrets"
            make_secrets(secret_directory)
            port = free_port()
            base_url = f"http://127.0.0.1:{port}"
            overlay = private_directory / "production-release.yaml"
            overlay.write_text(
                "services:\n"
                "  web:\n"
                "    platform: linux/amd64\n"
                "    environment:\n"
                "      DJANGO_SETTINGS_MODULE: startunnel.settings.production\n"
                "      PROMETHEUS_MULTIPROC_DIR: /tmp/startunnel-prometheus\n"
                "    ports: !override\n"
                "      - target: 8000\n"
                f'        published: "{port}"\n'
                "        host_ip: 127.0.0.1\n"
                "        protocol: tcp\n"
                "    volumes:\n"
                "      - type: bind\n"
                f"        source: {json.dumps(str(ROOT / 'scripts/proof_runtime.py'))}\n"
                "        target: /proof/runtime.py\n"
                "        read_only: true\n"
                "  migrate:\n"
                "    platform: linux/amd64\n"
                "  maintenance:\n"
                "    platform: linux/amd64\n",
                encoding="utf-8",
            )
            overlay.chmod(0o600)
            stack = ProductionComposeProject(
                project_name,
                candidate_image=image,
                source_environment=os.environ,
                smoke_overlay=overlay,
            )
        with termination_signals():
            contexts.enter_context(stack)
            proxy_prefix = available_proxy_prefix(stack.environment)
            set_production_environment(
                stack.environment,
                image=image,
                base_url=base_url,
                secret_directory=secret_directory,
                proxy_prefix=proxy_prefix,
            )
            current_step = "render_compose"
            with timings.phase("render_compose"):
                rendered = stack.capture("render_compose", "config", "--format", "json")
                rendered_path = private_directory / "rendered-compose.json"
                write_private_file(rendered_path, rendered)
                model = object_value(json.loads(rendered), "render_compose")
                verify_compose_model(
                    model, image=image, port=port, secret_directory=secret_directory
                )
            current_step = "pull_candidate"
            with timings.phase("pull_candidate"):
                run_process(
                    ["docker", "pull", "--platform", PLATFORM, image],
                    environment=stack.environment,
                    timeout=600,
                    step="pull_candidate",
                )
                labels, platform = image_metadata(image, stack.environment)
                validate_candidate_metadata(
                    labels, platform=platform, commit=commit, version=version
                )
                report["checks"]["source_identity"] = True
            current_step = "start_services"
            with timings.phase("start_services"):
                stack.run(
                    "start_services",
                    "up",
                    "--detach",
                    "--no-build",
                    "postgres",
                    *APP_SERVICES,
                    timeout_seconds=600,
                )
            current_step = "runtime_identity"
            with timings.phase("runtime_identity"):
                expected_id = required_output(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", image],
                    environment=stack.environment,
                    label="candidate image ID",
                )
                images = runtime_image_ids(stack)
                verify_runtime_image_ids(expected_id, images)
                report["image_ids"] = images
                report["checks"]["runtime_images"] = True
                settings = stack.capture(
                    "production_settings",
                    "exec",
                    "-T",
                    "web",
                    "/app/.venv/bin/python",
                    "/proof/runtime.py",
                    "settings",
                )
                if settings != "startunnel.settings.production":
                    raise LaneError(
                        "production_settings", "The web service is not using production settings."
                    )
                report["checks"]["production_settings"] = True
            current_step = "health"
            with timings.phase("health"):
                for name, path in (("health_live", "live"), ("health_ready", "ready")):
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline and not health_ok(
                        f"{base_url}/health/{path}"
                    ):
                        time.sleep(2)
                    if not health_ok(f"{base_url}/health/{path}"):
                        raise LaneError(name, "A required health endpoint did not pass.")
                    report["checks"][name] = True
            current_step = "bootstrap_admin"
            with timings.phase("bootstrap_admin"):
                before = metrics_inodes(stack)
                admin_key = admin_key_from_stack(stack, private_directory)
                after = metrics_inodes(stack)
                if not metrics_preserved(before, after):
                    raise LaneError(
                        "bootstrap_admin", "Bootstrap changed an existing metrics file inode."
                    )
                report["checks"]["metrics_files_preserved"] = True
            current_step = "download_cli"
            with timings.phase("download_cli"):
                run_cli_proof(
                    private_directory,
                    admin_key,
                    base_url,
                    version,
                    commit,
                    report["checks"],
                )
    except BaseException as error:
        safe_failure(report, error, current_step)
    finally:
        try:
            with timings.phase("cleanup"):
                try:
                    contexts.close()
                finally:
                    private.cleanup()
                if not (stack and stack.cleanup_complete) or private_directory.exists():
                    raise LaneError("cleanup", "Disposable resources were not fully removed.")
        except BaseException as error:
            safe_failure(report, error, "cleanup")
        finish_report(
            report,
            cleanup_complete=bool(stack and stack.cleanup_complete),
            private_files_removed=not private_directory.exists(),
        )
        report["phases"] = timings.records
        write_report(report_path, report)
    return report


def run_process(
    command: Sequence[str], *, environment: Mapping[str, str], timeout: int, step: str
) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=ROOT,
            env=dict(environment),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaneError(step, "A bounded proof command did not finish.") from error
    if result.returncode:
        raise LaneError(step, "A bounded proof command failed.")
    return result.stdout.strip()


def image_metadata(image: str, environment: Mapping[str, str]) -> tuple[dict[str, str], str]:
    labels: dict[str, str] = {}
    for name in ("org.opencontainers.image.revision", "org.opencontainers.image.version"):
        labels[name] = required_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{name}" }}}}',
                image,
            ],
            environment=environment,
            label="candidate image metadata",
        )
    platform = required_output(
        ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image],
        environment=environment,
        label="candidate platform",
    )
    return labels, platform


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-image", required=True, help="Immutable image reference with @sha256 digest"
    )
    parser.add_argument(
        "--project-name", help="Optional unique name with an approved stproof- prefix"
    )
    parser.add_argument("--output", help="Safe report path; existing paths are never overwritten")
    args = parser.parse_args(argv)
    try:
        image = validate_image_reference(args.candidate_image, name="Candidate image")
        commit = required_output(
            ["git", "rev-parse", "HEAD"],
            environment=safe_environment(os.environ),
            label="source commit",
        )
        version = candidate_version()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Git returned an invalid source commit.")
        project_name = (
            validate_project_name(args.project_name)
            if args.project_name
            else generated_project_name("system")
        )
        destination = output_path(args.output, project_name)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Report path already exists.")
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Production release proof setup failed: {type(error).__name__}.", file=sys.stderr)
        return 2
    report = run_proof(
        image=image,
        commit=commit,
        version=version,
        project_name=project_name,
        report_path=destination,
    )
    print(json.dumps({"status": report["status"], "report": str(destination)}, sort_keys=True))
    return 0 if report["release_evidence_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
