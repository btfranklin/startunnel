"""Resource reports use external counters, measured intervals, and owned observers."""

import subprocess
import time
from contextlib import contextmanager
from typing import Any

import pytest
from scripts import run_resource_measurement as resources
from scripts.compose_isolation import IsolationError, LaneError
from scripts.docker_stats import DockerStats


def _boundary(usage: dict[str, int], captured: dict[str, float]) -> resources.Boundary:
    return resources.Boundary("now", usage, captured, {name: 100 for name in usage}, {})


def test_cpu_rates_use_nanoseconds_and_each_services_actual_interval() -> None:
    start = _boundary({"web": 2_000_000_000, "postgres": 1_000_000_000}, {"web": 0, "postgres": 5})
    end = _boundary({"web": 3_000_000_000, "postgres": 2_000_000_000}, {"web": 10, "postgres": 25})
    assert resources.cpu_percentages(start, end) == {"web": 10, "postgres": 5}


@pytest.mark.parametrize(
    "usage, captured",
    [({"web": 9}, {"web": 2}), ({"web": 20}, {"web": 1}), ({}, {})],
)
def test_changed_or_reset_counters_cannot_pass_as_zero_cpu(
    usage: dict[str, int], captured: dict[str, float]
) -> None:
    with pytest.raises(ValueError):
        resources.cpu_percentages(_boundary({"web": 10}, {"web": 1}), _boundary(usage, captured))


@pytest.mark.parametrize("running, restart", [(True, 0), (True, 1), (False, 0)])
def test_external_snapshot_checks_container_state_without_exec_or_sql(
    monkeypatch: pytest.MonkeyPatch, running: bool, restart: int
) -> None:
    calls: list[str] = []

    class Stats:
        def get(self, path: str) -> dict[str, Any]:
            calls.append(path)
            if "/stats?" in path:
                return {
                    "cpu_stats": {"cpu_usage": {"total_usage": 123}},
                    "memory_stats": {"usage": 456, "stats": {"anon": 321}},
                }
            return {"State": {"Running": running, "StartedAt": "start"}, "RestartCount": restart}

    stack = resources.MeasurementStack(resources.ROOT, "sttest-resource-unit", "candidate")
    stack.identifiers = {"web": "id"}
    stack.started = {"web": ("start", 0)}
    stack.stats = Stats()  # type: ignore[assignment]
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    if running and restart == 0:
        snapshot = resources._boundary(stack)
        assert snapshot.cpu_captured_monotonic_seconds == {"web": 11}
        assert snapshot.cpu_usage_ns == {"web": 123}
        assert snapshot.memory_bytes == {"web": 456}
        assert snapshot.anonymous_rss_bytes == {"web": 321}
    else:
        with pytest.raises(LaneError):
            resources._boundary(stack)
    assert calls == ["/containers/id/stats?stream=false&one-shot=true", "/containers/id/json"]


def test_memory_sampling_keeps_its_schedule_when_collection_takes_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    reads = []

    def snapshot(stack: object) -> resources.Boundary:
        reads.append(clock[0])
        clock[0] += 0.3
        return _boundary({"web": 0}, {"web": clock[0]})

    monkeypatch.setattr(resources, "_boundary", snapshot)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))
    resources._sleep_with_memory_samples(None, 25, 10)  # type: ignore[arg-type]
    assert reads == [0, 10, 20]
    assert clock == [25]


def test_live_events_are_complete_and_observer_is_reaped_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended = []

    class Process:
        stderr = None
        stopped = False

        def poll(self) -> int | None:
            return -15 if self.stopped else None

        def terminate(self) -> None:
            self.stopped = True
            ended.append("terminate")

        def wait(self, timeout: int) -> int:
            ended.append("wait")
            return -15

    def start(*args: Any, **kwargs: Any) -> Process:
        kwargs["stdout"].write("event\n" * 300)
        kwargs["stdout"].flush()
        return Process()

    monkeypatch.setattr(subprocess, "Popen", start)
    stack = resources.MeasurementStack(resources.ROOT, "sttest-resource-events", "candidate")
    with pytest.raises(RuntimeError), resources._docker_events(stack) as events:
        raise RuntimeError("measurement interrupted")
    assert len(events) == 300
    assert ended == ["terminate", "wait"]


def test_memory_gate_checks_services_even_when_total_memory_is_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter([0, 0, 0, 0, 6, 6, 6, 12, 12, 12, 18])
    captured = iter(range(11))

    class Stack:
        identifiers = {"web": "web", "postgres": "postgres"}

        def __init__(self, *args: object) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def run(self, *args: object, **kwargs: object) -> None:
            pass

        def cache_containers(self) -> None:
            pass

        def container_ids(self) -> dict[str, str]:
            return self.identifiers

    def snapshot(stack: object) -> resources.Boundary:
        growth = next(snapshots) * 1024 * 1024
        return resources.Boundary(
            "now",
            {"web": 0},
            {"web": float(next(captured))},
            {"web": 100_000_000 + growth, "postgres": 100_000_000 - growth},
            {},
        )

    @contextmanager
    def events(stack: object) -> Any:
        yield []

    monkeypatch.setattr(resources, "MeasurementStack", Stack)
    monkeypatch.setattr(resources, "_boundary", snapshot)
    monkeypatch.setattr(resources, "_docker_events", events)
    monkeypatch.setattr(
        resources,
        "_activity_counters",
        lambda stack: {"database_transactions": 0, "maintenance": None},
    )
    monkeypatch.setattr(resources, "_sleep_with_memory_samples", lambda *args: [])
    monkeypatch.setattr(resources, "_run_load", lambda *args: None)
    result = resources._measure_stack(
        resources.ROOT,
        "sttest-resource-memory",
        "baseline",
        warmup_seconds=1,
        idle_seconds=1,
        sample_seconds=1,
        cycles=3,
        settle_seconds=1,
        load_operations=1,
        deletion_timeout_seconds=1,
    )
    assert result["memory"]["stabilized_totals_bytes"] == [200_000_000] * 3
    assert result["memory"]["stable"] is False


def test_unsupported_docker_transport_reports_missing_setup() -> None:
    with pytest.raises(ValueError, match="Unix socket or TCP"):
        DockerStats("npipe:////./pipe/docker_engine")


@pytest.mark.parametrize(
    "status, body", [(200, b'{"counter": 123}'), (200, b"[]"), (500, b"failed")]
)
def test_engine_client_reads_raw_json_and_closes_connections(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes
) -> None:
    from io import BytesIO

    calls: list[tuple[str, str] | str] = []

    class Response(BytesIO):
        status: int

    class Connection:
        def request(self, method: str, path: str) -> None:
            calls.append((method, path))

        def getresponse(self) -> Response:
            response = Response(body)
            response.status = status
            return response

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("scripts.docker_stats.UnixConnection", lambda path: Connection())
    client = DockerStats("unix:///test/docker.sock")
    if status == 200 and body.startswith(b"{"):
        assert client.get("/containers/id/stats") == {"counter": 123}
    else:
        with pytest.raises(ValueError):
            client.get("/containers/id/stats")
    assert calls == [("GET", "/containers/id/stats"), "close"]


def test_container_discovery_includes_optional_services_and_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    rows = [
        {"Service": service, "Name": name, "ID": name + "-id"}
        for service, name in [
            ("postgres", "db"),
            ("maintenance", "worker"),
            ("web", "web-1"),
            ("web", "web-2"),
            ("valkey", "old-cache"),
            ("caddy", "edge"),
        ]
    ]
    stack = resources.MeasurementStack(resources.ROOT, "sttest-resource-discovery", "baseline")
    monkeypatch.setattr(stack, "capture", lambda *args: "\n".join(json.dumps(row) for row in rows))
    assert stack.container_ids() == {
        "postgres": "db-id",
        "maintenance": "worker-id",
        "web:web-1": "web-1-id",
        "web:web-2": "web-2-id",
        "valkey": "old-cache-id",
        "caddy": "edge-id",
    }


def test_remote_host_override_is_rejected_before_inspecting_local_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = resources.MeasurementStack(resources.ROOT, "sttest-resource-override", "candidate")
    stack.environment["DOCKER_HOST"] = "tcp://remote.example:2375"

    def unexpected_context(*args: object) -> None:
        pytest.fail("Remote override must be rejected before querying Docker.")

    monkeypatch.setattr(resources, "assert_local_docker", unexpected_context)
    with pytest.raises(IsolationError, match="remote Docker endpoint"):
        stack.__enter__()


def test_engine_io_failure_is_bounded_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = []

    class Connection:
        def request(self, *args: object) -> None:
            raise OSError("arbitrary transport detail")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("scripts.docker_stats.UnixConnection", lambda path: Connection())
    with pytest.raises(ValueError, match=r"^Could not read Docker resource counters\.$"):
        DockerStats("unix:///test/docker.sock").get("/containers/id/stats")
    assert closed == [True]
