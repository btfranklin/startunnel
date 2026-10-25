"""Own fail-closed isolation for disposable Docker Compose projects."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import uuid4

if TYPE_CHECKING or __package__:
    from .proof_build import BuildProvenance, ProofBuildError
    from .proof_build import build_services as build_proof_services
    from .start_production import validate_image_reference
else:
    from proof_build import BuildProvenance, ProofBuildError
    from proof_build import build_services as build_proof_services
    from start_production import validate_image_reference

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("compose.yaml", "compose.test.yaml")
DISPOSABLE_BUILD_LABEL = "net.startunnel.disposable-build=true"
SAFE_PROJECT_PREFIXES = ("stci-", "sttest-", "stproof-", "strelease-", "stload-")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{7,62}")
PRODUCTION_PROJECT_TOKENS = frozenset({"prod", "production", "stage", "staging", "startunnel"})
SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "BUILDKIT_PROGRESS",
        "CI",
        "DOCKER_BUILDKIT",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "FORCE_COLOR",
        "GITHUB_ACTIONS",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "RUNNER_TEMP",
        "STARTUNNEL_LOAD_ACTIVITY_READERS",
        "STARTUNNEL_LOAD_CREDENTIALS",
        "STARTUNNEL_LOAD_DRAIN_SECONDS",
        "STARTUNNEL_LOAD_DURATION_SECONDS",
        "STARTUNNEL_LOAD_SENDS_PER_SECOND",
        "TERM",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
    }
)


class IsolationError(RuntimeError):
    """A requested lane could affect a non-test Docker target."""


class LaneError(RuntimeError):
    """A bounded validation step failed."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


def generated_project_name(lane: str) -> str:
    """Return a low-collision test project name that is not the main project."""

    prefix = {"load": "stload", "system": "stproof"}.get(lane, "sttest")
    safe_lane = re.sub(r"[^a-z0-9]+", "-", lane.lower()).strip("-")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{safe_lane}-{stamp}-{os.getpid()}-{uuid4().hex[:8]}"


def validate_project_name(project_name: str) -> str:
    """Reject the shared project name and names that can describe a deployment."""

    if not PROJECT_PATTERN.fullmatch(project_name):
        raise IsolationError(
            "The project name must contain 8 to 63 lowercase letters, digits, dashes, "
            "or underscores."
        )
    if not project_name.startswith(SAFE_PROJECT_PREFIXES):
        raise IsolationError(
            "The project name must use an approved disposable prefix: "
            + ", ".join(SAFE_PROJECT_PREFIXES)
        )
    tokens = frozenset(re.split(r"[-_]", project_name))
    if tokens & PRODUCTION_PROJECT_TOKENS:
        raise IsolationError("The project name looks like a shared or production project.")
    return project_name


def _local_docker_endpoint(endpoint: str) -> bool:
    if endpoint.startswith(("unix://", "npipe://")):
        return True
    if endpoint.startswith("tcp://"):
        host = urlsplit(endpoint.replace("tcp://", "http://", 1)).hostname
        return host in {"localhost", "127.0.0.1", "::1"}
    return False


def assert_safe_target(source: Mapping[str, str], project_name: str) -> None:
    """Check the disposable project and the Docker endpoint."""

    validate_project_name(project_name)
    docker_host = source.get("DOCKER_HOST", "")
    if docker_host and not _local_docker_endpoint(docker_host):
        raise IsolationError("DOCKER_HOST names a remote Docker endpoint.")


def safe_environment(
    source: Mapping[str, str] | None = None,
    *,
    include_openai: bool = False,
) -> dict[str, str]:
    """Return the small host environment that the Docker CLI needs."""

    original = dict(os.environ if source is None else source)
    cleaned = {
        name: value for name, value in original.items() if name in SAFE_ENVIRONMENT_NAMES and value
    }
    if include_openai:
        for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
            value = original.get(name, "")
            if value:
                cleaned[name] = value
    return cleaned


def _run_process(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    # Callers supply fixed and validated argument vectors.
    return subprocess.run(  # nosec B603
        list(command),
        cwd=ROOT,
        env=dict(environment),
        text=True,
        capture_output=capture_output,
        timeout=timeout_seconds,
        check=False,
    )


def required_output(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    label: str,
) -> str:
    try:
        result = _run_process(
            command,
            environment=environment,
            timeout_seconds=30,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IsolationError(f"Could not inspect {label}.") from error
    if result.returncode:
        raise IsolationError(f"Could not inspect {label}. Check that Docker is running.")
    return result.stdout.strip()


def available_proxy_prefix(environment: Mapping[str, str]) -> str:
    """Select a private proof subnet that does not overlap a Docker network."""

    identifiers = required_output(
        ["docker", "network", "ls", "--quiet"],
        environment=environment,
        label="existing network identifiers",
    ).splitlines()
    occupied = []
    if identifiers:
        networks = json.loads(
            required_output(
                ["docker", "network", "inspect", *identifiers],
                environment=environment,
                label="existing network subnets",
            )
        )
        for network in networks:
            for configuration in network.get("IPAM", {}).get("Config") or []:
                if subnet := configuration.get("Subnet"):
                    occupied.append(ip_network(subnet, strict=False))
    for second in range(16, 32):
        for third in range(1, 255):
            candidate = IPv4Network(f"172.{second}.{third}.0/24")
            if not any(candidate.overlaps(network) for network in occupied if network.version == 4):
                return f"172.{second}.{third}"
    raise IsolationError("No unused private proxy subnet is available for this proof.")


def assert_local_docker(environment: Mapping[str, str]) -> str:
    """Reject SSH and non-loopback TCP Docker endpoints."""

    raw_endpoint = required_output(
        ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
        environment=environment,
        label="the active Docker endpoint",
    )
    try:
        endpoint = json.loads(raw_endpoint)
    except json.JSONDecodeError as error:
        raise IsolationError("Docker returned an invalid endpoint value.") from error
    if not isinstance(endpoint, str):
        raise IsolationError("Docker returned an invalid endpoint value.")
    if _local_docker_endpoint(endpoint):
        return endpoint
    raise IsolationError("The active Docker endpoint is remote. Release proof must run locally.")


def assert_project_unused(project_name: str, environment: Mapping[str, str]) -> None:
    """Do not delete a project that another process already owns."""

    filters = (
        ("containers", ["docker", "ps", "--all", "--quiet", "--filter"]),
        ("volumes", ["docker", "volume", "ls", "--quiet", "--filter"]),
        ("networks", ["docker", "network", "ls", "--quiet", "--filter"]),
    )
    label = f"label=com.docker.compose.project={project_name}"
    for resource, command in filters:
        output = required_output(
            [*command, label],
            environment=environment,
            label=f"existing {resource} for {project_name}",
        )
        if output:
            raise IsolationError(
                f"The disposable project already has {resource}. Choose a new project name."
            )
    named_resources = (
        (
            "data volume",
            ["docker", "volume", "ls", "--quiet"],
            f"{project_name}_postgres_data",
        ),
        (
            "network",
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            f"{project_name}_startunnel",
        ),
    )
    for resource, command, expected_name in named_resources:
        names = required_output(
            command,
            environment=environment,
            label=f"the named {resource} for {project_name}",
        ).splitlines()
        if expected_name in names:
            raise IsolationError(
                f"The disposable project already has its named {resource}. "
                "Choose a new project name."
            )


def prune_disposable_images(environment: Mapping[str, str]) -> None:
    """Remove only untagged, unused images marked as StarTunnel test builds."""

    print("[Remove unused StarTunnel test images]", flush=True)
    try:
        result = _run_process(
            [
                "docker",
                "image",
                "prune",
                "--force",
                "--filter",
                f"label={DISPOSABLE_BUILD_LABEL}",
            ],
            environment=environment,
            timeout_seconds=180,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IsolationError("Could not remove unused StarTunnel test images.") from error
    if result.returncode:
        if (
            result.stderr.strip()
            == "Error response from daemon: a prune operation is already running"
        ):
            return
        raise IsolationError("Could not remove unused StarTunnel test images.")


@contextmanager
def termination_signals() -> Iterator[None]:
    """Convert termination signals to exceptions so Compose cleanup can run."""

    previous: dict[signal.Signals, Callable[[int, FrameType | None], object] | int | None] = {}

    def stop(signum: int, _frame: FrameType | None) -> None:
        raise InterruptedError(f"Received signal {signum}.")

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, stop)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class IsolatedComposeProject:
    """Own one new Compose project and always remove its volumes."""

    def __init__(
        self,
        project_name: str,
        *,
        profiles: Sequence[str] = (),
        include_openai: bool = False,
        source_environment: Mapping[str, str] | None = None,
        candidate_image: str | None = None,
    ) -> None:
        if candidate_image:
            try:
                validate_image_reference(candidate_image, name="Candidate image")
            except ValueError as error:
                raise IsolationError(str(error)) from error
        self.candidate_image = candidate_image
        self._runtime_overlay: Path | None = None
        self.project_name = validate_project_name(project_name)
        self.profiles = tuple(profiles)
        self.source_environment = dict(
            os.environ if source_environment is None else source_environment
        )
        assert_safe_target(self.source_environment, self.project_name)
        self.environment = safe_environment(
            self.source_environment,
            include_openai=include_openai,
        )
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._env_file: Path | None = None
        self._owned = False
        self.cleanup_complete = False

    def __enter__(self) -> IsolatedComposeProject:
        assert_local_docker(self.environment)
        assert_project_unused(self.project_name, self.environment)
        prune_disposable_images(self.environment)
        self._temporary = tempfile.TemporaryDirectory(prefix="startunnel-compose-")
        self._env_file = Path(self._temporary.name) / "empty.env"
        self._env_file.write_text("# Intentionally empty.\n", encoding="utf-8")
        self._env_file.chmod(0o600)
        # Linux bind mounts need the host identity. Docker Desktop maps macOS
        # file ownership; its low host group IDs can collide with image groups.
        # The candidate runtime keeps its original image user ID.
        helper_uid = (os.getuid() or 10001) if sys.platform == "linux" else 10001
        helper_gid = (os.getgid() or 10001) if sys.platform == "linux" else 10001
        self.environment.update(
            STARTUNNEL_CONTAINER_UID=str(helper_uid), STARTUNNEL_CONTAINER_GID=str(helper_gid)
        )
        if "proxy-proof" in self.profiles:
            self.environment["STARTUNNEL_PROXY_IPV4_PREFIX"] = available_proxy_prefix(
                self.environment
            )
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(mode=0o700, exist_ok=True)
        if os.getuid() == 0:
            os.chown(artifacts, helper_uid, helper_gid)
        if self.candidate_image or "load" in self.profiles:
            self._runtime_overlay = Path(self._temporary.name) / "runtime.yaml"
            overlay = "services:\n"
            services = (
                ("web", "migrate", "maintenance", "proxy-web") if self.candidate_image else ("web",)
            )
            for service in services:
                overlay += f"  {service}:\n"
                if self.candidate_image:
                    overlay += (
                        f"    build: !reset null\n    image: {self.candidate_image}\n"
                        "    platform: linux/amd64\n"
                    )
                if service == "web":
                    overlay += "    environment:\n"
                    if self.candidate_image:
                        overlay += "      DJANGO_SETTINGS_MODULE: startunnel.settings.development\n"
                    if "load" in self.profiles:
                        # Keep file metrics in the disposable container's private tmpfs.
                        overlay += (
                            '      STARTUNNEL_PROOF_METRICS: "1"\n'
                            "      PROMETHEUS_MULTIPROC_DIR: /tmp/startunnel-proof-metrics\n"
                        )
            self._runtime_overlay.write_text(overlay, encoding="utf-8")
            self._runtime_overlay.chmod(0o600)
        self._owned = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        cleanup_error: BaseException | None = None
        if self._owned:
            try:
                self.cleanup()
            except BaseException as error:  # Cleanup must not hide an earlier lane failure.
                cleanup_error = error
        if self._temporary is not None:
            self._temporary.cleanup()
        if cleanup_error is not None and exception is None:
            raise cleanup_error
        if cleanup_error is not None:
            print(f"Cleanup also failed: {cleanup_error}", file=sys.stderr)
        return False

    def compose_files(self) -> tuple[str, ...]:
        files = tuple(COMPOSE_FILES)
        if self._runtime_overlay is not None:
            files += (str(self._runtime_overlay),)
        return files

    def compose_command(self, *arguments: str) -> list[str]:
        if self._env_file is None:
            raise IsolationError("The isolated Compose project is not active.")
        command = ["docker", "compose", "--env-file", str(self._env_file)]
        for compose_file in self.compose_files():
            command.extend(("-f", compose_file))
        command.extend(("--project-name", self.project_name))
        for profile in self.profiles:
            command.extend(("--profile", profile))
        command.extend(arguments)
        return command

    def run(
        self,
        step: str,
        *arguments: str,
        timeout_seconds: int,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        print(f"[{step}]", flush=True)
        try:
            result = _run_process(
                self.compose_command(*arguments),
                environment=environment or self.environment,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneError(step, f"{step} did not finish inside its time limit.") from error
        if result.returncode:
            raise LaneError(step, f"{step} failed with exit code {result.returncode}.")

    def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
        try:
            result = _run_process(
                self.compose_command(*arguments),
                environment=self.environment,
                timeout_seconds=timeout_seconds,
                capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneError(step, f"Could not inspect {step}.") from error
        if result.returncode:
            raise LaneError(step, f"Could not inspect {step}.")
        return result.stdout.strip()

    def build_services(
        self,
        services: Sequence[str],
        *,
        no_cache: bool = False,
        timeout_seconds: int = 2400,
    ) -> BuildProvenance:
        """Build selected proof services through Buildx with isolated cache scopes."""

        model = self.capture(
            "the sanitized proof build model",
            "config",
            "--format",
            "json",
        )
        print(f"[Build proof services: {', '.join(services)}]", flush=True)
        build_environment = dict(self.environment)
        for name in ("ACTIONS_CACHE_URL", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL"):
            if value := self.source_environment.get(name):
                build_environment[name] = value
        try:
            return build_proof_services(
                services,
                compose_model=model,
                environment=build_environment,
                no_cache=no_cache,
                timeout_seconds=timeout_seconds,
            )
        except ProofBuildError as error:
            raise LaneError("Build proof services", str(error)) from error

    def pull_candidate(self) -> None:
        if not self.candidate_image:
            raise IsolationError("No candidate image was selected.")
        try:
            pulled = _run_process(
                ["docker", "pull", "--platform", "linux/amd64", self.candidate_image],
                environment=self.environment,
                timeout_seconds=600,
                capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise IsolationError("The candidate image pull failed or timed out.") from error
        if pulled.returncode:
            raise IsolationError("The candidate image pull failed; check registry access.")
        revision = required_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                self.candidate_image,
            ],
            environment=self.environment,
            label="the candidate source revision",
        )
        commit = required_output(
            ["git", "rev-parse", "HEAD"],
            environment=self.environment,
            label="the current source commit",
        )
        if revision != commit:
            raise IsolationError("Candidate image source revision differs from this checkout.")
        version = required_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.version" }}',
                self.candidate_image,
            ],
            environment=self.environment,
            label="the candidate version",
        )
        if version != "v" + (ROOT / "VERSION").read_text().strip():
            raise IsolationError("Candidate image version differs from VERSION.")
        platform = required_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                self.candidate_image,
            ],
            environment=self.environment,
            label="the candidate platform",
        )
        if platform != "linux/amd64":
            raise IsolationError("Candidate platform must be linux/amd64.")

    def verify_candidate_services(self) -> None:
        if not self.candidate_image:
            raise IsolationError("No candidate image was selected.")
        expected = required_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.candidate_image],
            environment=self.environment,
            label="the candidate image ID",
        )
        services: tuple[str, ...] = ("web", "migrate", "maintenance")
        if "proxy-proof" in self.profiles:
            services += ("proxy-web",)
        for service in services:
            images = self.capture(
                "candidate service image", "images", "--quiet", service
            ).splitlines()
            if len(images) != 1:
                raise LaneError("candidate image proof", f"Expected one image for {service}.")
            actual = required_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", images[0]],
                environment=self.environment,
                label=f"the {service} image ID",
            )
            if actual != expected:
                raise LaneError(
                    "candidate image proof", f"{service} did not run the candidate image."
                )

    def cleanup(self) -> None:
        print("[Remove disposable containers and volumes]", flush=True)
        try:
            result = _run_process(
                self.compose_command(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "30",
                ),
                environment=self.environment,
                timeout_seconds=180,
                capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise IsolationError(
                "Could not remove the disposable Compose project and its volumes."
            ) from error
        if result.returncode:
            raise IsolationError("Could not remove the disposable Compose project and its volumes.")
        self.cleanup_complete = True
        prune_disposable_images(self.environment)
