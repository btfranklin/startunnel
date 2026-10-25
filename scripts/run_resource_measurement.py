"""Measure runtime resources in disposable Compose stacks."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import (
        json_boolean,
        json_integer,
        json_number,
        json_object,
        json_string,
    )
else:
    from json_data import (
        json_boolean,
        json_integer,
        json_number,
        json_object,
        json_string,
    )


if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        IsolationError,
        LaneError,
        assert_local_docker,
        assert_project_unused,
        assert_safe_target,
        generated_project_name,
        safe_environment,
        termination_signals,
        validate_project_name,
    )
    from scripts.docker_stats import DockerStats
else:
    from compose_isolation import (
        IsolationError,
        LaneError,
        assert_local_docker,
        assert_project_unused,
        assert_safe_target,
        generated_project_name,
        safe_environment,
        termination_signals,
        validate_project_name,
    )
    from docker_stats import DockerStats

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SERVICES = {"postgres", "web", "maintenance"}
HEALTH_MARKERS = (
    "health/ready",
    "pg_isready",
    "startunnel-maintenance-healthcheck",
)


@dataclass(frozen=True, slots=True)
class Boundary:
    captured_at: str
    cpu_usage_ns: dict[str, int]
    cpu_captured_monotonic_seconds: dict[str, float]
    memory_bytes: dict[str, int]
    anonymous_rss_bytes: dict[str, int]


class BoundaryData(TypedDict):
    captured_at: str
    cpu_usage_ns: dict[str, int]
    cpu_captured_monotonic_seconds: dict[str, float]
    memory_bytes: dict[str, int]
    anonymous_rss_bytes: dict[str, int]


def _boundary_data(boundary: Boundary) -> BoundaryData:
    return {
        "captured_at": boundary.captured_at,
        "cpu_usage_ns": boundary.cpu_usage_ns,
        "cpu_captured_monotonic_seconds": boundary.cpu_captured_monotonic_seconds,
        "memory_bytes": boundary.memory_bytes,
        "anonymous_rss_bytes": boundary.anonymous_rss_bytes,
    }


class ActivityCounters(TypedDict):
    captured_at: str
    database_transactions: int
    maintenance: dict[str, int] | None


class MemorySample(TypedDict):
    captured_at: str
    cgroup_bytes: dict[str, int]
    anonymous_rss_bytes: dict[str, int]


class DeletionProof(TypedDict):
    passed: bool
    elapsed_seconds: float
    target_seconds: int
    message_count: int


class CycleMeasurement(TypedDict):
    cycle: int
    before: BoundaryData
    after_load: BoundaryData
    after_settle: BoundaryData
    settle_memory_samples: list[MemorySample]


class IdleMeasurement(TypedDict):
    requested_seconds: int
    actual_seconds: float
    start: BoundaryData
    end: BoundaryData
    cpu_percent_of_one_core: float
    cpu_percent_by_service: dict[str, float]
    database_transaction_delta: int
    maintenance_delta: dict[str, int] | None
    health_probe_executions: dict[str, int]
    health_probe_scope: str
    memory_samples: list[MemorySample]
    activity_counter_start: ActivityCounters
    activity_counter_end: ActivityCounters
    database_counter_scope: str


class MemoryMeasurement(TypedDict):
    stabilized_totals_bytes: list[int]
    sampled_peak_total_bytes: int
    stable: bool
    stabilized_service_bytes: dict[str, list[int]]
    criterion: str


class StackMeasurement(TypedDict):
    source: str
    project_name: str
    build_seconds: float
    ready_seconds: float
    idle: IdleMeasurement
    cycles: list[CycleMeasurement]
    memory: MemoryMeasurement
    deletion: DeletionProof | None


class MeasurementOptions(TypedDict):
    warmup_seconds: int
    idle_seconds: int
    sample_seconds: int
    cycles: int
    settle_seconds: int
    load_operations: int
    deletion_timeout_seconds: int


class MeasurementMethod(MeasurementOptions):
    baseline_ref: str | None
    candidate_ref: str
    candidate_commit: str
    compose_file: str
    scope: str
    web_database_pool_max_size: int
    command: str
    production_process_mode: str
    external_clients: bool
    cgroup_cpu_reads: str
    memory_sample_seconds: int


class MeasurementHost(TypedDict):
    system: str
    release: str
    machine: str
    processor_count: int | None
    docker_server_version: str


class ResourceAcceptance(TypedDict):
    candidate_idle_cpu_below_one_percent: bool
    candidate_memory_stable: bool
    candidate_deletion_within_target: bool
    candidate_recurring_work_bounded: bool
    candidate_ready_within_two_minutes: bool
    physical_wakeups_or_watts_measured: bool


class ResourceReport(TypedDict):
    schema: str
    created_at: str
    host: MeasurementHost
    method: MeasurementMethod
    baseline: StackMeasurement | None
    candidate: StackMeasurement
    acceptance: ResourceAcceptance


def cpu_percentages(start: Boundary, end: Boundary) -> dict[str, float]:
    """Return each container's CPU rate over its own measured interval."""

    if start.cpu_usage_ns.keys() != end.cpu_usage_ns.keys() or not start.cpu_usage_ns:
        raise ValueError("The measured container set changed.")
    percentages = {}
    for service, initial in start.cpu_usage_ns.items():
        duration = (
            end.cpu_captured_monotonic_seconds[service]
            - start.cpu_captured_monotonic_seconds[service]
        )
        usage = end.cpu_usage_ns[service] - initial
        if duration <= 0 or usage < 0:
            raise ValueError("The resource clock or CPU counter reset during measurement.")
        percentages[service] = usage / (duration * 1_000_000_000) * 100
    return percentages


def memory_is_stable(samples: Sequence[int]) -> bool:
    """Allow normal allocator noise but reject continued material growth."""

    if len(samples) < 3:
        return True
    allowance = max(5 * 1024 * 1024, int(samples[0] * 0.10))
    materially_rising = all(later > earlier + 1024 * 1024 for earlier, later in pairwise(samples))
    return not materially_rising and samples[-1] <= samples[0] + allowance


def count_health_probes(events: str) -> dict[str, int]:
    """Count Compose health-check exec events without counting inspection execs."""

    counts: dict[str, int] = {}
    for line in events.splitlines():
        try:
            event = json_object(json.loads(line))
        except ValueError:
            continue
        action = str(event.get("Action", ""))
        if "exec_create:" not in action or not any(marker in action for marker in HEALTH_MARKERS):
            continue
        attributes = json_object(json_object(event.get("Actor", {})).get("Attributes", {}))
        service = attributes.get("com.docker.compose.service")
        if isinstance(service, str):
            counts[service] = counts.get(service, 0) + 1
    return counts


def recurring_work_is_bounded(idle: Mapping[str, object]) -> bool:
    """Accept at most one scheduled probe or reconciliation per minute plus jitter."""

    allowed = math.ceil(float(json_number(idle["actual_seconds"])) / 60) + 1
    health = json_object(idle["health_probe_executions"])
    maintenance = (
        None if idle["maintenance_delta"] is None else json_object(idle["maintenance_delta"])
    )
    health_is_bounded = all(json_integer(count) <= allowed for count in health.values())
    reconciliation_is_bounded = (
        maintenance is None or json_integer(maintenance["reconciliation_count"]) <= allowed
    )
    return health_is_bounded and reconciliation_is_bounded


class MeasurementStack:
    """Own one disposable stack rooted at a specified source checkout."""

    def __init__(self, root: Path, project_name: str, label: str) -> None:
        self.root = root
        self.project_name = validate_project_name(project_name)
        self.label = label
        self.environment = safe_environment()
        self.environment.update(
            {
                "STARTUNNEL_APP_IMAGE": f"startunnel-resource-{label}:{project_name}",
                "STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE": "18",
                "STARTUNNEL_MAINTENANCE_DATABASE_POOL_MAX_SIZE": "4",
                "STARTUNNEL_WEB_WORKERS": "1",
            }
        )
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._env_file: Path | None = None
        self.identifiers: dict[str, str] = {}
        self.started: dict[str, tuple[str, int]] = {}
        self.stats: DockerStats

    def __enter__(self) -> MeasurementStack:
        assert_safe_target(self.environment, self.project_name)
        endpoint = assert_local_docker(self.environment)
        if not self.environment.get("DOCKER_CONTEXT"):
            endpoint = self.environment.get("DOCKER_HOST") or endpoint
        self.stats = DockerStats(endpoint)
        assert_project_unused(self.project_name, self.environment)
        self._temporary = tempfile.TemporaryDirectory(prefix="startunnel-resource-")
        self._env_file = Path(self._temporary.name) / "empty.env"
        self._env_file.write_text("# Intentionally empty.\n", encoding="utf-8")
        self._env_file.chmod(0o600)
        return self

    def __exit__(self, *arguments: object) -> None:
        del arguments
        try:
            self.run(
                "Remove disposable resource stack",
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "30",
                timeout_seconds=180,
                capture=True,
                allow_failure=True,
            )
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()

    def command(self, *arguments: str) -> list[str]:
        if self._env_file is None:
            raise IsolationError("The resource stack is not active.")
        return [
            "docker",
            "compose",
            "--env-file",
            str(self._env_file),
            "-f",
            str(self.root / "compose.yaml"),
            "--project-name",
            self.project_name,
            *arguments,
        ]

    def run(
        self,
        step: str,
        *arguments: str,
        timeout_seconds: int = 60,
        capture: bool = False,
        allow_failure: bool = False,
    ) -> str:
        if not capture:
            print(f"[{self.label}: {step}]", flush=True)
        try:
            result = subprocess.run(  # nosec B603
                self.command(*arguments),
                cwd=self.root,
                env=self.environment,
                text=True,
                capture_output=capture,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneError(step, f"{step} did not finish inside its time limit.") from error
        if result.returncode and not allow_failure:
            detail = result.stderr.strip() if capture else f"exit code {result.returncode}"
            raise LaneError(step, f"{step} failed: {detail}")
        return result.stdout.strip() if result.stdout is not None else ""

    def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
        return self.run(step, *arguments, timeout_seconds=timeout_seconds, capture=True)

    def container_ids(self) -> dict[str, str]:
        raw = self.capture(
            "inspect running containers", "ps", "--status", "running", "--format", "{{json .}}"
        )
        rows = [json_object(json.loads(line)) for line in raw.splitlines() if line.strip()]
        counts = Counter(json_string(row["Service"]) for row in rows)
        return {
            json_string(row["Service"])
            if counts[json_string(row["Service"])] == 1
            else json_string(row["Service"]) + ":" + json_string(row["Name"]): json_string(
                row["ID"]
            )
            for row in rows
        }

    def cache_containers(self) -> None:
        self.identifiers = self.container_ids()
        if not {name.split(":")[0] for name in self.identifiers} >= REQUIRED_SERVICES:
            raise LaneError("runtime containers", "The reference backend is missing a service.")
        for service, identifier in self.identifiers.items():
            state = self.stats.get(f"/containers/{identifier}/json")
            if not json_boolean(json_object(state["State"])["Running"]):
                raise LaneError("runtime containers", f"The {service} container is not running.")
            self.started[service] = (
                json_string(json_object(state["State"])["StartedAt"]),
                json_integer(state["RestartCount"]),
            )

    def sql(self, statement: str) -> str:
        return self.capture(
            "query resource counters",
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            "startunnel_admin",
            "--dbname",
            "startunnel",
            "--tuples-only",
            "--no-align",
            "--quiet",
            "--command",
            statement,
        )


def _boundary(stack: MeasurementStack) -> Boundary:
    cpu: dict[str, int] = {}
    captured: dict[str, float] = {}
    memory: dict[str, int] = {}
    rss: dict[str, int] = {}
    for service, identifier in stack.identifiers.items():
        before = time.monotonic()
        stats = stack.stats.get(f"/containers/{identifier}/stats?stream=false&one-shot=true")
        captured[service] = (before + time.monotonic()) / 2
        cpu[service] = json_integer(
            json_object(json_object(stats["cpu_stats"])["cpu_usage"])["total_usage"]
        )
        memory[service] = json_integer(json_object(stats["memory_stats"])["usage"])
        rss[service] = json_integer(
            json_object(json_object(stats["memory_stats"])["stats"])["anon"]
        )
        state = stack.stats.get(f"/containers/{identifier}/json")
        if (
            not json_boolean(json_object(state["State"])["Running"])
            or (
                json_string(json_object(state["State"])["StartedAt"]),
                json_integer(state["RestartCount"]),
            )
            != stack.started[service]
        ):
            raise LaneError("runtime containers", f"The {service} container stopped or restarted.")
    return Boundary(datetime.now(UTC).isoformat(), cpu, captured, memory, rss)


def _activity_counters(stack: MeasurementStack) -> ActivityCounters:
    transactions = stack.sql(
        "SELECT xact_commit + xact_rollback FROM pg_stat_database "
        "WHERE datname = current_database();"
    )
    maintenance: dict[str, int] | None = None
    exists = stack.sql("SELECT to_regclass('public.core_maintenancestate') IS NOT NULL;")
    if exists == "t":
        raw = stack.sql(
            "SELECT reconciliation_count || '|' || due_work_count || '|' || "
            "notification_wake_count FROM core_maintenancestate WHERE key = 'worker';"
        )
        if raw:
            reconciliation, due, wakes = (int(value) for value in raw.split("|"))
            maintenance = {
                "reconciliation_count": reconciliation,
                "due_work_count": due,
                "notification_wake_count": wakes,
            }
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "database_transactions": int(transactions),
        "maintenance": maintenance,
    }


def _sleep_with_memory_samples(
    stack: MeasurementStack, duration_seconds: int, sample_seconds: int
) -> list[MemorySample]:
    samples: list[MemorySample] = []
    deadline = time.monotonic() + duration_seconds
    next_sample = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_sample:
            boundary = _boundary(stack)
            samples.append(
                {
                    "captured_at": boundary.captured_at,
                    "cgroup_bytes": boundary.memory_bytes,
                    "anonymous_rss_bytes": boundary.anonymous_rss_bytes,
                }
            )
            next_sample += sample_seconds
        time.sleep(max(0, min(next_sample, deadline) - time.monotonic()))
    return samples


@contextmanager
def _docker_events(stack: MeasurementStack) -> Iterator[list[str]]:
    """Capture events live so the daemon's bounded history cannot truncate them."""

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        process = subprocess.Popen(  # nosec B603
            [
                "docker",
                "events",
                "--since",
                str(time.time()),
                "--filter",
                f"label=com.docker.compose.project={stack.project_name}",
                "--format",
                "{{json .}}",
            ],
            env=stack.environment,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
        events: list[str] = []
        try:
            yield events
            if process.poll() is not None:
                raise LaneError("Docker events", "The live Docker event stream ended early.")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if process.stderr is not None:
                process.stderr.close()
            output.seek(0)
            events.extend(output.read().splitlines())


def _delta(start: dict[str, int] | None, end: dict[str, int] | None) -> dict[str, int] | None:
    if start is None or end is None:
        return None
    return {name: end[name] - start.get(name, 0) for name in end}


def _run_load(stack: MeasurementStack, operations: int) -> None:
    # This path exists inside one disposable, isolated web container.
    fixture = "/tmp/startunnel-resource.env"  # nosec B108
    stack.run(
        "provision internal load credentials",
        "exec",
        "-T",
        "-e",
        "STARTUNNEL_LIVE_AGENT_TESTS=1",
        "web",
        "/app/.venv/bin/python",
        "manage.py",
        "provision_live_agent_proof",
        "--output",
        fixture,
        timeout_seconds=120,
    )
    command = (
        f"set -a; . {fixture}; set +a; export STARTUNNEL_BASE_URL=http://127.0.0.1:8000; "
        "cd /app/examples/python; "
        "/app/.venv/bin/python concurrent_replies.py >/dev/null"
    )
    for number in range(operations):
        stack.run(
            f"internal load operation {number + 1}",
            "exec",
            "-T",
            "web",
            "/bin/sh",
            "-ec",
            command,
            timeout_seconds=120,
        )
    stack.run(
        "remove internal load credentials",
        "exec",
        "-T",
        "web",
        "rm",
        "-f",
        fixture,
        timeout_seconds=30,
    )


def _prove_deletion(stack: MeasurementStack, timeout_seconds: int) -> DeletionProof:
    cycle_id = stack.sql(
        "SELECT id FROM tunnels_cycle WHERE state = 'closed' ORDER BY created_at DESC LIMIT 1;"
    )
    if not cycle_id:
        raise LaneError("deletion proof", "The load did not create a closed cycle.")
    started = time.monotonic()
    stack.sql(
        "UPDATE tunnels_cycle SET delete_after = now() - interval '1 second' "
        f"WHERE id = '{cycle_id}'; SELECT pg_notify('startunnel_maintenance', 'cycle_due');"
    )
    state = ""
    message_count = -1
    while time.monotonic() - started <= timeout_seconds:
        raw = stack.sql(
            "SELECT c.state || '|' || count(m.id) FROM tunnels_cycle c "
            "LEFT JOIN tunnels_message m ON m.cycle_id = c.id "
            f"WHERE c.id = '{cycle_id}' GROUP BY c.state;"
        )
        if raw:
            state, raw_count = raw.split("|")
            message_count = int(raw_count)
        if state == "deleted" and message_count == 0:
            return {
                "passed": True,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "target_seconds": timeout_seconds,
                "message_count": message_count,
            }
        time.sleep(2)
    raise LaneError("deletion proof", "Expired content was not removed within the target.")


def _measure_stack(
    root: Path,
    project_name: str,
    label: str,
    *,
    warmup_seconds: int,
    idle_seconds: int,
    sample_seconds: int,
    cycles: int,
    settle_seconds: int,
    load_operations: int,
    deletion_timeout_seconds: int,
) -> StackMeasurement:
    with MeasurementStack(root, project_name, label) as stack:
        build_started = time.monotonic()
        stack.run("build runtime image", "build", "web", timeout_seconds=2_400)
        build_seconds = time.monotonic() - build_started
        startup_started = time.monotonic()
        stack.run(
            "start runtime stack",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "web",
            "maintenance",
            timeout_seconds=600,
        )
        ready_seconds = time.monotonic() - startup_started
        stack.cache_containers()
        print(f"[{label}: warm up for {warmup_seconds} seconds]", flush=True)
        _sleep_with_memory_samples(stack, warmup_seconds, sample_seconds)
        activity_start = _activity_counters(stack)
        with _docker_events(stack) as events:
            idle_start = _boundary(stack)
            idle_started = time.monotonic()
            idle_samples = _sleep_with_memory_samples(stack, idle_seconds, sample_seconds)
            idle_duration = time.monotonic() - idle_started
            idle_end = _boundary(stack)
        health_probes = count_health_probes("\n".join(events))
        if stack.container_ids() != stack.identifiers:
            raise LaneError("runtime containers", "The measured container set changed.")
        activity_end = _activity_counters(stack)

        cycle_results: list[CycleMeasurement] = []
        stabilized_totals: list[int] = []
        stabilized_services: dict[str, list[int]] = {service: [] for service in stack.identifiers}
        peak_memory = max(
            sum(idle_start.memory_bytes.values()),
            sum(idle_end.memory_bytes.values()),
            *(sum(sample["cgroup_bytes"].values()) for sample in idle_samples),
        )
        for cycle in range(1, cycles + 1):
            before = _boundary(stack)
            _run_load(stack, load_operations)
            load_end = _boundary(stack)
            settle_samples = _sleep_with_memory_samples(stack, settle_seconds, sample_seconds)
            settled = _boundary(stack)
            all_memory = [sample["cgroup_bytes"] for sample in settle_samples]
            all_memory.extend((load_end.memory_bytes, settled.memory_bytes))
            peak_memory = max(peak_memory, *(sum(values.values()) for values in all_memory))
            stabilized_total = sum(settled.memory_bytes.values())
            stabilized_totals.append(stabilized_total)
            for service, value in settled.memory_bytes.items():
                stabilized_services[service].append(value)
            cycle_results.append(
                {
                    "cycle": cycle,
                    "before": _boundary_data(before),
                    "after_load": _boundary_data(load_end),
                    "after_settle": _boundary_data(settled),
                    "settle_memory_samples": settle_samples,
                }
            )

        deletion: DeletionProof | None = None
        if label == "candidate":
            deletion = _prove_deletion(stack, deletion_timeout_seconds)
        percentages = cpu_percentages(idle_start, idle_end)
        cpu_percent = sum(percentages.values())
        return {
            "source": str(root),
            "project_name": project_name,
            "build_seconds": round(build_seconds, 3),
            "ready_seconds": round(ready_seconds, 3),
            "idle": {
                "requested_seconds": idle_seconds,
                "actual_seconds": round(idle_duration, 3),
                "start": _boundary_data(idle_start),
                "end": _boundary_data(idle_end),
                "cpu_percent_of_one_core": round(cpu_percent, 4),
                "cpu_percent_by_service": {
                    name: round(value, 4) for name, value in percentages.items()
                },
                "database_transaction_delta": (
                    activity_end["database_transactions"] - activity_start["database_transactions"]
                ),
                "maintenance_delta": _delta(
                    activity_start["maintenance"], activity_end["maintenance"]
                ),
                "health_probe_executions": health_probes,
                "health_probe_scope": "Live observer interval includes boundary collection.",
                "memory_samples": idle_samples,
                "activity_counter_start": activity_start,
                "activity_counter_end": activity_end,
                "database_counter_scope": "Includes observation queries outside the CPU interval.",
            },
            "cycles": cycle_results,
            "memory": {
                "stabilized_totals_bytes": stabilized_totals,
                "sampled_peak_total_bytes": peak_memory,
                "stable": all(memory_is_stable(values) for values in stabilized_services.values()),
                "stabilized_service_bytes": stabilized_services,
                "criterion": "final <= first + max(5 MiB, 10%) and no material rise in every cycle",
            },
            "deletion": deletion,
        }


@contextmanager
def _baseline_worktree(ref: str) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="startunnel-baseline-"))
    added = False
    try:
        result = subprocess.run(  # nosec B603
            ["git", "worktree", "add", "--detach", str(directory), ref],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise LaneError("baseline checkout", "Could not create the detached baseline worktree.")
        added = True
        yield directory
    finally:
        if added:
            subprocess.run(  # nosec B603
                ["git", "worktree", "remove", "--force", str(directory)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        shutil.rmtree(directory, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", help="Optional Git ref for a baseline comparison.")
    parser.add_argument("--warmup-seconds", type=int, default=120)
    parser.add_argument("--idle-seconds", type=int, default=600)
    parser.add_argument("--sample-seconds", type=int, default=60)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--settle-seconds", type=int, default=60)
    parser.add_argument("--load-operations", type=int, default=5)
    parser.add_argument("--deletion-timeout-seconds", type=int, default=300)
    return parser


def _positive(arguments: argparse.Namespace) -> None:
    for name, value in vars(arguments).items():
        if isinstance(value, int) and value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive.")


def _write_report(report: ResourceReport) -> Path:
    output = (
        ROOT
        / "artifacts"
        / ("resource-measurement-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = _parser().parse_args(raw_arguments)
    try:
        _positive(arguments)
        environment = safe_environment()
        assert_safe_target(environment, "sttest-resource-preflight")
        assert_local_docker(environment)
        docker_version = subprocess.run(  # nosec B603
            ["docker", "version", "--format", "{{.Server.Version}}"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        candidate_commit = subprocess.run(  # nosec B603
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        common: MeasurementOptions = {
            "warmup_seconds": arguments.warmup_seconds,
            "idle_seconds": arguments.idle_seconds,
            "sample_seconds": arguments.sample_seconds,
            "cycles": arguments.cycles,
            "settle_seconds": arguments.settle_seconds,
            "load_operations": arguments.load_operations,
            "deletion_timeout_seconds": arguments.deletion_timeout_seconds,
        }
        baseline = None
        with termination_signals():
            if arguments.baseline_ref:
                with _baseline_worktree(arguments.baseline_ref) as baseline_root:
                    baseline = _measure_stack(
                        baseline_root,
                        generated_project_name("resource-baseline"),
                        "baseline",
                        **common,
                    )
            candidate = _measure_stack(
                ROOT,
                generated_project_name("resource-candidate"),
                "candidate",
                **common,
            )
        assert candidate["deletion"] is not None
        report: ResourceReport = {
            "schema": "startunnel-resource-measurement",
            "created_at": datetime.now(UTC).isoformat(),
            "host": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor_count": os.cpu_count(),
                "docker_server_version": docker_version,
            },
            "method": {
                "baseline_ref": arguments.baseline_ref,
                "candidate_ref": "working-tree",
                "candidate_commit": candidate_commit,
                "compose_file": "compose.yaml",
                "scope": "All running containers in the isolated reference Compose project.",
                "web_database_pool_max_size": 18,
                "command": "python scripts/run_resource_measurement.py " + " ".join(raw_arguments),
                "production_process_mode": "Uvicorn with one worker and no reloader",
                "external_clients": False,
                "cgroup_cpu_reads": "External Engine counters with per-service monotonic intervals",
                "memory_sample_seconds": arguments.sample_seconds,
                **common,
            },
            "baseline": baseline,
            "candidate": candidate,
            "acceptance": {
                "candidate_idle_cpu_below_one_percent": (
                    candidate["idle"]["cpu_percent_of_one_core"] < 1.0
                ),
                "candidate_memory_stable": candidate["memory"]["stable"],
                "candidate_deletion_within_target": bool(candidate["deletion"]["passed"]),
                "candidate_recurring_work_bounded": recurring_work_is_bounded(candidate["idle"]),
                "candidate_ready_within_two_minutes": candidate["ready_seconds"] < 120,
                "physical_wakeups_or_watts_measured": False,
            },
        }
        report_path = _write_report(report)
        if not all(
            (
                report["acceptance"]["candidate_idle_cpu_below_one_percent"],
                report["acceptance"]["candidate_memory_stable"],
                report["acceptance"]["candidate_deletion_within_target"],
                report["acceptance"]["candidate_recurring_work_bounded"],
                report["acceptance"]["candidate_ready_within_two_minutes"],
            )
        ):
            print(f"Resource targets were missed. Safe report: {report_path}", file=sys.stderr)
            return 1
        print(f"Resource measurement passed. Safe report: {report_path}")
    except (
        ValueError,
        KeyError,
        OSError,
        http.client.HTTPException,
        subprocess.SubprocessError,
        InterruptedError,
        IsolationError,
        LaneError,
    ) as error:
        print(f"Resource measurement failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
