"""Build isolated Compose proof images with per-target BuildKit cache scopes."""

from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import re
import subprocess  # nosec B404
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import JsonObject, json_object, json_string
else:
    from json_data import JsonObject, json_object, json_string

ROOT = Path(__file__).resolve().parents[1]
GHA_CACHE_ENV = ("ACTIONS_CACHE_URL", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL")
GHA_CACHE_REQUIRED_ENV = ("ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL")
SENSITIVE_LINE = re.compile(
    r"password|secret|token|api[_-]?key|credential|glyph|payload|"
    r"(?:postgres(?:ql)?|mysql|redis|amqp)://|0x[0-9a-f]{32,}",
    re.IGNORECASE,
)
CONNECTION_STRING = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s]+", re.IGNORECASE)


BakeTarget = TypedDict(
    "BakeTarget",
    {
        "context": str,
        "dockerfile": str,
        "target": str,
        "args": JsonObject | None,
        "labels": JsonObject,
        "tags": list[str],
        "no-cache": bool,
        "cache-from": list[str],
        "cache-to": list[str],
    },
    total=False,
)


class BuildProvenance(TypedDict):
    started_at: str
    duration_seconds: float
    services: list[str]
    image_ids: dict[str, str]
    build_arguments: dict[str, dict[str, str]]
    build_metadata: dict[str, dict[str, str]]
    platform: str
    source_revision: str
    base_images: dict[str, str]
    hashes: dict[str, str]
    tool_versions: dict[str, str]
    cache: str
    cache_hits: int
    cache_error_fallback: bool
    no_cache_requested: bool
    provenance_file: NotRequired[str]


class ProofBuildError(RuntimeError):
    """A disposable proof image build failed."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:100] or "unknown"


def _build_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in source.items() if key not in GHA_CACHE_ENV}
    if source.get("GITHUB_ACTIONS") == "true" and all(
        source.get(key) for key in GHA_CACHE_REQUIRED_ENV
    ):
        environment.update({key: source[key] for key in GHA_CACHE_ENV if source.get(key)})
    return environment


def _run(
    command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603
            list(command),
            cwd=ROOT,
            env=dict(environment),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProofBuildError("The isolated Buildx proof build did not finish.") from error


def _tool_version(command: Sequence[str], environment: Mapping[str, str]) -> str:
    result = _run(command, environment, 30)
    if result.returncode:
        raise ProofBuildError("Could not inspect the Docker Buildx version.")
    return result.stdout.strip()[:200]


def _safe_failure_report(output: str, environment: Mapping[str, str]) -> str:
    """Return a bounded Buildx diagnostic without credentials or payloads."""

    secret_values = sorted(
        (
            value
            for name, value in environment.items()
            if value and re.search(r"token|password|secret|api[_-]?key|credential", name, re.I)
        ),
        key=len,
        reverse=True,
    )
    safe_lines: list[str] = []
    for line in output.splitlines()[-80:]:
        safe_line = line
        for value in secret_values:
            safe_line = safe_line.replace(value, "[redacted]")
        if SENSITIVE_LINE.search(safe_line) or any(ord(character) > 127 for character in safe_line):
            safe_line = "[redacted sensitive build output]"
        else:
            safe_line = CONNECTION_STRING.sub("[redacted connection string]", safe_line)
        safe_lines.append(safe_line[:500])
    return "\n".join(safe_lines)


def build_services(
    services: Sequence[str],
    *,
    compose_model: str,
    environment: Mapping[str, str],
    no_cache: bool = False,
    timeout_seconds: int = 2400,
) -> BuildProvenance:
    """Build named Compose services and write safe build provenance."""

    if not services or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in services):
        raise ProofBuildError("Build targets must be named Compose services.")

    docker_environment = _build_environment(environment)
    platform_result = _run(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"],
        docker_environment,
        30,
    )
    if platform_result.returncode or not re.fullmatch(
        r"[a-z0-9]+/[a-z0-9_]+", platform_result.stdout.strip()
    ):
        raise ProofBuildError("Could not inspect the Docker build platform.")
    operating_system, architecture = platform_result.stdout.strip().split("/", maxsplit=1)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    target_platform = f"{operating_system}/{architecture}"
    source_revision = _run(["git", "rev-parse", "HEAD"], docker_environment, 30)
    if source_revision.returncode or not re.fullmatch(
        r"[0-9a-f]{40,64}", source_revision.stdout.strip()
    ):
        raise ProofBuildError("Could not inspect the source revision for the image label.")
    revision = source_revision.stdout.strip()

    cache_enabled = (
        not no_cache
        and docker_environment.get("GITHUB_ACTIONS") == "true"
        and all(docker_environment.get(key) for key in GHA_CACHE_REQUIRED_ENV)
    )
    try:
        model = json_object(json.loads(compose_model))
    except ValueError as error:
        raise ProofBuildError("Docker Compose returned an invalid build model.") from error
    model_services = model.get("services") if isinstance(model, dict) else None
    targets: dict[str, BakeTarget] = {}
    build_arguments: dict[str, dict[str, str]] = {}
    image_tags: dict[str, str] = {}
    for service in services:
        definition = model_services.get(service) if isinstance(model_services, dict) else None
        build = definition.get("build") if isinstance(definition, dict) else None
        image = definition.get("image") if isinstance(definition, dict) else None
        if not isinstance(build, dict) or not isinstance(image, str):
            raise ProofBuildError(
                f"Compose service {service} has no build definition or image tag."
            )
        target: BakeTarget = {}
        try:
            if "context" in build:
                target["context"] = json_string(build["context"])
            if "dockerfile" in build:
                target["dockerfile"] = json_string(build["dockerfile"])
            if "target" in build:
                target["target"] = json_string(build["target"])
            if "args" in build:
                target["args"] = None if build["args"] is None else json_object(build["args"])
            if "labels" in build:
                labels_value = build["labels"]
                target["labels"] = (
                    json_object(labels_value) if isinstance(labels_value, dict) else {}
                )
        except ValueError as error:
            raise ProofBuildError(
                f"Compose service {service} has an invalid build definition."
            ) from error
        target["tags"] = [image]
        image_tags[service] = image
        raw_args = target.get("args")
        build_arguments[service] = (
            {
                str(key): "<redacted>"
                if re.search(r"secret|token|password|credential|key", str(key), re.IGNORECASE)
                else str(value)
                for key, value in raw_args.items()
            }
            if isinstance(raw_args, dict)
            else {}
        )
        labels = target.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        target["labels"] = {**labels, "net.startunnel.source": revision}
        if no_cache:
            target["no-cache"] = True
        scope = _safe_component(f"startunnel-{service}-{target_platform}")
        if cache_enabled:
            target["cache-from"] = [f"type=gha,version=2,scope={scope}"]
            target["cache-to"] = [f"type=gha,version=2,scope={scope},mode=max"]
        targets[service] = target

    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    command = ["docker", "buildx", "bake", "--load", "--progress", "plain"]
    metadata: dict[str, dict[str, str]] = {}
    cache_error = False
    cache_hits = 0
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as bake_file:
        os.chmod(bake_file.name, 0o600)
        bake_file.write(json.dumps({"target": targets}, sort_keys=True))
        bake_file.flush()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8"
        ) as metadata_file:
            os.chmod(metadata_file.name, 0o600)
            result = _run(
                [
                    *command,
                    "--metadata-file",
                    metadata_file.name,
                    "--file",
                    bake_file.name,
                    *services,
                ],
                docker_environment,
                timeout_seconds,
            )
            output = result.stdout + "\n" + result.stderr
            cache_hits = sum("CACHED" in line.upper() for line in output.splitlines())
            if (
                result.returncode
                and cache_enabled
                and re.search(
                    r"cache (?:import|export)|gha cache|failed to (?:export|import) cache",
                    output,
                    re.IGNORECASE,
                )
            ):
                cache_error = True
                for build_config in targets.values():
                    if isinstance(build_config, dict):
                        build_config.pop("cache-from", None)
                        build_config.pop("cache-to", None)
                bake_file.seek(0)
                bake_file.truncate()
                bake_file.write(json.dumps({"target": targets}, sort_keys=True))
                bake_file.flush()
                cache_hits = sum("CACHED" in line.upper() for line in output.splitlines())
                result = _run(
                    [
                        *command,
                        "--metadata-file",
                        metadata_file.name,
                        "--file",
                        bake_file.name,
                        *services,
                    ],
                    docker_environment,
                    timeout_seconds,
                )
                output += "\n" + result.stdout + "\n" + result.stderr
                cache_hits += sum(
                    "CACHED" in line.upper()
                    for line in (result.stdout + "\n" + result.stderr).splitlines()
                )
            if result.returncode == 0:
                try:
                    raw_metadata: object = json.loads(
                        Path(metadata_file.name).read_text(encoding="utf-8")
                    )
                    if isinstance(raw_metadata, dict):
                        metadata = {
                            service: {
                                key: value
                                for key, value in entry.items()
                                if key in ("containerimage.digest", "containerimage.config.digest")
                                and isinstance(value, str)
                                and re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                            }
                            for service, entry in json_object(raw_metadata).items()
                            if service in services and isinstance(entry, dict)
                        }
                except ValueError:
                    metadata = {}
    elapsed = round(time.monotonic() - started, 3)
    if result.returncode:
        phase = next(
            (
                match.group(1)
                for line in reversed(output.splitlines())
                if (match := re.search(r"ERROR \[([a-zA-Z0-9_-]+) [0-9]+/[0-9]+\]", line))
            ),
            "unknown",
        )
        safe_output = _safe_failure_report(output, docker_environment)
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        log_path = artifacts / f"proof-build-failure-{stamp}.log"
        log_path.write_text(f"Buildx failed at phase: {phase}\n{safe_output}\n", encoding="utf-8")
        log_path.chmod(0o600)
        print(f"Buildx failed at phase: {phase}. Safe diagnostic: {log_path.name}", flush=True)
        if safe_output:
            print(safe_output, flush=True)
        raise ProofBuildError(f"The isolated Buildx proof build failed at phase {phase}.")

    versions = {
        "docker": _tool_version(
            ["docker", "version", "--format", "{{.Server.Version}}"], docker_environment
        ),
        "buildx": _tool_version(["docker", "buildx", "version"], docker_environment),
        "python": host_platform.python_version(),
    }
    image_ids: dict[str, str] = {}
    for service, image in image_tags.items():
        inspected = _run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                (
                    '{{.Id}}|{{index .Config.Labels "net.startunnel.source"}}|'
                    "{{.Os}}/{{.Architecture}}"
                ),
                image,
            ],
            docker_environment,
            30,
        )
        fields = inspected.stdout.strip().split("|")
        expected_platform = target_platform
        if (
            inspected.returncode
            or len(fields) != 3
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", fields[0])
            or fields[1] != revision
            or fields[2] != expected_platform
        ):
            raise ProofBuildError(f"Could not inspect the built proof image for {service}.")
        image_ids[service] = fields[0]
    base_images = {}
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        match = re.match(r"ARG ([A-Z0-9_]+_IMAGE)=\"?([^\" ]+)", line)
        if match:
            base_images[match.group(1)] = match.group(2)
    provenance: BuildProvenance = {
        "started_at": started_at,
        "duration_seconds": elapsed,
        "services": list(services),
        "image_ids": image_ids,
        "build_arguments": build_arguments,
        "build_metadata": metadata,
        "platform": target_platform,
        "source_revision": revision,
        "base_images": base_images,
        "hashes": {
            name: _sha256(ROOT / name) for name in ("Dockerfile", "pyproject.toml", "pdm.lock")
        },
        "tool_versions": versions,
        "cache": "gha" if cache_enabled else "local-buildkit",
        "cache_hits": cache_hits,
        "cache_error_fallback": cache_error,
        "no_cache_requested": no_cache,
    }
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = artifacts / f"proof-build-{stamp}.json"
    temporary = path.with_suffix(".json.tmp")
    provenance["provenance_file"] = path.name
    temporary.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    print(
        f"Build provenance: {path.name}; cache={provenance['cache']}; seconds={elapsed}", flush=True
    )
    return provenance
