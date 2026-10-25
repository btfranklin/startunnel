"""Protect release-lane isolation and provider-secret boundaries."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from http.client import HTTPMessage
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml
from examples.python.startunnel_client import StarTunnelError
from scripts import compose_isolation as isolation
from scripts import prove_activity_restart as activity_worker
from scripts import run_full_load_profile as load_runner
from scripts import run_isolated_test_lane as lane_runner
from scripts import run_resource_measurement as resource_runner
from scripts import system_proof
from scripts.json_data import JsonObject, json_array, json_object, json_string
from tests.structure.workflows import load_workflow

ROOT = Path(__file__).resolve().parents[2]


class RunCall(NamedTuple):
    step: str
    arguments: tuple[str, ...]
    environment: dict[str, str]


def _assert_postgres_permission_tools_are_mounted(services: JsonObject) -> None:
    postgres = json_object(services["postgres"])
    volumes = {
        json_string(volume["target"]): volume
        for volume in (json_object(item) for item in json_array(postgres["volumes"]))
    }
    for target, source in (
        (
            "/usr/local/share/startunnel/history-grants.sql",
            "src/tunnels/migrations/sql/history-grants.sql",
        ),
        (
            "/usr/local/share/startunnel/runtime-history-guards.sql",
            "src/tunnels/migrations/sql/runtime-history-guards.sql",
        ),
        ("/usr/local/bin/startunnel-check-runtime-role", "docker/postgres/check-runtime-role.sh"),
    ):
        assert volumes[target]["source"] == str(ROOT / source)
        assert volumes[target]["read_only"] is True


@pytest.mark.parametrize(
    ("candidate_image", "build_images"),
    [(None, False), (None, True), ("registry.example/startunnel@sha256:" + "a" * 64, False)],
)
def test_system_outage_proof_checks_online_backup_availability_before_database_loss(
    monkeypatch: pytest.MonkeyPatch,
    candidate_image: str | None,
    build_images: bool,
) -> None:
    class Stack:
        project_name = "stproof-online-backup"
        candidate_image: str | None = None
        environment: dict[str, str] = {}
        running = False
        runtime_contents_checked = False
        backup_finished = False
        availability_checked = False
        database_loss_started = False
        builds: list[tuple[str, ...]] = []
        proof_service_creations: list[tuple[str, ...]] = []
        proof_services_created = False

        def pull_candidate(self) -> None:
            pass

        def verify_candidate_services(self) -> None:
            assert self.proof_services_created

        def build_services(
            self, services: Sequence[str], *, no_cache: bool = False, timeout_seconds: int = 2400
        ) -> dict[str, object]:
            del timeout_seconds
            self.builds.append(("build", *(("--no-cache",) if no_cache else ()), *services))
            return {"services": list(services)}

        def run(self, label: str, *args: str, **kwargs: object) -> None:
            del label, kwargs
            if args[0] == "build":
                self.builds.append(args)
            if args[0] == "create":
                self.proof_service_creations.append(args)
                self.proof_services_created = True
            if args[0] == "up" and "--wait" in args:
                self.running = True
            if args[-1] == "runtime-contents-proof":
                assert self.running
                assert args[:5] == ("exec", "-T", "web", "/app/.venv/bin/python", "-c")
                self.runtime_contents_checked = True
            if self.backup_finished and not self.database_loss_started:
                if args[-1].startswith("health-proof-200"):
                    self.availability_checked = True
                elif args[0] != "stop":
                    assert args[0] not in {"up", "restart", "stop"}
            if args[0] == "stop" and args[-1] == "postgres":
                assert self.availability_checked
                self.database_loss_started = True
            if args[-1].startswith("health-proof-"):
                assert self.running

        def capture(self, *args: str, **kwargs: object) -> str:
            return "Application service started."

    stack = Stack()
    stack.candidate_image = candidate_image

    def online_backup(value: Stack) -> None:
        value.backup_finished = True

    monkeypatch.setattr(system_proof, "_coordinated_restore_proof", online_backup)
    monkeypatch.setattr(system_proof, "_runtime_contents_script", lambda: "runtime-contents-proof")
    monkeypatch.setattr(
        system_proof,
        "_web_health_script",
        lambda status, *, path="/health/ready": f"health-proof-{status}-{path}",
    )
    monkeypatch.setattr(system_proof, "_assert_reusable_images", lambda value: None)
    monkeypatch.setattr(system_proof, "_run_activity_restart_proof", lambda value: None)
    monkeypatch.setattr(system_proof, "_system_host_profile", lambda value: {})
    monkeypatch.setattr(system_proof, "_system_image_ids", lambda value: {})
    monkeypatch.setattr(system_proof, "required_output", lambda *args, **kwargs: "28.4.0")

    report = system_proof.run_system_proof(stack, build_images=build_images)  # type: ignore[arg-type]  # The double supplies only this proof path.
    assert report["status"] == "passed"
    assert stack.runtime_contents_checked
    assert stack.availability_checked
    expected_creations = (
        [
            ("create", "web", "migrate", "maintenance", "proxy-web"),
            ("create", "example-tests", "agent-e2e", "proxy-tests"),
        ]
        if candidate_image
        else [("create", "example-tests", "agent-e2e", "proxy-tests")]
    )
    assert stack.proof_service_creations == expected_creations
    if candidate_image:
        assert stack.builds == [("build", "example-tests", "agent-e2e", "proxy-tests")]
    elif build_images:
        assert stack.builds == [
            ("build", "--no-cache", "web", "migrate", "maintenance"),
            ("build", "example-tests", "agent-e2e", "proxy-tests"),
        ]
    else:
        assert stack.builds == []


def test_system_proof_stops_after_candidate_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Stack:
        project_name = "stproof-candidate-mismatch"
        candidate_image = "registry.example/startunnel@sha256:" + "a" * 64
        environment: dict[str, str] = {}

        def pull_candidate(self) -> None:
            events.append("pull")

        def verify_candidate_services(self) -> None:
            events.append("identity")
            raise isolation.LaneError("candidate image proof", "proxy-web image mismatch")

        def run(self, label: str, *args: str, **kwargs: object) -> None:
            del label, kwargs
            events.append("create" if args[0] == "create" else "long-step")

    with pytest.raises(isolation.LaneError, match="proxy-web image mismatch"):
        system_proof.run_system_proof(Stack())  # type: ignore[arg-type]

    assert events == ["pull", "create", "identity"]


def test_system_step_reports_fractional_seconds_even_when_it_fails() -> None:
    evidence: list[system_proof.SystemStep] = []

    def fail() -> None:
        raise RuntimeError("expected failure")

    with pytest.raises(RuntimeError, match="expected failure"):
        system_proof._recorded_step(evidence, "failing proof step", fail)

    record = evidence[0]
    assert record["status"] == "failed"
    assert isinstance(record["duration_seconds"], float)
    assert record["duration_seconds"] >= 0
    assert datetime.fromisoformat(str(record["started_at"])).tzinfo is not None
    assert datetime.fromisoformat(str(record["finished_at"])).tzinfo is not None


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="Compose rendering is checked on the host."
)
def test_maintenance_healthcheck_uses_the_local_liveness_file() -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    _assert_postgres_permission_tools_are_mounted(services)
    healthcheck = services["maintenance"]["healthcheck"]
    assert healthcheck == {
        "test": ["CMD", "/usr/local/bin/startunnel-maintenance-healthcheck"],
        "timeout": "1s",
        "interval": "1m0s",
        "start_interval": "2s",
        "retries": 3,
        "start_period": "10s",
    }

    command = healthcheck["test"]
    assert "/usr/local/bin/startunnel-entrypoint" not in command
    assert not any("python" in argument for argument in command)

    assert os.access(ROOT / "docker/app/maintenance-healthcheck.sh", os.X_OK)


def _run_web_healthcheck(
    command: str, directory: Path, *, base_url: str, exit_code: int
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    executable = directory / "wget"
    executable.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$WGET_ARGUMENTS"
exit "$WGET_EXIT_CODE"
"""
    )
    executable.chmod(0o755)
    output = directory / "wget-arguments"
    environment = os.environ | {
        "PATH": str(directory) + os.pathsep + os.defpath,
        "STARTUNNEL_BASE_URL": base_url,
        "WGET_ARGUMENTS": str(output),
        "WGET_EXIT_CODE": str(exit_code),
    }
    result = subprocess.run(
        ["/bin/sh", "-c", command.replace("$$", "$")],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, output.read_text().splitlines()


@pytest.mark.parametrize(
    ("base_url", "expected_host"),
    [
        ("http://startunnel.internal", "startunnel.internal"),
        ("https://startunnel.example", "startunnel.example"),
        ("http://startunnel.internal:8000", "startunnel.internal:8000"),
        ("https://startunnel.example:8443/", "startunnel.example:8443"),
        ("http://[::1]:8000/", "[::1]:8000"),
    ],
)
@pytest.mark.parametrize("exit_code", [0, 8])
def test_web_healthcheck_uses_the_origin_and_preserves_failure(
    executable_dir: Path, base_url: str, expected_host: str, exit_code: int
) -> None:
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    kind, command = services["web"]["healthcheck"]["test"]
    assert kind == "CMD-SHELL"

    result, arguments = _run_web_healthcheck(
        command, executable_dir, base_url=base_url, exit_code=exit_code
    )

    assert result.returncode == exit_code, result.stderr
    assert arguments == [
        "-q",
        "-O",
        "/dev/null",
        "-T",
        "3",
        "--header=Host: " + expected_host,
        "--header=X-Forwarded-Proto: https",
        "http://127.0.0.1:8000/health/ready",
    ]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI is required.")
@pytest.mark.parametrize(
    (
        "base_url",
        "allowed_hosts",
        "trusted_origins",
        "expected_url",
        "expected_hosts",
        "expected_trusted",
    ),
    [
        (
            "http://startunnel.internal:8000",
            "internal.example.test,.vpn.example.test",
            "https://*.browser.example.test",
            "http://startunnel.internal:8000",
            "internal.example.test,.vpn.example.test",
            "https://*.browser.example.test",
        ),
        (None, None, None, "https://startunnel.example.test", "startunnel.example.test", ""),
    ],
)
def test_production_compose_uses_operator_origin_or_https_default(
    executable_dir: Path,
    base_url: str | None,
    allowed_hosts: str | None,
    trusted_origins: str | None,
    expected_url: str,
    expected_hosts: str,
    expected_trusted: str,
) -> None:
    environment = os.environ.copy()
    environment.update(
        STARTUNNEL_DOMAIN="startunnel.example.test",
        STARTUNNEL_APP_IMAGE="startunnel:test",
    )
    environment.pop("STARTUNNEL_BASE_URL", None)
    environment.pop("STARTUNNEL_ALLOWED_HOSTS", None)
    environment.pop("STARTUNNEL_CSRF_TRUSTED_ORIGINS", None)
    if base_url is not None:
        environment["STARTUNNEL_BASE_URL"] = base_url
    if allowed_hosts is not None:
        environment["STARTUNNEL_ALLOWED_HOSTS"] = allowed_hosts
    if trusted_origins is not None:
        environment["STARTUNNEL_CSRF_TRUSTED_ORIGINS"] = trusted_origins
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "compose.yaml",
            "-f",
            "compose.prod.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    _assert_postgres_permission_tools_are_mounted(services)
    healthcheck = services["web"]["healthcheck"]["test"][-1]
    probe, arguments = _run_web_healthcheck(
        healthcheck, executable_dir, base_url=expected_url, exit_code=0
    )
    assert probe.returncode == 0, probe.stderr
    expected_host = "startunnel.internal:8000" if base_url else "startunnel.example.test"
    assert "--header=Host: " + expected_host in arguments
    for name in ("migrate", "web", "maintenance"):
        assert services[name]["environment"]["STARTUNNEL_BASE_URL"] == expected_url
        assert services[name]["environment"]["STARTUNNEL_ALLOWED_HOSTS"] == expected_hosts
        assert services[name]["environment"]["STARTUNNEL_CSRF_TRUSTED_ORIGINS"] == expected_trusted


def test_system_health_probe_outlasts_database_pool_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def unavailable(request: urllib.request.Request, timeout: float) -> object:
        calls.append((request.full_url, timeout))
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", HTTPMessage(), None)

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    exec(system_proof._web_health_script(503), {})
    assert calls == [("http://127.0.0.1:8000/health/ready", 10)]
    with pytest.raises(AssertionError):
        exec(system_proof._web_health_script(200), {})


@pytest.mark.parametrize(
    "lane,prefix",
    [
        ("canonical", "sttest-"),
        ("system", "stproof-"),
        ("load", "stload-"),
    ],
)
def test_generated_project_names_are_disposable(lane: str, prefix: str) -> None:
    project_name = isolation.generated_project_name(lane)

    assert project_name.startswith(prefix)
    assert isolation.validate_project_name(project_name) == project_name
    assert "startunnel" not in project_name


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([100_000_000, 102_000_000, 103_000_000], True),
        ([100_000_000, 106_000_000, 112_000_000], False),
        ([100_000_000, 120_000_000, 101_000_000], True),
    ],
)
def test_resource_memory_stability_uses_stabilized_samples(
    samples: list[int], expected: bool
) -> None:
    assert resource_runner.memory_is_stable(samples) is expected


def test_resource_parsers_count_only_health_check_execs() -> None:
    events = "\n".join(
        (
            json.dumps(
                {
                    "Action": "exec_create: pg_isready --quiet",
                    "Actor": {"Attributes": {"com.docker.compose.service": "postgres"}},
                }
            ),
            json.dumps(
                {
                    "Action": "exec_create: cat /sys/fs/cgroup/cpu.stat",
                    "Actor": {"Attributes": {"com.docker.compose.service": "web"}},
                }
            ),
            json.dumps(
                {
                    "Action": "exec_create: /usr/local/bin/startunnel-maintenance-healthcheck",
                    "Actor": {"Attributes": {"com.docker.compose.service": "maintenance"}},
                }
            ),
        )
    )

    assert resource_runner.count_health_probes(events) == {"postgres": 1, "maintenance": 1}


def test_resource_recurring_work_allows_one_event_per_minute_plus_jitter() -> None:
    idle = {
        "actual_seconds": 600,
        "health_probe_executions": {"postgres": 10, "web": 11},
        "maintenance_delta": {"reconciliation_count": 11},
    }

    assert resource_runner.recurring_work_is_bounded(idle)
    idle["maintenance_delta"] = {"reconciliation_count": 12}
    assert not resource_runner.recurring_work_is_bounded(idle)


@pytest.mark.parametrize(
    "project_name",
    [
        "startunnel",
        "sttest-production-proof",
        "sttest-stage-proof",
        "strelease-startunnel-proof",
        "project-without-approved-prefix",
        "sttest-UPPERCASE-proof",
    ],
)
def test_project_name_rejects_shared_or_production_names(project_name: str) -> None:
    with pytest.raises(isolation.IsolationError):
        isolation.validate_project_name(project_name)


@pytest.mark.parametrize("endpoint", ["ssh://production-host", "tcp://192.0.2.1:2376"])
def test_target_guard_rejects_remote_docker(endpoint: str) -> None:
    with pytest.raises(isolation.IsolationError, match="remote Docker endpoint"):
        isolation.assert_safe_target({"DOCKER_HOST": endpoint}, "sttest-focused-12345678")


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI is required.")
def test_isolated_compose_discards_ambient_deployment_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    source = dict(os.environ)
    source.update(
        STARTUNNEL_ENV="production",
        DJANGO_SETTINGS_MODULE="startunnel.settings.production",
        COMPOSE_FILE="/missing/compose.prod.yaml",
        COMPOSE_PROFILES="edge",
        COMPOSE_PROJECT_NAME="startunnel",
        DATABASE_URL="postgresql://unused@remote.example/live",
        DATABASE_ADMIN_URL="postgresql://unused@remote.example/live",
        TEST_DATABASE_URL="postgresql://unused@remote.example/live",
        STARTUNNEL_BASE_URL="https://remote.example",
    )
    with isolation.IsolatedComposeProject(
        "sttest-ambient-settings", source_environment=source
    ) as stack:
        model = json.loads(stack.capture("Render isolated model", "config", "--format", "json"))
        assert model["name"] == stack.project_name
        assert len(stack.compose_files()) == 2
        assert stack.profiles == ()
        web = model["services"]["web"]["environment"]
        assert web["DJANGO_SETTINGS_MODULE"] == "tests.browser_settings"
        assert web["STARTUNNEL_ENV"] == "development"
        assert web["DATABASE_URL"].endswith("@postgres:5432/startunnel")
        assert web["STARTUNNEL_BASE_URL"] == "http://web:8000"
        assert "caddy" not in model["services"]


def test_environment_allowlist_removes_service_and_provider_secrets() -> None:
    source = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "STARTUNNEL_LOAD_DURATION_SECONDS": "3",
        "STARTUNNEL_LOAD_ACTIVITY_READERS": "4",
        "DATABASE_URL": "postgresql://user:password@example.com/live",
        "STARTUNNEL_SECRET_KEY": "do-not-pass",
        "OPENAI_API_KEY": "do-not-pass",
        "OPENAI_MODEL": "do-not-pass",
    }

    assert isolation.safe_environment(source) == {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "STARTUNNEL_LOAD_ACTIVITY_READERS": "4",
        "STARTUNNEL_LOAD_DURATION_SECONDS": "3",
    }
    assert isolation.safe_environment(source, include_openai=True) == {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "STARTUNNEL_LOAD_ACTIVITY_READERS": "4",
        "STARTUNNEL_LOAD_DURATION_SECONDS": "3",
        "OPENAI_API_KEY": "do-not-pass",
        "OPENAI_MODEL": "do-not-pass",
    }


def test_remote_docker_endpoint_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolation, "required_output", lambda *args, **kwargs: '"ssh://host"')

    with pytest.raises(isolation.IsolationError, match="remote"):
        isolation.assert_local_docker({"PATH": "/safe/bin"})


def test_existing_project_is_not_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isolation, "required_output", lambda *args, **kwargs: "container-id")

    with pytest.raises(isolation.IsolationError, match="already has containers"):
        isolation.assert_project_unused("sttest-focused-12345678", {"PATH": "/safe/bin"})


def test_unlabelled_named_volume_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    project_name = "sttest-focused-12345678"
    expected_volume = f"{project_name}_postgres_data"

    def fake_output(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        label: str,
    ) -> str:
        del environment, label
        if command == ["docker", "volume", "ls", "--quiet"]:
            return expected_volume
        return ""

    monkeypatch.setattr(isolation, "required_output", fake_output)

    with pytest.raises(isolation.IsolationError, match="named data volume"):
        isolation.assert_project_unused(project_name, {"PATH": "/safe/bin"})


def test_unlabelled_named_network_is_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    project_name = "sttest-focused-12345678"
    expected_network = f"{project_name}_startunnel"

    def fake_output(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        label: str,
    ) -> str:
        del environment, label
        if command == ["docker", "network", "ls", "--format", "{{.Name}}"]:
            return expected_network
        return ""

    monkeypatch.setattr(isolation, "required_output", fake_output)

    with pytest.raises(isolation.IsolationError, match="named network"):
        isolation.assert_project_unused(project_name, {"PATH": "/safe/bin"})


def test_failed_lane_still_removes_disposable_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_process(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: int,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout_seconds, capture_output
        command_list = list(command)
        commands.append(command_list)
        return_code = 7 if "run" in command_list and "tests" in command_list else 0
        return subprocess.CompletedProcess(command_list, return_code, "", "")

    monkeypatch.setattr(isolation, "_run_process", fake_process)
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(
        isolation,
        "assert_project_unused",
        lambda project_name, environment: None,
    )

    result = lane_runner.main(["canonical", "--project-name", "sttest-focused-failure-12345678"])

    assert result == 1
    cleanup = next(command for command in commands if "down" in command)
    assert "down" in cleanup
    assert "--volumes" in cleanup
    assert "--remove-orphans" in cleanup
    assert cleanup[cleanup.index("--project-name") + 1] == "sttest-focused-failure-12345678"


@pytest.mark.parametrize(
    "lane,profiles,command,timeout_seconds",
    [
        ("canonical", (), ("run", "--rm", "tests"), 2_400),
        (
            "postgres",
            (),
            (
                "run",
                "--rm",
                "tests",
                "pdm",
                "run",
                "test-postgres",
            ),
            2_400,
        ),
        ("examples", ("examples",), ("run", "--rm", "example-tests"), 2_400),
        ("agents", ("agents",), ("run", "--rm", "agent-e2e"), 2_400),
        ("browser", ("browser",), ("run", "--rm", "browser-tests"), 4_800),
        (
            "live-agents",
            ("live-agents",),
            ("run", "--rm", "live-agent-e2e"),
            3_600,
        ),
    ],
)
def test_lane_dispatch_keeps_the_fixed_compose_contract(
    lane: str,
    profiles: tuple[str, ...],
    command: tuple[str, ...],
    timeout_seconds: int,
) -> None:
    specification = lane_runner.LANES[lane]

    assert specification.profiles == profiles
    assert specification.command == command
    assert specification.timeout_seconds == timeout_seconds


def test_lane_build_targets_include_active_buildable_dependencies() -> None:
    class FakeStack:
        def capture(self, *arguments: str, **kwargs: object) -> str:
            del arguments, kwargs
            return json.dumps(
                {
                    "services": {
                        "web": {"build": {}, "depends_on": {"migrate": {}}},
                        "migrate": {"build": {}, "depends_on": {"postgres": {}}},
                        "maintenance": {"build": {}, "depends_on": {"web": {}}},
                        "tests": {"build": {}, "depends_on": {"web": {}}},
                        "postgres": {"image": "postgres:18"},
                    }
                }
            )

    stack = FakeStack()
    assert lane_runner._lane_build_targets(stack, "tests") == (  # type: ignore[arg-type]  # The double supplies only Compose capture.
        "migrate",
        "tests",
        "web",
    )


def test_load_runner_uses_isolated_project_and_safe_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeStack:
        cleanup_complete = False

        environment = {"PATH": "/safe/bin"}

        def __init__(self, project_name: str, *, profiles: Sequence[str]) -> None:
            events.append(("init", (project_name, tuple(profiles))))

        def __enter__(self) -> FakeStack:
            events.append(("enter", None))
            return self

        def __exit__(self, *arguments: object) -> None:
            del arguments
            self.cleanup_complete = True
            events.append(("exit", None))

        def build_services(self, services: Sequence[str], **kwargs: object) -> dict[str, object]:
            events.append(("build", (tuple(services), kwargs)))
            return {"services": list(services)}

        def run(
            self,
            step: str,
            *arguments: str,
            timeout_seconds: int,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            del timeout_seconds
            events.append(("run", RunCall(step, arguments, dict(environment or {}))))
            if arguments == ("run", "--rm", "--no-deps", "load-profile"):
                assert environment is not None
                artifact_path = tmp_path / "artifacts"
                artifact_path.mkdir(parents=True, exist_ok=True)
                workload_path = artifact_path / environment["STARTUNNEL_LOAD_REPORT_FILENAME"]
                driver_path = workload_path.with_suffix(".driver.jsonl")
                driver_path.write_text("{}\n", encoding="utf-8")
                workload_path.write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "fixture_cleanup_complete": True,
                            "driver_observations_file": driver_path.name,
                        }
                    ),
                    encoding="utf-8",
                )

        def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
            del step, arguments, timeout_seconds
            return "image-reference"

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def safe_output(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        label: str,
    ) -> str:
        del environment, label
        if command[:2] == ["docker", "version"]:
            return "29.0.0"
        if command[:2] == ["git", "rev-parse"]:
            return "a" * 40
        if "{{.Os}}/{{.Architecture}}" in command:
            return "linux/amd64"
        identity_format = (
            '{{index .Config.Labels "net.startunnel.source"}}|{{.Os}}/{{.Architecture}}'
        )
        if identity_format in command:
            return "a" * 40 + "|linux/amd64"
        return "sha256:" + "a" * 64

    class CompleteObservations:
        failed = False

        def __init__(self, *args: object) -> None:
            del args

        def __enter__(self) -> CompleteObservations:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def summary(self) -> JsonObject:
            return {"samples": 1, "complete": True}

    monkeypatch.setattr(load_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(load_runner, "LoadObservations", CompleteObservations)
    monkeypatch.setattr(load_runner, "__file__", str(tmp_path / "scripts/run_full_load_profile.py"))
    monkeypatch.setattr(load_runner, "termination_signals", no_signals)
    monkeypatch.setattr(load_runner, "required_output", safe_output)

    result = load_runner.main(["--project-name", "stload-focused-12345678"])

    assert result == 0
    assert events[0] == ("init", ("stload-focused-12345678", ("load",)))
    assert events[-1] == ("exit", None)
    load_calls = [
        event
        for name, event in events
        if name == "run"
        and isinstance(event, RunCall)
        and event.arguments == ("run", "--rm", "--no-deps", "load-profile")
    ]
    assert len(load_calls) == 1
    load_environment = load_calls[0].environment
    assert load_environment["STARTUNNEL_LOAD_DOCKER_VERSION"] == "29.0.0"
    assert load_environment["STARTUNNEL_LOAD_BUILD_MODE"] == "built-current-run"
    assert load_environment["STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE"] == "18"
    assert load_environment["STARTUNNEL_WEB_WORKERS"] == "4"
    assert load_environment["STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT"] == "500"
    assert "OPENAI_API_KEY" not in load_environment


def test_load_runner_can_reuse_verified_current_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, tuple[str, ...]]] = []

    class FakeStack:
        cleanup_complete = False
        environment = {"PATH": "/safe/bin"}

        def __init__(self, project_name: str, *, profiles: Sequence[str]) -> None:
            del project_name, profiles

        def __enter__(self) -> FakeStack:
            return self

        def __exit__(self, *arguments: object) -> None:
            del arguments
            self.cleanup_complete = True

        def build_services(self, services: Sequence[str], **kwargs: object) -> dict[str, object]:
            del services, kwargs
            raise AssertionError("reuse mode must not build images")

        def run(
            self,
            step: str,
            *arguments: str,
            timeout_seconds: int,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            del timeout_seconds
            events.append((step, arguments))
            if arguments == ("run", "--rm", "--no-deps", "load-profile"):
                assert environment is not None
                assert environment["STARTUNNEL_LOAD_BUILD_MODE"] == "reused-current-images"
                artifact_path = tmp_path / "artifacts"
                artifact_path.mkdir(parents=True, exist_ok=True)
                workload_path = artifact_path / environment["STARTUNNEL_LOAD_REPORT_FILENAME"]
                driver_path = workload_path.with_suffix(".driver.jsonl")
                driver_path.write_text("{}\n", encoding="utf-8")
                workload_path.write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "fixture_cleanup_complete": True,
                            "driver_observations_file": driver_path.name,
                        }
                    ),
                    encoding="utf-8",
                )

        def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
            del step, timeout_seconds
            if arguments[:3] == ("config", "--format", "json"):
                return json.dumps(
                    {
                        "services": {
                            service: {"image": f"startunnel-{service}:current"}
                            for service in load_runner.SERVICES
                        }
                    }
                )
            return "image-reference"

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    class CompleteObservations:
        failed = False

        def __init__(self, *args: object) -> None:
            del args

        def __enter__(self) -> CompleteObservations:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def summary(self) -> JsonObject:
            return {"samples": 1, "complete": True}

    monkeypatch.setattr(load_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(load_runner, "LoadObservations", CompleteObservations)
    monkeypatch.setattr(load_runner, "__file__", str(tmp_path / "scripts/run_full_load_profile.py"))
    monkeypatch.setattr(load_runner, "termination_signals", no_signals)
    monkeypatch.setattr(
        load_runner,
        "required_output",
        lambda *args, **kwargs: (
            "29.0.0"
            if args[0][:2] == ["docker", "version"]
            else "a" * 40
            if args[0][:2] == ["git", "rev-parse"]
            else "linux/amd64"
            if "{{.Os}}/{{.Architecture}}" in args[0]
            else "a" * 40 + "|linux/amd64"
            if '{{index .Config.Labels "net.startunnel.source"}}|{{.Os}}/{{.Architecture}}'
            in args[0]
            else "sha256:" + "a" * 64
        ),
    )

    result = load_runner.main(
        [
            "--project-name",
            "stload-focused-reuse-12345678",
            "--reuse-current-images",
        ]
    )

    assert result == 0
    assert all(arguments[0] != "build" for _step, arguments in events)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI is required.")
def test_load_service_forwards_safe_provenance_fields() -> None:
    environment = os.environ.copy()
    expected = {
        "STARTUNNEL_LOAD_DOCKER_VERSION": "29.0.0",
        "STARTUNNEL_LOAD_IMAGE_DIGESTS": "web=sha256:fixture",
        "STARTUNNEL_LOAD_BUILD_MODE": "built-current-run",
    }
    environment.update(expected)
    environment.update(
        STARTUNNEL_WEB_WORKERS="3",
        STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE="12",
        DATABASE_ADMIN_URL="postgresql://fixture_admin:fixture@postgres/fixture",
    )
    model = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "--env-file",
                "/dev/null",
                "-f",
                "compose.yaml",
                "-f",
                "compose.test.yaml",
                "--profile",
                "load",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
        )
    )
    forwarded = model["services"]["load-profile"]["environment"]
    for name, value in expected.items():
        assert forwarded[name] == value
    assert forwarded["STARTUNNEL_LOAD_WEB_WORKERS"] == "3"
    assert forwarded["STARTUNNEL_LOAD_WEB_DATABASE_POOL_MAX_SIZE"] == "12"
    assert forwarded["DATABASE_URL"] == environment["DATABASE_ADMIN_URL"]


def test_system_report_is_written_only_after_volume_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeStack:
        cleanup_complete = False

        def __init__(
            self,
            project_name: str,
            *,
            profiles: Sequence[str],
            include_openai: bool,
        ) -> None:
            del project_name, include_openai
            assert tuple(profiles) == ("examples", "agents", "proxy-proof")
            events.append("init")

        def __enter__(self) -> FakeStack:
            events.append("enter")
            return self

        def __exit__(self, *arguments: object) -> None:
            del arguments
            self.cleanup_complete = True
            events.append("cleanup")

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def fake_proof(
        stack: FakeStack, *, build_images: bool, report: dict[str, object]
    ) -> dict[str, object]:
        del stack
        assert build_images is True
        events.append("proof")
        report.update(status="passed")
        return report

    def fake_write(report: Mapping[str, object]) -> Path:
        assert report["disposable_volumes_removed"] is True
        events.append("report")
        return ROOT / "artifacts/system-proof-test.json"

    monkeypatch.setattr(lane_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(lane_runner, "termination_signals", no_signals)
    monkeypatch.setattr(lane_runner, "run_system_proof", fake_proof)
    monkeypatch.setattr(lane_runner, "write_system_report", fake_write)

    result = lane_runner.main(["system", "--project-name", "stproof-focused-12345678"])

    assert result == 0
    assert events == ["init", "enter", "proof", "cleanup", "report"]


def test_system_failure_writes_partial_report_after_failed_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports: list[Mapping[str, object]] = []

    class FakeStack:
        cleanup_complete = False

        def __init__(
            self,
            project_name: str,
            *,
            profiles: Sequence[str],
            include_openai: bool,
        ) -> None:
            del project_name, profiles, include_openai

        def __enter__(self) -> FakeStack:
            return self

        def __exit__(self, *arguments: object) -> None:
            del arguments
            self.cleanup_complete = False

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def failing_proof(
        stack: FakeStack, *, build_images: bool, report: dict[str, object]
    ) -> JsonObject:
        del stack, build_images
        report.update(
            status="failed",
            steps=[{"name": "candidate image preflight", "status": "failed"}],
        )
        raise isolation.LaneError("candidate image proof", "proxy-web image mismatch")

    def write(report: Mapping[str, object]) -> Path:
        reports.append(report)
        return ROOT / "artifacts/system-proof-failed-test.json"

    monkeypatch.setattr(lane_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(lane_runner, "termination_signals", no_signals)
    monkeypatch.setattr(lane_runner, "run_system_proof", failing_proof)
    monkeypatch.setattr(lane_runner, "write_system_report", write)

    result = lane_runner.main(["system", "--project-name", "stproof-failed-12345678"])

    assert result == 1
    assert reports[0]["status"] == "failed"
    assert reports[0]["disposable_volumes_removed"] is False
    assert reports[0]["steps"] == [{"name": "candidate image preflight", "status": "failed"}]
    assert "Isolated lane failed" in capsys.readouterr().err


def test_reuse_current_images_is_only_valid_for_system(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = lane_runner.main(["canonical", "--reuse-current-images"])

    assert result == 2
    assert "valid only for the system lane" in capsys.readouterr().err


def test_system_reuse_mode_is_explicit_and_not_release_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proof_arguments: list[bool] = []
    written_reports: list[Mapping[str, object]] = []

    class FakeStack:
        cleanup_complete = False

        def __init__(
            self,
            project_name: str,
            *,
            profiles: Sequence[str],
            include_openai: bool,
        ) -> None:
            del project_name, profiles, include_openai

        def __enter__(self) -> FakeStack:
            return self

        def __exit__(self, *arguments: object) -> None:
            del arguments
            self.cleanup_complete = True

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def fake_proof(
        stack: FakeStack, *, build_images: bool, report: dict[str, object]
    ) -> dict[str, object]:
        del stack
        proof_arguments.append(build_images)
        report.update(
            {
                "status": "passed",
                "build_mode": "reused-current-images",
                "release_evidence_eligible": False,
            }
        )
        return report

    def fake_write(report: Mapping[str, object]) -> Path:
        written_reports.append(report)
        return ROOT / "artifacts/system-proof-test.json"

    monkeypatch.setattr(lane_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(lane_runner, "termination_signals", no_signals)
    monkeypatch.setattr(lane_runner, "run_system_proof", fake_proof)
    monkeypatch.setattr(lane_runner, "write_system_report", fake_write)

    result = lane_runner.main(
        [
            "system",
            "--project-name",
            "stproof-focused-reuse-12345678",
            "--reuse-current-images",
        ]
    )

    assert result == 0
    assert proof_arguments == [False]
    assert written_reports[0]["release_evidence_eligible"] is False
    output = capsys.readouterr().out
    assert "not fresh-build release evidence" in output


def test_reuse_mode_verifies_images_without_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, tuple[str, ...]]] = []
    model = {
        "services": {
            service: {"image": f"startunnel-{service}:current"}
            for service in system_proof.SYSTEM_SERVICES
        }
    }

    class FakeStack:
        environment = {"PATH": "/safe/bin"}

        def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
            del timeout_seconds
            commands.append((step, arguments))
            return json.dumps(model)

    monkeypatch.setattr(
        system_proof,
        "required_output",
        lambda *args, **kwargs: "sha256:" + "a" * 64,
    )

    system_proof._assert_reusable_images(FakeStack())  # type: ignore[arg-type]

    assert commands == [("reusable image model", ("config", "--format", "json"))]


def test_system_report_uses_bounded_host_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "{{.Architecture}}": "x86_64",
        "{{.MemTotal}}": "17179869184",
        "{{.OperatingSystem}}": "Docker Desktop",
        "{{.NCPU}}": "8",
    }

    def fake_output(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        label: str,
    ) -> str:
        del environment, label
        return values[command[-1]]

    monkeypatch.setattr(system_proof, "required_output", fake_output)
    stack = isolation.IsolatedComposeProject.__new__(isolation.IsolatedComposeProject)
    stack.environment = {"PATH": "/safe/bin"}

    assert system_proof._system_host_profile(stack) == {
        "architecture": "x86_64",
        "memory_bytes": 17179869184,
        "operating_system": "Docker Desktop",
        "processor_count": 8,
    }


@pytest.mark.parametrize(
    "unsafe_log",
    [
        "request st_" + "a" * 43,
        "request sta_" + "a" * 43,
        "provider sk-" + "A" * 24,
        "address \U0001f700",
        'field "receipt_handle"',
        'field "text_payload"',
        'field "json_payload"',
    ],
    ids=("startunnel-key", "admin-key", "provider-key", "glyph", "receipt", "text", "json"),
)
def test_system_log_inspection_rejects_sensitive_values(unsafe_log: str) -> None:
    with pytest.raises(isolation.LaneError, match="forbidden"):
        system_proof._assert_safe_runtime_logs(unsafe_log)


def test_system_log_inspection_accepts_safe_structured_metadata() -> None:
    system_proof._assert_safe_runtime_logs(
        '{"event":"request.complete","request_id":"safe-id","status":204}'
    )


def test_activity_worker_uses_tree_retry_contract_without_sensitive_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[tuple[str, object]] = []

    class FakeClient:
        def __init__(self, base_url: str, agent_key: str) -> None:
            events.append(("client", (base_url, agent_key)))
            self.role = agent_key
            self.read_count = 0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_arguments: object) -> None:
            return None

        async def create_tunnel(self, **arguments: object) -> dict[str, object]:
            events.append(("create", arguments))
            return {
                "tunnel": {"id": "private-tunnel-id", "address": "private-address"},
                "cycle": {"id": "private-cycle-id", "root": {"id": "private-root-id"}},
            }

        async def read_tree(self, **arguments: object) -> dict[str, object]:
            self.read_count += 1
            events.append(("read", (self.role, arguments)))
            if self.role == "private-receiver-key" and self.read_count == 2:
                raise StarTunnelError(
                    status_code=0,
                    code="connection_failed",
                    message="safe failure",
                    request_id=None,
                )
            nodes = [{"id": "private-root-id"}]
            if self.role == "private-sender-key" or self.read_count > 2:
                nodes.append({"id": "private-reply-id"})
            return {"nodes": nodes, "snapshot_cursor": "private-cursor"}

        async def me(self) -> dict[str, object]:
            events.append(("me", self.role))
            return {"id": "private-agent-id"}

        async def post_reply(self, **arguments: object) -> dict[str, object]:
            events.append(("reply", arguments))
            return {"message": {"id": "private-reply-id"}}

        async def close_cycle(self, **arguments: object) -> dict[str, object]:
            events.append(("close", arguments))
            return {"cycle": {"id": "private-cycle-id"}}

    monkeypatch.setattr(activity_worker, "StarTunnelClient", FakeClient)

    asyncio.run(
        activity_worker._run_public_proof(
            {
                "STARTUNNEL_SENDER_KEY": "private-sender-key",
                "STARTUNNEL_RECEIVER_KEY": "private-receiver-key",
            },
            "http://web:8000",
        )
    )

    output = capsys.readouterr().out.splitlines()
    assert output == [
        activity_worker.WAITING_MARKER,
        activity_worker.INTERRUPTED_MARKER,
        activity_worker.COMPLETE_MARKER,
    ]
    assert not any("private-" in line for line in output)
    reads = [value for name, value in events if name == "read"]
    assert reads[1][1]["snapshot_cursor"] == "private-cursor"  # type: ignore[index]
    replies = [value for name, value in events if name == "reply"]
    assert len(replies) == 2
    assert replies[0] == replies[1]


def test_activity_restart_orchestration_stops_and_recovers_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, tuple[str, ...]]] = []
    markers: list[str] = []
    log_results = iter(
        [
            system_proof.ACTIVITY_WAITING_MARKER,
            "\n".join(
                (
                    system_proof.ACTIVITY_WAITING_MARKER,
                    system_proof.ACTIVITY_INTERRUPTED_MARKER,
                    system_proof.ACTIVITY_COMPLETE_MARKER,
                )
            ),
        ]
    )

    class FakeStack:
        project_name = "stproof-focused-12345678"
        environment = {"PATH": "/safe/bin"}

        def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
            del timeout_seconds
            commands.append((step, arguments))
            return "safe-container-id"

        def run(
            self,
            step: str,
            *arguments: str,
            timeout_seconds: int,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            del timeout_seconds, environment
            commands.append((step, arguments))

    def record_marker(
        stack: object,
        container_name: str,
        marker: str,
        *,
        timeout_seconds: float,
    ) -> None:
        del stack, container_name, timeout_seconds
        markers.append(marker)

    monkeypatch.setattr(system_proof, "_wait_for_proof_marker", record_marker)
    monkeypatch.setattr(system_proof, "_proof_client_logs", lambda *args: next(log_results))
    monkeypatch.setattr(system_proof, "_proof_client_state", lambda *args: (True, 0))
    monkeypatch.setattr(system_proof, "_wait_for_proof_exit", lambda *args, **kwargs: 0)
    monkeypatch.setattr("scripts.system_proof.time.sleep", lambda seconds: None)

    system_proof._run_activity_restart_proof(FakeStack())  # type: ignore[arg-type]

    assert markers == [
        system_proof.ACTIVITY_WAITING_MARKER,
        system_proof.ACTIVITY_INTERRUPTED_MARKER,
        system_proof.ACTIVITY_COMPLETE_MARKER,
    ]
    module_index = commands[0][1].index("-m")
    assert commands[0][1][module_index - 1 : module_index + 2] == (
        "python",
        "-m",
        "scripts.prove_activity_restart",
    )
    stop = next(arguments for _step, arguments in commands if arguments[0] == "stop")
    assert stop == ("stop", "--timeout", "2", "web")
    restart = next(arguments for _step, arguments in commands if arguments[0] == "up")
    assert restart[:5] == ("up", "--detach", "--wait", "--wait-timeout", "300")
    assert restart[-1] == "web"


def test_activity_worker_and_runner_use_the_same_safe_markers() -> None:
    assert (
        system_proof.ACTIVITY_WAITING_MARKER,
        system_proof.ACTIVITY_INTERRUPTED_MARKER,
        system_proof.ACTIVITY_COMPLETE_MARKER,
        system_proof.ACTIVITY_FAILED_MARKER,
        system_proof.ACTIVITY_FAILURE_STAGE_PREFIX,
    ) == (
        activity_worker.WAITING_MARKER,
        activity_worker.INTERRUPTED_MARKER,
        activity_worker.COMPLETE_MARKER,
        activity_worker.FAILED_MARKER,
        activity_worker.FAILURE_STAGE_PREFIX,
    )


def test_activity_worker_refuses_a_nonisolated_api_target() -> None:
    assert activity_worker._validate_base_url("http://web:8000") == "http://web:8000"
    with pytest.raises(activity_worker.ProofError, match="isolated"):
        activity_worker._validate_base_url("https://startunnel.example")


def test_activity_worker_failure_output_redacts_exception_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "st_" + "a" * 43
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "http://web:8000")

    def fail_provisioning(path: Path) -> dict[str, str]:
        del path
        raise activity_worker.ProofError(sensitive_value)

    monkeypatch.setattr(activity_worker, "_provision", fail_provisioning)

    assert activity_worker.main() == 1
    output = capsys.readouterr().out
    assert output.splitlines() == [
        activity_worker.FAILED_MARKER,
        f"{activity_worker.FAILURE_STAGE_PREFIX}PROVISIONING",
    ]
    assert sensitive_value not in output


def test_system_report_records_every_used_service_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services: list[str] = []

    class FakeStack:
        environment = {"PATH": "/safe/bin"}

        def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
            del step, timeout_seconds
            services.append(arguments[-1])
            return "image-reference"

    monkeypatch.setattr(
        system_proof,
        "required_output",
        lambda *args, **kwargs: "sha256:" + "a" * 64,
    )

    image_ids = system_proof._system_image_ids(FakeStack())  # type: ignore[arg-type]

    assert set(image_ids) == {
        "postgres",
        "migrate",
        "web",
        "maintenance",
        "example-tests",
        "agent-e2e",
        "proxy-web",
        "proxy-caddy",
        "proxy-tests",
    }
    assert services == list(image_ids)


def test_external_workflow_actions_use_immutable_commits() -> None:
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for job in load_workflow(path.name)["jobs"].values():
            actions = [job.get("uses"), *(step.get("uses") for step in job.get("steps", []))]
            for action in actions:
                if action and not action.startswith("./"):
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", action), f"{path.name}: {action}"


def test_source_workflow_checks_service_lanes_and_cli_platforms() -> None:
    workflow = load_workflow("ci.yml")
    assert {"pull_request", "push", "workflow_dispatch"} <= workflow["on"].keys()
    jobs = workflow["jobs"]
    assert set(jobs["external-proofs"]["strategy"]["matrix"]["lane"]) == {"examples", "agents"}
    matrix = jobs["standalone-cli"]["strategy"]["matrix"]
    assert set(matrix["python"]) == {"3.10", "3.14"}
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest", "macos-latest"}
    for job in jobs.values():
        assert not job.get("secrets")
        for environment in [
            job.get("env", {}),
            *(step.get("env", {}) for step in job.get("steps", [])),
        ]:
            assert {"OPENAI_API_KEY", "OPENAI_MODEL"}.isdisjoint(environment)


def test_source_scan_failure_blocks_service_builds() -> None:
    jobs = load_workflow("ci.yml")["jobs"]
    steps = jobs["canonical"]["steps"]
    source = next(step for step in steps if step.get("run") == "pdm run security-release source")
    checks = next(step for step in steps if step.get("run") == "pdm run check")
    assert steps.index(source) < steps.index(checks)
    assert not source.get("continue-on-error")
    for job in ("postgres", "external-proofs", "browser"):
        assert jobs[job]["needs"] == "canonical"


@pytest.mark.parametrize(
    ("version", "script", "option"),
    [
        ("v0.1.0", "prove_admin_upgrade.py", "--baseline-record"),
        ("v0.2.0", "prove_published_upgrade.py", "--release-record"),
        ("v0.3.0", "prove_published_upgrade.py", "--release-record"),
    ],
)
def test_ci_checks_each_published_upgrade_with_its_own_record(
    version: str, script: str, option: str
) -> None:
    job = load_workflow("ci.yml")["jobs"]["postgres"]
    steps = [
        step
        for step in job["steps"]
        if script in step.get("run", "")
        and option in step["run"]
        and f"startunnel-{version}/" in step["run"]
    ]
    assert len(steps) == 1
    step = steps[0]
    assert step["if"] == "github.event_name != 'pull_request'"
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "GH_TOKEN" not in job.get("env", {})
    assert not step.get("continue-on-error")
    directory = f'"$RUNNER_TEMP/startunnel-{version}"'
    record = f'"$RUNNER_TEMP/startunnel-{version}/release.json"'
    assert step["run"].splitlines() == [
        f"git fetch --depth=1 origin refs/tags/{version}:refs/tags/{version}",
        f"gh release download {version} --pattern release.json --dir {directory}",
        f"pdm run python scripts/{script} {option} {record}",
    ]


def test_website_publication_is_separate_and_opt_in() -> None:
    workflow = load_workflow("website.yml")
    build = workflow["jobs"]["build"]
    deploy = workflow["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["if"] == (
        "github.event_name != 'pull_request' && github.ref == 'refs/heads/main' "
        "&& vars.PAGES_ENABLED == 'true'"
    )
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    artifact = next(
        step
        for step in build["steps"]
        if step.get("uses", "").split("@")[0] == "actions/upload-pages-artifact"
    )
    assert artifact["with"]["path"] == "website/dist"
    configuration = next(
        step
        for step in build["steps"]
        if step.get("uses", "").split("@")[0] == "actions/configure-pages"
    )
    assert (
        configuration["if"] == "github.event_name != 'pull_request' && vars.PAGES_ENABLED == 'true'"
    )


def test_manual_release_workflow_limits_provider_settings_to_live_proof_step() -> None:
    workflow = load_workflow("release-validation.yml")
    assert workflow["on"]["workflow_dispatch"]["inputs"]["run_live_agents"]["default"] == "false"
    live = workflow["jobs"]["live-agents"]
    assert live["if"] == "${{ inputs.run_live_agents }}"
    assert set(live["needs"]) == {"security", "system"}
    provider_steps = [
        (job_id, step)
        for job_id, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if {"OPENAI_API_KEY", "OPENAI_MODEL"} & step.get("env", {}).keys()
    ]
    assert len(provider_steps) == 1
    job_id, step = provider_steps[0]
    assert job_id == "live-agents"
    assert step["env"] == {
        "OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}",
        "OPENAI_MODEL": "${{ secrets.OPENAI_MODEL }}",
    }
    assert all(
        {"OPENAI_API_KEY", "OPENAI_MODEL"}.isdisjoint(job.get("env", {}))
        for job in workflow["jobs"].values()
    )


@pytest.mark.parametrize(
    ("baseline_ref", "candidate_cpu", "expected_status"),
    [(None, 0.1, 0), ("main", 0.1, 0), (None, 2.0, 1)],
)
def test_resource_measurement_runs_only_requested_stacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    baseline_ref: str | None,
    candidate_cpu: float,
    expected_status: int,
) -> None:
    measured: list[str] = []
    baseline_events: list[str] = []
    reports: list[resource_runner.ResourceReport] = []

    @contextmanager
    def baseline_checkout(ref: str) -> Iterator[Path]:
        assert ref == baseline_ref
        baseline_events.append("open")
        try:
            yield tmp_path / "baseline"
        finally:
            baseline_events.append("close")

    def measure(root: Path, project: str, label: str, **kwargs: object) -> JsonObject:
        measured.append(label)
        assert root == (tmp_path / "baseline" if label == "baseline" else resource_runner.ROOT)
        if label == "candidate" and baseline_ref:
            assert baseline_events == ["open", "close"]
        return {
            "idle": {
                "cpu_percent_of_one_core": candidate_cpu,
                "actual_seconds": 600,
                "health_probe_executions": {},
                "maintenance_delta": None,
            },
            "memory": {"stable": True},
            "deletion": {"passed": True},
            "ready_seconds": 5,
        }

    def report(document: resource_runner.ResourceReport) -> Path:
        reports.append(document)
        return tmp_path / "report.json"

    monkeypatch.setattr(resource_runner, "assert_local_docker", lambda environment: None)
    monkeypatch.setattr(
        "scripts.run_resource_measurement.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "synthetic-version\n"),
    )
    monkeypatch.setattr(resource_runner, "_baseline_worktree", baseline_checkout)
    monkeypatch.setattr(resource_runner, "_measure_stack", measure)
    monkeypatch.setattr(resource_runner, "_write_report", report)
    previous_umask = os.umask(0o077)
    try:
        arguments = ["--baseline-ref", baseline_ref] if baseline_ref else []
        assert resource_runner.main(arguments) == expected_status
    finally:
        os.umask(previous_umask)
    assert measured == (["baseline", "candidate"] if baseline_ref else ["candidate"])
    assert reports[0]["method"]["baseline_ref"] == baseline_ref
    assert (reports[0]["baseline"] is not None) == bool(baseline_ref)
    assert reports[0]["acceptance"]["candidate_idle_cpu_below_one_percent"] == (candidate_cpu < 1.0)


def test_host_proof_scripts_parse_on_runner_python() -> None:
    for path in (ROOT / "scripts").glob("*.py"):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 12))
