"""Protect release-lane isolation and provider-secret boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from examples.python.startunnel_client import StarTunnelError
from scripts import compose_isolation as isolation
from scripts import prove_activity_restart as activity_worker
from scripts import run_full_load_profile as load_runner
from scripts import run_isolated_test_lane as lane_runner
from scripts import run_resource_measurement as resource_runner
from scripts import system_proof

ROOT = Path(__file__).resolve().parents[2]


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
    healthcheck = json.loads(result.stdout)["services"]["maintenance"]["healthcheck"]
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

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_rule = (
        "COPY --chmod=0555 docker/app/maintenance-healthcheck.sh "
        "/usr/local/bin/startunnel-maintenance-healthcheck"
    )
    production_runtime = dockerfile.split("FROM python-base AS runtime", 1)[1].split(
        "FROM development-dependencies AS development-runtime", 1
    )[0]
    development_runtime = dockerfile.split(
        "FROM development-dependencies AS development-runtime", 1
    )[1].split("FROM development-runtime AS development", 1)[0]
    assert copy_rule in production_runtime
    assert copy_rule in development_runtime
    assert os.access(ROOT / "docker/app/maintenance-healthcheck.sh", os.X_OK)


def test_system_health_probe_outlasts_database_pool_acquisition() -> None:
    proof = system_proof._web_health_script(503)

    assert "timeout=10" in proof
    assert "status == 503" in proof


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


def test_resource_cpu_percent_aggregates_all_containers() -> None:
    result = resource_runner.aggregate_one_core_percent(
        {"web": 1_000_000, "postgres": 2_000_000},
        {"web": 1_600_000, "postgres": 2_400_000},
        10,
    )

    assert result == 10.0


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

    assert resource_runner.parse_cpu_stat("usage_usec 123\nuser_usec 100\n") == 123
    assert resource_runner.parse_memory_stat_anon("anon 456\nfile 100\n") == 456
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


@pytest.mark.parametrize(
    "environment",
    [
        {"STARTUNNEL_ENV": "production"},
        {"DJANGO_SETTINGS_MODULE": "startunnel.settings.production"},
        {"COMPOSE_FILE": "compose.yaml:compose.prod.yaml"},
        {"COMPOSE_PROFILES": "edge"},
        {"COMPOSE_PROJECT_NAME": "startunnel"},
        {"DOCKER_HOST": "ssh://production-host"},
        {"DATABASE_URL": "postgresql://service@example.com/startunnel"},
        {"STARTUNNEL_BASE_URL": "https://startunnel.example"},
    ],
)
def test_target_guard_rejects_production_like_environment(
    environment: Mapping[str, str],
) -> None:
    with pytest.raises(isolation.IsolationError):
        isolation.assert_safe_target(environment, "sttest-focused-12345678")


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
        ("canonical", (), ("run", "--build", "--rm", "tests"), 2_400),
        (
            "postgres",
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
        ("examples", ("examples",), ("run", "--build", "--rm", "example-tests"), 2_400),
        ("agents", ("agents",), ("run", "--build", "--rm", "agent-e2e"), 2_400),
        ("browser", ("browser",), ("run", "--build", "--rm", "browser-tests"), 4_800),
        (
            "live-agents",
            ("live-agents",),
            ("run", "--build", "--rm", "live-agent-e2e"),
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


def test_load_runner_uses_isolated_project_and_safe_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakeStack:
        environment = {"PATH": "/safe/bin"}

        def __init__(self, project_name: str, *, profiles: Sequence[str]) -> None:
            events.append(("init", (project_name, tuple(profiles))))

        def __enter__(self) -> FakeStack:
            events.append(("enter", None))
            return self

        def __exit__(self, *arguments: Any) -> None:
            del arguments
            events.append(("exit", None))

        def run(
            self,
            step: str,
            *arguments: str,
            timeout_seconds: int,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            del timeout_seconds
            events.append(("run", (step, arguments, dict(environment or {}))))

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
        return "sha256:" + "a" * 64

    monkeypatch.setattr(load_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(load_runner, "termination_signals", no_signals)
    monkeypatch.setattr(load_runner, "required_output", safe_output)

    result = load_runner.main(["--project-name", "stload-focused-12345678", "--seed", "42"])

    assert result == 0
    assert events[0] == ("init", ("stload-focused-12345678", ("load",)))
    assert events[-1] == ("exit", None)
    load_calls = [event for name, event in events if name == "run" and "load profile" in event[0]]
    assert len(load_calls) == 1
    load_environment = load_calls[0][2]
    assert load_environment["STARTUNNEL_LOAD_SEED"] == "42"
    assert load_environment["STARTUNNEL_LOAD_DOCKER_VERSION"] == "29.0.0"
    assert load_environment["STARTUNNEL_LOAD_BUILD_MODE"] == "built-current-run"
    assert load_environment["STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE"] == "6"
    assert load_environment["STARTUNNEL_WEB_WORKERS"] == "12"
    assert "OPENAI_API_KEY" not in load_environment


def test_load_runner_can_reuse_verified_current_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, tuple[str, ...]]] = []

    class FakeStack:
        environment = {"PATH": "/safe/bin"}

        def __init__(self, project_name: str, *, profiles: Sequence[str]) -> None:
            del project_name, profiles

        def __enter__(self) -> FakeStack:
            return self

        def __exit__(self, *arguments: Any) -> None:
            del arguments

        def run(
            self,
            step: str,
            *arguments: str,
            timeout_seconds: int,
            environment: Mapping[str, str] | None = None,
        ) -> None:
            del timeout_seconds
            events.append((step, arguments))
            if step == "Run fixed five-minute load profile":
                assert environment is not None
                assert environment["STARTUNNEL_LOAD_BUILD_MODE"] == "reused-current-images"

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

    monkeypatch.setattr(load_runner, "IsolatedComposeProject", FakeStack)
    monkeypatch.setattr(load_runner, "termination_signals", no_signals)
    monkeypatch.setattr(
        load_runner,
        "required_output",
        lambda *args, **kwargs: (
            "29.0.0" if args[0][:2] == ["docker", "version"] else "sha256:" + "a" * 64
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
    assert all(step != "Build load images" for step, _arguments in events)


def test_load_service_forwards_safe_provenance_fields() -> None:
    compose = (ROOT / "compose.test.yaml").read_text(encoding="utf-8")

    for name in (
        "STARTUNNEL_LOAD_DOCKER_VERSION",
        "STARTUNNEL_LOAD_IMAGE_DIGESTS",
        "STARTUNNEL_LOAD_BUILD_MODE",
        "STARTUNNEL_LOAD_SEED",
    ):
        assert f"{name}: ${{{name}:-" in compose
    assert (
        "STARTUNNEL_LOAD_WEB_DATABASE_POOL_MAX_SIZE: ${STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE:-18}"
    ) in compose
    assert "STARTUNNEL_LOAD_WEB_WORKERS: ${STARTUNNEL_WEB_WORKERS:-4}" in compose
    assert (
        "DATABASE_URL: ${DATABASE_ADMIN_URL:-postgresql://startunnel_admin:"
        "startunnel_admin@postgres:5432/startunnel}"
    ) in compose
    assert "Public API\n      # traffic still reaches the web service" in compose


def test_security_lane_enforces_actionable_bandit_findings() -> None:
    runner = (ROOT / "scripts/run_security_release.py").read_text(encoding="utf-8")
    docs_renderer = (ROOT / "src/site_app/docs.py").read_text(encoding="utf-8")

    assert '"--severity-level",\n                "medium"' in runner
    assert '"--confidence-level",\n                "medium"' in runner
    assert "# nosec B308, B703" in docs_renderer


def test_system_report_is_written_only_after_volume_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeStack:
        def __init__(
            self,
            project_name: str,
            *,
            profiles: Sequence[str],
            include_openai: bool,
        ) -> None:
            del project_name, profiles, include_openai
            events.append("init")

        def __enter__(self) -> FakeStack:
            events.append("enter")
            return self

        def __exit__(self, *arguments: Any) -> None:
            del arguments
            events.append("cleanup")

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def fake_proof(stack: FakeStack, *, build_images: bool) -> dict[str, object]:
        del stack
        assert build_images is True
        events.append("proof")
        return {"status": "passed"}

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

        def __exit__(self, *arguments: Any) -> None:
            del arguments

    @contextmanager
    def no_signals() -> Iterator[None]:
        yield

    def fake_proof(stack: FakeStack, *, build_images: bool) -> dict[str, object]:
        del stack
        proof_arguments.append(build_images)
        return {
            "status": "passed",
            "build_mode": "reused-current-images",
            "release_evidence_eligible": False,
        }

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
        "provider sk-" + "A" * 24,
        "address \U0001f700",
        'field "receipt_handle"',
        'field "text_payload"',
        'field "json_payload"',
    ],
    ids=("startunnel-key", "provider-key", "glyph", "receipt", "text", "json"),
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
    stop = next(arguments for step, arguments in commands if step.startswith("Stop web"))
    assert stop == ("stop", "--timeout", "2", "web")
    restart = next(arguments for step, arguments in commands if step.startswith("Restart web"))
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


def test_system_restore_proof_uses_one_coordinated_backup() -> None:
    backup = (ROOT / "scripts/backup-startunnel.sh").read_text(encoding="utf-8")
    restore = (ROOT / "scripts/verify-startunnel-restore.sh").read_text(encoding="utf-8")

    assert "pg_dump" in backup
    assert "manifest.sha256" in backup
    assert "pg_restore" in restore
    assert "runtime-grants.sql" in restore
    assert "CREATE TABLE startunnel_forbidden_schema" in restore
    assert "DELETE FROM tunnels_message" in restore
    assert "INSERT INTO django_session" in restore
    assert "UPDATE django_session" in restore
    assert "DELETE FROM django_session" in restore
    runtime_restrictions = system_proof._runtime_restriction_script()
    assert "CREATE DATABASE startunnel_forbidden_system_database" in runtime_restrictions
    assert "DELETE FROM tunnels_message" in runtime_restrictions
    runtime_session_dml = system_proof._runtime_session_dml_script()
    assert "INSERT INTO django_session" in runtime_session_dml
    assert "UPDATE django_session" in runtime_session_dml
    assert "DELETE FROM django_session" in runtime_session_dml


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
        "web",
        "maintenance",
        "example-tests",
        "agent-e2e",
        "browser-tests",
    }
    assert services == list(image_ids)


def test_pull_request_workflow_has_all_non_live_release_lanes() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "pdm install --frozen-lockfile --no-self" in workflow
    assert "pdm run check" in workflow
    for lane in ("postgres", "examples", "agents", "browser"):
        assert f"run_isolated_test_lane.py {lane}" in workflow or f"- {lane}" in workflow
    assert "OPENAI" not in workflow
    assert "live-agent" not in workflow
    assert "github.run_id" in workflow
    assert "github.run_attempt" in workflow
    assert workflow.count("--project-name") == workflow.count("scripts/run_isolated_test_lane.py")
    assert "docker compose" not in workflow


def test_website_publication_is_separate_and_opt_in() -> None:
    workflow = (ROOT / ".github/workflows/website.yml").read_text(encoding="utf-8")
    build_job, deploy_job = workflow.split("  deploy:\n", maxsplit=1)

    assert "pdm install --prod --frozen-lockfile --no-self" in build_job
    assert "pdm run python website/build.py" in build_job
    assert "steps.pages.outputs.base_path" in build_job
    assert "path: website/dist" in build_job
    assert "github.server_url" in build_job
    assert "needs: build" in deploy_job
    assert "github.event_name != 'pull_request'" in deploy_job
    assert "github.ref == 'refs/heads/main'" in deploy_job
    assert "vars.PAGES_ENABLED == 'true'" in deploy_job
    assert "pages: write" in deploy_job
    assert "id-token: write" in deploy_job
    assert "name: github-pages" in deploy_job
    assert "docker" not in workflow


def test_manual_release_workflow_limits_provider_settings_to_live_proof_step() -> None:
    workflow = (ROOT / ".github/workflows/release-validation.yml").read_text(encoding="utf-8")
    live_job = workflow.split("  live-agents:\n", maxsplit=1)[1]
    live_test_step = live_job.split("      - name: Run the isolated opt-in proof\n", maxsplit=1)[1]

    assert "pdm run security-release" in workflow
    assert "run_isolated_test_lane.py system" in workflow
    assert "run_full_load_profile.py" in workflow
    assert "run_isolated_test_lane.py live-agents" in live_job
    assert "--allow-openai-cost" in live_job
    assert "OPENAI_API_KEY" not in workflow.split("  live-agents:\n", maxsplit=1)[0]
    assert "OPENAI_MODEL" not in workflow.split("  live-agents:\n", maxsplit=1)[0]
    assert "OPENAI_API_KEY" in live_job
    assert "OPENAI_MODEL" in live_job
    assert (
        "OPENAI_API_KEY"
        not in live_job.split("      - name: Run the isolated opt-in proof\n", maxsplit=1)[0]
    )
    assert live_test_step.count("          OPENAI_API_KEY:") == 1
    assert live_test_step.count("          OPENAI_MODEL:") == 1
    assert "github.run_id" in workflow
    assert "github.run_attempt" in workflow
    runner_count = workflow.count("scripts/run_isolated_test_lane.py") + workflow.count(
        "scripts/run_full_load_profile.py"
    )
    assert workflow.count("--project-name") == runner_count
    assert "docker compose" not in workflow
