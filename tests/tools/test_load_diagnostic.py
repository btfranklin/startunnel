"""Diagnostic locks are bounded and their child process always stops."""

from __future__ import annotations

import io
import selectors
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import load_diagnostic


@pytest.mark.parametrize("raises", [False, True])
def test_diagnostic_lock_stops_child_on_normal_and_failed_workload(
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    class Process:
        stdout = io.StringIO("locked\n")
        terminated = False
        waited = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: int) -> None:
            assert timeout == 5
            self.waited = True

    process = Process()
    commands: list[list[str]] = []

    def popen(command: list[str], **kwargs: Any) -> Process:
        assert kwargs["stderr"] == subprocess.DEVNULL
        commands.append(command)
        return process

    class Selector:
        def __enter__(self) -> Selector:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def register(self, *args: object) -> None:
            pass

        def select(self, *, timeout: int) -> list[int]:
            assert timeout == 10
            return [1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(selectors, "DefaultSelector", Selector)
    if raises:
        with pytest.raises(InterruptedError), load_diagnostic.DiagnosticLock(5, Path("/safe")):
            raise InterruptedError("stop")
    else:
        with load_diagnostic.DiagnosticLock(5, Path("/safe")):
            pass
    assert process.terminated
    assert process.waited
    assert process.stdout.closed
    assert commands[0][-1] == "5"
    assert commands[0][-2] == load_diagnostic.LOCK_PROGRAM
    assert "statement_timeout" in load_diagnostic.LOCK_PROGRAM
    assert "ACCESS EXCLUSIVE" in load_diagnostic.LOCK_PROGRAM
    assert "connect_timeout=5" in load_diagnostic.LOCK_PROGRAM


def test_diagnostic_lock_rejects_unbounded_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_start(*args: object, **kwargs: object) -> None:
        raise AssertionError("A process must not start.")

    monkeypatch.setattr(subprocess, "Popen", must_not_start)
    with pytest.raises(ValueError), load_diagnostic.DiagnosticLock(61, Path("/safe")):
        pass
    with load_diagnostic.DiagnosticLock(0, Path("/safe")):
        pass


@pytest.mark.parametrize("broken", [None, "cleanup", "no_wait", "no_error"])
def test_diagnostic_validation_requires_failure_waits_and_cleanup(
    tmp_path: Path, broken: str | None
) -> None:
    import json

    from scripts.validate_load_diagnostic import validate

    project = "stload-lock-diagnostic"
    run = {
        "status": "failed",
        "release_evidence_eligible": False,
        "disposable_volumes_removed": broken != "cleanup",
        "workload_report": "workload.json",
        "observations_file": "observations.jsonl",
    }
    workload = {
        "fixture_cleanup_complete": True,
        "configuration": {"duration_seconds": 300},
        "results": {
            "expected_sends": 15000,
            "failures_by_status": {} if broken == "no_error" else {"503": 1},
        },
    }
    sample = {
        "postgres": {"lock_waiters": 0 if broken == "no_wait" else 7},
        "pool": {"requests_wait_ms": 100},
    }
    (tmp_path / f"load-run-{project}.json").write_text(json.dumps(run))
    (tmp_path / "workload.json").write_text(json.dumps(workload))
    (tmp_path / "observations.jsonl").write_text(json.dumps(sample) + "\n")
    if broken:
        with pytest.raises(ValueError):
            validate(tmp_path, project)
    else:
        assert validate(tmp_path, project) == {"peak_lock_waiters": 7, "peak_pool_wait_ms": 100}
