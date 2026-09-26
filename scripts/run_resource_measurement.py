"""Compare baseline and candidate runtime resources in disposable Compose stacks."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from scripts.compose_isolation import (
        IsolationError,
        LaneError,
        assert_local_docker,
        assert_project_unused,
        generated_project_name,
        safe_environment,
        termination_signals,
        validate_project_name,
    )
else:
    from compose_isolation import (
        IsolationError,
        LaneError,
        assert_local_docker,
        assert_project_unused,
        generated_project_name,
        safe_environment,
        termination_signals,
        validate_project_name,
    )

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("postgres", "valkey", "web", "maintenance")
APP_SERVICES = ("web", "maintenance")
HEALTH_MARKERS = (
    "health/ready",
    "pg_isready",
    "startunnel-maintenance-healthcheck",
    "startunnel-valkey-healthcheck",
)


@dataclass(frozen=True, slots=True)
class Boundary:
    captured_at: str
    cpu_usage_usec: dict[str, int]
    memory_bytes: dict[str, int]
    anonymous_rss_bytes: dict[str, int]
    database_transactions: int
    maintenance: dict[str, int] | None


def aggregate_one_core_percent(
    start: Mapping[str, int], end: Mapping[str, int], duration_seconds: float
) -> float:
    """Return aggregate cgroup CPU use as a percentage of one CPU core."""

    if duration_seconds <= 0:
        raise ValueError("The measurement duration must be positive.")
    used = sum(max(0, end.get(name, 0) - value) for name, value in start.items())
    return used / (duration_seconds * 1_000_000) * 100


def memory_is_stable(samples: Sequence[int]) -> bool:
    """Allow normal allocator noise but reject continued material growth."""

    if len(samples) < 3:
        return True
    allowance = max(5 * 1024 * 1024, int(samples[0] * 0.10))
    materially_rising = all(later > earlier + 1024 * 1024 for earlier, later in pairwise(samples))
    return not materially_rising and samples[-1] <= samples[0] + allowance


def parse_cpu_stat(raw: str) -> int:
    """Read usage_usec from a cgroup v2 cpu.stat value."""

    for line in raw.splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec" and value.isdigit():
            return int(value)
    raise ValueError("cpu.stat did not contain usage_usec.")


def parse_memory_stat_anon(raw: str) -> int:
    """Read anonymous resident memory from a cgroup v2 memory.stat value."""

    for line in raw.splitlines():
        name, _, value = line.partition(" ")
        if name == "anon" and value.isdigit():
            return int(value)
    raise ValueError("memory.stat did not contain anon.")


def count_health_probes(events: str) -> dict[str, int]:
    """Count Compose health-check exec events without counting inspection execs."""

    counts: dict[str, int] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = str(event.get("Action", ""))
        if "exec_create:" not in action or not any(marker in action for marker in HEALTH_MARKERS):
            continue
        attributes = event.get("Actor", {}).get("Attributes", {})
        service = attributes.get("com.docker.compose.service")
        if isinstance(service, str):
            counts[service] = counts.get(service, 0) + 1
    return counts


def recurring_work_is_bounded(idle: Mapping[str, Any]) -> bool:
    """Accept at most one scheduled probe or reconciliation per minute plus jitter."""

    allowed = math.ceil(float(idle["actual_seconds"]) / 60) + 1
    health = idle["health_probe_executions"]
    maintenance = idle["maintenance_delta"]
    health_is_bounded = all(int(count) <= allowed for count in health.values())
    reconciliation_is_bounded = (
        maintenance is None or int(maintenance["reconciliation_count"]) <= allowed
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
                "STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE": "6",
                "STARTUNNEL_MAINTENANCE_DATABASE_POOL_MAX_SIZE": "4",
                "STARTUNNEL_WEB_WORKERS": "1",
            }
        )
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._env_file: Path | None = None

    def __enter__(self) -> MeasurementStack:
        assert_local_docker(self.environment)
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
        identifiers: dict[str, str] = {}
        configured = set(
            self.capture("inspect configured services", "config", "--services").splitlines()
        )
        for service in SERVICES:
            if service not in configured:
                continue
            value = self.capture(f"inspect {service} container", "ps", "--quiet", service)
            if value:
                identifiers[service] = value.splitlines()[0]
        return identifiers

    def container_file(self, service: str, path: str) -> str:
        return self.capture(f"read {service} {path}", "exec", "-T", service, "cat", path)

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
    memory: dict[str, int] = {}
    rss: dict[str, int] = {}
    for service in stack.container_ids():
        cpu[service] = parse_cpu_stat(stack.container_file(service, "/sys/fs/cgroup/cpu.stat"))
        raw_memory = stack.container_file(service, "/sys/fs/cgroup/memory.current")
        if not raw_memory.isdigit():
            raise LaneError("cgroup memory", f"Invalid memory.current value for {service}.")
        memory[service] = int(raw_memory)
        rss[service] = parse_memory_stat_anon(
            stack.container_file(service, "/sys/fs/cgroup/memory.stat")
        )
    transactions_raw = stack.sql(
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
    return Boundary(
        captured_at=datetime.now(UTC).isoformat(),
        cpu_usage_usec=cpu,
        memory_bytes=memory,
        anonymous_rss_bytes=rss,
        database_transactions=int(transactions_raw),
        maintenance=maintenance,
    )


def _sleep_with_memory_samples(
    stack: MeasurementStack, duration_seconds: int, sample_seconds: int
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    deadline = time.monotonic() + duration_seconds
    next_sample = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_sample:
            values: dict[str, int] = {}
            rss: dict[str, int] = {}
            for service in stack.container_ids():
                raw = stack.container_file(service, "/sys/fs/cgroup/memory.current")
                values[service] = int(raw)
                rss[service] = parse_memory_stat_anon(
                    stack.container_file(service, "/sys/fs/cgroup/memory.stat")
                )
            samples.append(
                {
                    "captured_at": datetime.now(UTC).isoformat(),
                    "cgroup_bytes": values,
                    "anonymous_rss_bytes": rss,
                }
            )
            next_sample = now + sample_seconds
        time.sleep(min(10, max(0.1, deadline - time.monotonic())))
    return samples


def _docker_events(
    stack: MeasurementStack, started_epoch: int, finished_epoch: int
) -> dict[str, int]:
    command = [
        "docker",
        "events",
        "--since",
        str(started_epoch),
        "--until",
        str(finished_epoch),
        "--filter",
        f"label=com.docker.compose.project={stack.project_name}",
        "--format",
        "{{json .}}",
    ]
    try:
        result = subprocess.run(  # nosec B603
            command,
            env=stack.environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaneError("Docker events", "Could not read Docker health events.") from error
    if result.returncode:
        raise LaneError("Docker events", "Docker did not return health events.")
    return count_health_probes(result.stdout)


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


def _prove_deletion(stack: MeasurementStack, timeout_seconds: int) -> dict[str, Any]:
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
) -> dict[str, Any]:
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
            timeout_seconds=600,
        )
        ready_seconds = time.monotonic() - startup_started
        print(f"[{label}: warm up for {warmup_seconds} seconds]", flush=True)
        _sleep_with_memory_samples(stack, warmup_seconds, sample_seconds)
        idle_event_start = int(time.time())
        idle_start = _boundary(stack)
        idle_started = time.monotonic()
        idle_samples = _sleep_with_memory_samples(stack, idle_seconds, sample_seconds)
        idle_duration = time.monotonic() - idle_started
        idle_end = _boundary(stack)
        idle_event_end = int(time.time())
        health_probes = _docker_events(stack, idle_event_start, idle_event_end)

        cycle_results: list[dict[str, Any]] = []
        stabilized_totals: list[int] = []
        peak_memory = sum(idle_end.memory_bytes.values())
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
            cycle_results.append(
                {
                    "cycle": cycle,
                    "before": asdict(before),
                    "after_load": asdict(load_end),
                    "after_settle": asdict(settled),
                    "settle_memory_samples": settle_samples,
                }
            )

        deletion: dict[str, Any] | None = None
        if label == "candidate":
            deletion = _prove_deletion(stack, deletion_timeout_seconds)
        cpu_percent = aggregate_one_core_percent(
            idle_start.cpu_usage_usec, idle_end.cpu_usage_usec, idle_duration
        )
        return {
            "source": str(root),
            "project_name": project_name,
            "build_seconds": round(build_seconds, 3),
            "ready_seconds": round(ready_seconds, 3),
            "idle": {
                "requested_seconds": idle_seconds,
                "actual_seconds": round(idle_duration, 3),
                "start": asdict(idle_start),
                "end": asdict(idle_end),
                "cpu_percent_of_one_core": round(cpu_percent, 4),
                "database_transaction_delta": (
                    idle_end.database_transactions - idle_start.database_transactions
                ),
                "maintenance_delta": _delta(idle_start.maintenance, idle_end.maintenance),
                "health_probe_executions": health_probes,
                "memory_samples": idle_samples,
            },
            "cycles": cycle_results,
            "memory": {
                "stabilized_totals_bytes": stabilized_totals,
                "peak_total_bytes": peak_memory,
                "stable": memory_is_stable(stabilized_totals),
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
    parser.add_argument("--baseline-ref", default="HEAD", help="Baseline Git ref (default: HEAD).")
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


def _write_report(report: dict[str, Any]) -> Path:
    output = (
        ROOT
        / "artifacts"
        / ("resource-comparison-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
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
        common = {
            "warmup_seconds": arguments.warmup_seconds,
            "idle_seconds": arguments.idle_seconds,
            "sample_seconds": arguments.sample_seconds,
            "cycles": arguments.cycles,
            "settle_seconds": arguments.settle_seconds,
            "load_operations": arguments.load_operations,
            "deletion_timeout_seconds": arguments.deletion_timeout_seconds,
        }
        with termination_signals(), _baseline_worktree(arguments.baseline_ref) as baseline_root:
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
        report: dict[str, Any] = {
            "schema": "startunnel-resource-comparison",
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
                "command": "python scripts/run_resource_measurement.py " + " ".join(raw_arguments),
                "production_process_mode": "Uvicorn with one worker and no reloader",
                "external_clients": False,
                "cgroup_cpu_reads": "measurement boundaries only",
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
        print(f"Resource comparison passed. Safe report: {report_path}")
    except (
        ValueError,
        subprocess.SubprocessError,
        InterruptedError,
        IsolationError,
        LaneError,
    ) as error:
        print(f"Resource comparison failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
