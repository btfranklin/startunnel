"""Load observations contain only bounded numeric service data."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from scripts import load_observations, run_full_load_profile
from scripts.compose_isolation import LaneError
from scripts.load_observations import container_counters, numeric_fields, pool_metrics
from scripts.proof_timing import PhaseTimings
from tests.load import run_profile


def test_counter_allowlists_drop_secrets_and_unknowns() -> None:
    raw = {
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
    metrics = pool_metrics(
        "startunnel_database_pool_size 18\n"
        "startunnel_database_pool_queued 4\n"
        'startunnel_database_pool_waiting{secret="key"} 6\n'
        'startunnel_database_pool_sampler_status{status="ok"} 1\n'
        'startunnel_database_pool_sampler_status{status="missing"} 2\n'
        'startunnel_database_pool_sampler_status{status="error"} 1\n'
        "startunnel_database_pool_secret 1\n"
        'query{sql="secret-sql"} 1\n'
    )
    assert metrics["size"] == 18
    assert metrics["queued"] == 4
    assert metrics["waiting"] is None
    assert metrics["sampler_ok"] == 1
    assert metrics["sampler_missing"] == 2
    assert metrics["sampler_error"] == 1
    assert "secret" not in json.dumps(metrics)


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

        def get(self, path: str) -> dict[str, Any]:
            return {"NCPU": 8, "MemTotal": 8192, "secret": "key"}

    class Stack:
        environment = {"DOCKER_HOST": "unix:///safe"}

    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "observations.jsonl")  # type: ignore[arg-type]

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


def test_metrics_probe_uses_the_service_auth_header(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io
    import urllib.request

    monkeypatch.setenv("STARTUNNEL_METRICS_TOKEN", "private-probe-token")

    def urlopen(request: urllib.request.Request, *, timeout: int) -> io.BytesIO:
        assert request.get_header("X-metrics-token") == "private-probe-token"
        assert not request.get_header("Authorization")
        assert request.full_url == "http://127.0.0.1:8000/metrics"
        assert timeout == 1
        return io.BytesIO(b"startunnel_database_pool_size 18")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(load_observations.METRICS_COMMAND, {})
    output = capsys.readouterr().out
    assert output.strip() == "startunnel_database_pool_size 18"
    assert "private-probe-token" not in output


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

    def command(stack: object, *arguments: str) -> str:
        if "psql" in arguments:
            return json.dumps({name: 1 for name in load_observations.DATABASE_FIELDS})
        return "\n".join(
            f'startunnel_database_pool_sampler_status{{status="{status}"}} {count}'
            for status, count in (("ok", ok), ("missing", missing), ("error", error))
        )

    monkeypatch.setattr(load_observations, "DockerStats", Stats)
    monkeypatch.setattr(load_observations, "_command", command)
    observations = load_observations.LoadObservations(Stack(), tmp_path / "samples.jsonl")  # type: ignore[arg-type]
    observations.identifiers = {name: "a" * 12 for name in load_observations.SERVICES}
    sample = observations._sample()
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
