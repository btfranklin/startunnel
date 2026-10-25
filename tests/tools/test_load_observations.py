"""Load observations contain only bounded numeric service data."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from prometheus_client import CollectorRegistry
from scripts import load_observations, run_full_load_profile
from scripts.compose_isolation import IsolatedComposeProject, LaneError
from scripts.docker_stats import DockerStats
from scripts.json_data import JsonObject
from scripts.load_observations import container_counters, numeric_fields
from scripts.proof_timing import PhaseTimings
from tests.load import run_profile


def test_counter_allowlists_drop_secrets_and_unknowns() -> None:
    raw: JsonObject = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 12, "query": "secret-sql"},
            "throttling_data": {"periods": 3, "throttled_periods": 1, "secret": "key"},
        },
        "memory_stats": {"usage": 100, "stats": {"pgmajfault": 2, "secret": "key"}},
        "secret": "credential",
    }
    values = container_counters(raw)
    assert values["cpu_usage_ns"] == 12
    memory = values["memory"]
    assert isinstance(memory, dict)
    assert memory["limit"] is None
    assert "secret" not in json.dumps(values)
    assert numeric_fields({"a": "secret", "b": float("nan"), "c": -1}, ("a", "b", "c")) == {
        "a": None,
        "b": None,
        "c": None,
    }


def test_failed_phase_retains_both_clocks() -> None:
    phases = PhaseTimings()
    with pytest.raises(InterruptedError), phases.phase("cleanup"):
        raise InterruptedError("must-not-be-recorded")
    record = phases.records[0]
    assert record["status"] == "failed"
    assert float(str(record["finished_monotonic_seconds"])) >= float(
        str(record["started_monotonic_seconds"])
    )
    assert float(str(record["elapsed_seconds"])) >= 0
    assert str(record["started_at"]).endswith("+00:00")
    assert "must-not-be-recorded" not in json.dumps(record)


@pytest.mark.parametrize("interrupt", [False, True])
def test_fixture_failure_retains_safe_report_and_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
) -> None:
    monkeypatch.setenv("STARTUNNEL_LOAD_TESTS", "1")
    manifest = tmp_path / "manifest.json"
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(run_profile, "default_manifest_path", lambda: manifest)

    def provision(*args: object) -> None:
        manifest.write_text("secret-credential")
        if interrupt:
            raise InterruptedError("secret-credential")
        raise ValueError("secret-credential")

    def cleanup(path: Path) -> bool:
        path.unlink()
        return True

    monkeypatch.setattr(run_profile, "_provision", provision)
    monkeypatch.setattr(run_profile, "_cleanup", cleanup)
    assert run_profile.main(["--report", str(report_path), "--base-url", "http://web"]) == 1
    report = json.loads(report_path.read_text())
    assert report["passed"] is False
    assert report["fixture_cleanup_complete"] is True
    assert report["release_evidence_eligible"] is False
    assert report["phases"][0]["status"] == "failed"
    assert report["phases"][-1]["status"] == "passed"
    assert not manifest.exists()
    assert "secret-credential" not in report_path.read_text()


@pytest.mark.parametrize("failure", [None, "identity", "workload", "cleanup"])
def test_run_report_is_final_only_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    project = "stload-report-focused-12345678"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fake_script = tmp_path / "scripts" / "run_full_load_profile.py"
    events: list[str] = []
    candidate = "example/startunnel@sha256:" + "c" * 64

    class Stack:
        cleanup_complete = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.environment = {"PATH": "/safe"}

        def __enter__(self) -> Stack:
            return self

        def __exit__(self, *args: object) -> None:
            events.append("cleanup")
            assert not (artifacts / f"load-run-{project}.json").exists()
            if failure == "cleanup":
                raise LaneError("cleanup", "secret-credential")
            self.cleanup_complete = True

        def build_services(self, *args: object, **kwargs: object) -> None:
            pass

        def pull_candidate(self) -> None:
            pass

        def verify_candidate_services(self) -> None:
            events.append("verify")
            if failure == "identity":
                raise LaneError("candidate image proof", "secret-credential")

        def run(self, step: str, *args: str, **kwargs: object) -> None:
            if args[0] == "run" and args[-1] == "load-profile":
                events.append("workload")
                if failure == "workload":
                    raise InterruptedError("secret-credential")
                (artifacts / f"load-profile-{project}.json").write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "configuration": {},
                            "fixture_cleanup_complete": True,
                            "driver_observations_file": f"load-profile-{project}.driver.jsonl",
                        }
                    )
                )
                (artifacts / f"load-profile-{project}.driver.jsonl").write_text("{}\n")

        def capture(self, *args: object) -> str:
            return "image"

    class Observations:
        failed = False
        samples = 1

        def __init__(self, *args: object) -> None:
            pass

        def __enter__(self) -> Observations:
            events.append("observe")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("stop_observe")

        def summary(self) -> dict[str, object]:
            return {"samples": 1, "complete": True}

    @contextmanager
    def signals() -> Iterator[None]:
        yield

    def output(command: list[str], **kwargs: object) -> str:
        if command[0] == "git":
            return "a" * 40
        if command[:2] == ["docker", "version"]:
            return "29.0.0"
        if (
            command[4]
            == '{{index .Config.Labels "net.startunnel.source"}}|{{.Os}}/{{.Architecture}}'
        ):
            return "a" * 40 + "|linux/arm64"
        if command[4] == "{{.Os}}/{{.Architecture}}":
            return "linux/arm64"
        return "sha256:" + "b" * 64

    monkeypatch.setattr(run_full_load_profile, "__file__", str(fake_script))
    monkeypatch.setattr(run_full_load_profile, "IsolatedComposeProject", Stack)
    monkeypatch.setattr(run_full_load_profile, "LoadObservations", Observations)
    monkeypatch.setattr(run_full_load_profile, "termination_signals", signals)
    monkeypatch.setattr(run_full_load_profile, "required_output", output)
    result = run_full_load_profile.main(["--project-name", project, "--candidate-image", candidate])
    report_path = artifacts / f"load-run-{project}.json"
    report = json.loads(report_path.read_text())
    assert result == (0 if failure is None else 1)
    assert events[-1] == "cleanup"
    assert report["status"] == ("passed" if failure is None else "failed")
    assert report["release_evidence_eligible"] is (failure is None)
    assert report["disposable_volumes_removed"] is (failure != "cleanup")
    assert "secret-credential" not in report_path.read_text()
    if failure == "identity":
        assert "workload" not in events
    if failure is None:
        assert events.index("verify") < events.index("workload")
        assert events.count("verify") == 2


def test_collector_writes_each_sample_and_retains_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stats:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> dict[str, object]:
            return {"NCPU": 8, "MemTotal": 8192, "secret": "key"}

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}

    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "observations.jsonl")  # type: ignore[arg-type]

    observations.identifiers = {name: "a" * 64 for name in load_observations.SERVICES}
    fake_reader = SimpleNamespace(start=lambda: None, close=lambda: None, failed=False)
    monkeypatch.setattr(load_observations, "PoolReader", lambda *args: fake_reader)

    def sample() -> dict[str, object]:
        if observations.samples:
            raise ValueError("secret-credential")
        return {"monotonic_seconds": 1.0, "collector": {}}

    monkeypatch.setattr(observations, "_sample", sample)
    with observations:
        observations.thread.join(timeout=3)
    assert observations.samples == 1
    assert observations.failed
    assert observations.errors == 1
    assert len(observations.path.read_text().splitlines()) == 1
    assert "secret" not in json.dumps(observations.summary())


def test_metrics_reader_reads_worker_shards_without_http_or_credentials() -> None:
    source = Path(load_observations.__file__).with_name("proof_pool_reader.py").read_text()
    assert "MultiProcessCollector(registry)" in source
    assert "core.metrics" not in source
    assert "urllib" not in source
    assert "TOKEN" not in source
    assert "statement_timeout=500" in str(load_observations.POSTGRES_ENVIRONMENT)


def test_load_runner_rejects_unsafe_project_before_creating_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_full_load_profile, "__file__", str(tmp_path / "scripts" / "runner.py"))
    assert run_full_load_profile.main(["--project-name", "../../unsafe"]) == 2
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("ok,missing,error", [(1, 3, 0), (4, 0, 0), (3, 0, 1)])
def test_collector_requires_all_four_pool_samplers_and_records_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ok: int,
    missing: int,
    error: int,
) -> None:
    class Stats:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> dict[str, object]:
            return {"cpu_stats": {"cpu_usage": {"total_usage": 1}}, "memory_stats": {"usage": 1}}

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}

    def execute(self: object, identifier: str, command: tuple[str, ...], **kwargs: object) -> str:
        if "psql" in command:
            return json.dumps({name: 1 for name in load_observations.DATABASE_FIELDS})
        return "\n".join(
            f'startunnel_database_pool_sampler_status{{status="{status}"}} {count}'
            for status, count in (("ok", ok), ("missing", missing), ("error", error))
        )

    monkeypatch.setattr(Stats, "exec", execute, raising=False)
    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "samples.jsonl")  # type: ignore[arg-type]
    observations.identifiers = {name: "a" * 12 for name in load_observations.SERVICES}
    observations.pool_reader = cast(
        load_observations.PoolReader,
        SimpleNamespace(
            snapshot=lambda: {
                "pool": {"sampler_ok": ok, "sampler_missing": missing, "sampler_error": error},
                "cpu_seconds": 1,
                "peak_memory_bytes": 10,
            }
        ),
    )
    try:
        sample = observations._sample()
    finally:
        observations.executor.shutdown(wait=True)
    observations.samples = 1
    assert observations.pool_samples == int(ok == 4)
    assert observations.summary()["complete"] is (ok == 4)
    assert observations.pool_missing_samples == int(missing > 0)
    assert observations.pool_error_samples == int(error > 0)
    errors = sample["errors"]
    assert isinstance(errors, list)
    assert ("pool_sampler_error" in errors) is (error > 0)


def test_driver_observer_failure_retains_partial_counts_and_cancels_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from tests.load.profile import Credential, CredentialManifest, LoadConfig, run_load_profile

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, path: str, **kwargs: object) -> httpx.Response:
            if path == "/api/v1/tunnels":
                return httpx.Response(
                    201,
                    json={
                        "tunnel": {"address": "private-address"},
                        "cycle": {"id": "private-cycle", "root": {"id": "private-root"}},
                        "activity_cursor": "private-cursor",
                    },
                )
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    report: dict[str, object] = {}
    phases = PhaseTimings()

    def fail(sample: dict[str, object]) -> None:
        assert all(isinstance(value, (str, int, float)) for value in sample.values())
        assert "private" not in json.dumps(sample)
        raise OSError("private-write-failure")

    async def exercise() -> None:
        initial = set(asyncio.all_tasks())
        with pytest.raises(OSError):
            await run_load_profile(
                base_url="http://fixture",
                host_header=None,
                manifest=CredentialManifest(
                    "private-run", (Credential("private-id", "private-key"),)
                ),
                config=LoadConfig(duration_seconds=1, sends_per_second=20),
                partial_report=report,
                phases=phases,
                driver_observer=fail,
            )
        assert set(asyncio.all_tasks()) == initial

    asyncio.run(exercise())
    assert report["passed"] is False
    assert phases.records[-1]["phase"] == "traffic"
    assert phases.records[-1]["status"] == "failed"
    assert "private" not in json.dumps(report)


def test_engine_exec_demultiplexes_only_stdout_and_uses_fixed_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import struct

    from scripts.docker_stats import DockerStats

    requests: list[tuple[str, object]] = []
    identifier = "a" * 64
    exec_id = "b" * 64
    stdout = b'{"connections": 4}'
    stderr = b"private-password"
    frames = struct.pack(">B3sI", 1, b"\0\0\0", len(stdout)) + stdout
    frames += struct.pack(">B3sI", 2, b"\0\0\0", len(stderr)) + stderr
    client = DockerStats("unix:///safe", timeout=1)

    def request(path: str, **kwargs: object) -> bytes:
        requests.append((path, kwargs.get("payload")))
        assert 0 < float(str(kwargs["timeout"])) <= 1
        if path.endswith("/exec"):
            return json.dumps({"Id": exec_id}).encode()
        if path.endswith("/start"):
            return frames
        return b'{"Running":false,"ExitCode":0}'

    monkeypatch.setattr(client, "_request", request)
    result = client.exec(
        identifier,
        load_observations.POSTGRES_COMMAND,
        environment=load_observations.POSTGRES_ENVIRONMENT,
    )
    assert result == stdout.decode()
    assert "private-password" not in result
    body = requests[0][1]
    assert isinstance(body, dict)
    assert body["Cmd"] == list(load_observations.POSTGRES_COMMAND)
    assert body["Tty"] is False
    assert body["AttachStderr"] is True
    assert "PGOPTIONS=-c statement_timeout=500 -c lock_timeout=250" in body["Env"]
    assert len(requests) == 3


@pytest.mark.parametrize("raw", [b"bad", b"\1\0\0\0\0\0\0\5x", b"\3\0\0\0\0\0\0\0"])
def test_engine_exec_rejects_incomplete_or_invalid_frames(raw: bytes) -> None:
    from scripts.docker_stats import demultiplex_stdout

    with pytest.raises(ValueError):
        demultiplex_stdout(raw)


def test_parallel_sample_finishes_under_one_second_and_caches_scoped_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from urllib.parse import parse_qs, urlsplit

    inventories: list[str] = []

    class Stats:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get_list(self, path: str) -> list[dict[str, object]]:
            inventories.append(path)
            query = parse_qs(urlsplit(path).query)
            assert json.loads(query["filters"][0]) == {
                "label": ["com.docker.compose.project=stload-safe-12345678"]
            }
            rows: list[dict[str, object]] = [
                {
                    "Id": str(index) * 64,
                    "State": "running",
                    "Labels": {"com.docker.compose.service": name},
                }
                for index, name in enumerate(load_observations.SERVICES, 1)
            ]
            rows.append(
                {
                    "Id": "a" * 64,
                    "State": "created",
                    "Labels": {"com.docker.compose.service": "load-profile"},
                }
            )
            return rows

        def get(self, path: str) -> dict[str, object]:
            time.sleep(0.1)
            return {"cpu_stats": {"cpu_usage": {"total_usage": 1}}}

        def exec(self, identifier: str, command: tuple[str, ...], **kwargs: object) -> str:
            time.sleep(0.1)
            if "psql" in command:
                return json.dumps({name: 1 for name in load_observations.DATABASE_FIELDS})
            return 'startunnel_database_pool_sampler_status{status="ok"} 4'

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}
        project_name = "stload-safe-12345678"

    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "sample.jsonl")  # type: ignore[arg-type]
    observations.pool_reader = cast(
        load_observations.PoolReader,
        SimpleNamespace(
            snapshot=lambda: {
                "pool": {"sampler_ok": 4, "sampler_missing": 0, "sampler_error": 0},
                "cpu_seconds": 1,
                "peak_memory_bytes": 10,
            }
        ),
    )
    try:
        first = observations._sample()
        second = observations._sample()
    finally:
        observations.executor.shutdown(wait=True)
    assert len(inventories) == 1
    assert observations.identifiers["load-profile"] == "4" * 64
    assert observations.max_sample_overhead_seconds < 0.5
    assert first["errors"] == second["errors"] == []


def test_slow_sample_stops_without_queuing_a_second_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    class Stats:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, path: str) -> dict[str, object]:
            if path == "/info":
                return {}
            release.wait(timeout=2)
            return {}

        def exec(self, *args: object, **kwargs: object) -> str:
            release.wait(timeout=2)
            return "{}"

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}

    release = threading.Event()
    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "sample.jsonl")  # type: ignore[arg-type]
    observations.identifiers = {name: "a" * 64 for name in load_observations.SERVICES}
    fake_reader = SimpleNamespace(
        start=lambda: None,
        close=lambda: None,
        failed=False,
        snapshot=lambda: {"pool": {"sampler_ok": 4}},
    )
    monkeypatch.setattr(load_observations, "PoolReader", lambda *args: fake_reader)
    with observations:
        observations.thread.join(timeout=1.5)
        assert not observations.thread.is_alive()
        assert observations.failed
        assert observations.samples == 1
        release.set()
    sample = json.loads(observations.path.read_text())
    assert "sample_deadline_exceeded" in sample["errors"]
    assert observations.summary()["complete"] is False
    assert observations.executor._shutdown


def test_metrics_probe_reads_existing_worker_files_without_creating_shards(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    environment = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    for _ in range(4):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from core.metrics import DATABASE_POOL_SAMPLER_STATUS; "
                "DATABASE_POOL_SAMPLER_STATUS.labels(status='ok').set(1)",
            ],
            env=environment,
            capture_output=True,
            check=True,
            timeout=5,
        )
    files = sorted(path.name for path in tmp_path.iterdir())
    assert files
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", "from scripts.proof_pool_reader import run; run(0.01)"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        assert rows[1]["pool"]["sampler_ok"] == 4
        assert rows[1]["cpu_seconds"] >= 0
        assert rows[1]["peak_memory_bytes"] > 0
        assert sorted(path.name for path in tmp_path.iterdir()) == files


def test_engine_inventory_returns_array_and_failed_exec_discards_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.docker_stats import DockerStats

    client = DockerStats("unix:///safe", timeout=1)

    def request(path: str, **kwargs: object) -> bytes:
        if path.startswith("/containers/json"):
            return b'[{"Id":"aaaaaaaaaaaa","Labels":{}}]'
        if path.endswith("/exec"):
            return b'{"Id":"bbbbbbbbbbbb"}'
        if path.endswith("/start"):
            return b""
        return b'{"Running":false,"ExitCode":1,"Error":"private-password"}'

    monkeypatch.setattr(client, "_request", request)
    assert client.get_list("/containers/json?filters=fixed") == [
        {"Id": "aaaaaaaaaaaa", "Labels": {}}
    ]
    with pytest.raises(ValueError, match=r"^The Docker observation command failed\.$"):
        client.exec("a" * 12, load_observations.POSTGRES_COMMAND)


def test_persistent_pool_reader_stops_remote_process_and_rejects_stale_cache(
    tmp_path: Path,
) -> None:
    import os
    import signal
    import sys
    import time

    class Stack:
        environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}

        def compose_command(self, *arguments: str) -> list[str]:
            assert arguments[:3] == ("exec", "-T", "web")
            return [sys.executable, "-u", "-c", arguments[-1]]

    class Stats:
        def exec(self, identifier: str, command: tuple[str, ...]) -> str:
            assert identifier == "a" * 64
            assert command[:2] == ("/bin/kill", "-TERM")
            assert command[2].isdigit()
            os.kill(int(command[2]), signal.SIGTERM)
            return ""

    reader = load_observations.PoolReader(
        cast(IsolatedComposeProject, Stack()), cast(DockerStats, Stats()), "a" * 64
    )
    reader.start()
    try:
        assert reader.pid is not None
        first = reader.snapshot()
        assert first["cpu_seconds"] is not None
        assert first["cpu_seconds"] >= 0
        assert first["peak_memory_bytes"] is not None
        assert first["peak_memory_bytes"] > 0
        assert first["pool"]["sampler_ok"] is None
        with reader.lock:
            reader.received_at = time.monotonic() - 3
        with pytest.raises(ValueError, match="stale"):
            reader.snapshot()
    finally:
        reader.close()
    assert reader.failed is False
    assert reader.process is not None and reader.process.poll() is not None
    assert not reader.thread.is_alive()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ProcessLookupError):
        os.kill(reader.pid, 0)


@pytest.mark.parametrize("pid", [True, 0, 1, -1, 2**31, "secret-pid"])
def test_persistent_reader_rejects_invalid_process_header_without_kill(pid: object) -> None:
    import io

    reader = load_observations.PoolReader(
        cast(IsolatedComposeProject, None), cast(DockerStats, None), "a" * 64
    )
    reader.process = cast(
        subprocess.Popen[str], SimpleNamespace(stdout=io.StringIO(json.dumps({"pid": pid}) + "\n"))
    )
    reader._read()
    assert reader.failed
    assert reader.pid is None


def test_pool_reader_program_rejects_unbounded_lifetime() -> None:
    from scripts.proof_pool_reader import run

    with pytest.raises(ValueError, match="900"):
        run(901)


@pytest.mark.parametrize("failure", [InterruptedError, KeyboardInterrupt])
def test_collector_startup_interrupt_stops_reader_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    class Stats:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, *args: object) -> dict[str, object]:
            return {}

    closed = []

    class Reader:
        def __init__(self, *args: object) -> None:
            pass

        def start(self) -> None:
            raise failure()

        def close(self) -> None:
            closed.append(True)

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}

    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    monkeypatch.setattr(load_observations, "PoolReader", Reader)
    observations = load_observations.LoadObservations(
        cast(IsolatedComposeProject, Stack()), tmp_path / "sample.jsonl"
    )
    observations.identifiers = {"web": "a" * 64}
    with pytest.raises(failure):
        observations.__enter__()
    assert closed == [True]
    assert observations.executor._shutdown
    assert observations._output is not None
    assert observations._output.closed
    assert not observations.thread.is_alive()


def test_pool_reader_ignores_unrelated_metric_names_and_labels() -> None:
    from scripts.proof_pool_reader import read_pool

    samples = [
        SimpleNamespace(name="size", labels={}, value=999),
        SimpleNamespace(
            name="startunnel_database_pool_available", labels={"secret": "key"}, value=7
        ),
        SimpleNamespace(name="startunnel_database_pool_waiting", labels={}, value=2),
    ]
    registry = SimpleNamespace(collect=lambda: [SimpleNamespace(samples=samples)])
    result = read_pool(cast(CollectorRegistry, registry))
    assert result["size"] is None
    assert result["available"] is None
    assert result["waiting"] == 2
    assert "secret" not in json.dumps(result)
