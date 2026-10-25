"""Helper comparisons use fixed inputs and keep failed diagnostic reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts import benchmark_proof_helpers as helpers
from scripts.compose_isolation import LaneError
from scripts.proof_timing import PhaseTimings


def test_comparison_uses_cold_full_cold_focused_then_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    class Stack:
        def build_services(
            self,
            services: tuple[str, ...],
            *,
            no_cache: bool,
            timeout_seconds: int,
        ) -> dict[str, object]:
            assert timeout_seconds == 2400
            calls.append((services, no_cache))
            return {
                "source_revision": "a" * 40,
                "platform": "linux/amd64",
                "hashes": {"pdm.lock": "b" * 64},
                "image_ids": {services[0]: "sha256:" + "c" * 64},
                "duration_seconds": 10 if no_cache else 1,
                "cache_hits": 0 if no_cache else 4,
                "provenance_file": f"proof-build-20261024T12000{len(calls)}.000000Z.json",
            }

    sizes = iter((1000, 100, 100))
    monkeypatch.setattr(
        helpers,
        "_image",
        lambda *args: {
            "source_revision": "a" * 40,
            "platform": "linux/amd64",
            "size_bytes": next(sizes),
        },
    )
    preflights: list[str] = []
    monkeypatch.setattr(helpers, "_preflight", lambda stack: preflights.append("passed"))
    report: dict[str, Any] = {}
    helpers.benchmark(Stack(), report, PhaseTimings())  # type: ignore[arg-type]
    assert calls == [(("tests",), True), (("load-profile",), True), (("load-profile",), False)]
    assert len(preflights) == 2
    assert report["comparison"]["focused_image_smaller"] is True
    assert report["comparison"]["warm_cache_hits"] == 4


@pytest.mark.parametrize("failure", ["inputs", "cache", "size"])
def test_comparison_rejects_changed_inputs_and_unproven_cache(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    calls = 0

    class Stack:
        def build_services(self, services: tuple[str, ...], **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "source_revision": "a" * 40 if calls == 1 or failure != "inputs" else "b" * 40,
                "platform": "linux/amd64",
                "hashes": {"pdm.lock": "c" * 64},
                "image_ids": {services[0]: "sha256:" + "d" * 64},
                "cache_hits": 0 if failure == "cache" else 1,
                "provenance_file": f"proof-build-20261024T12000{calls}.000000Z.json",
            }

    sizes = iter((1000, 2000 if failure == "size" else 100, 100))
    monkeypatch.setattr(
        helpers,
        "_image",
        lambda *args: {
            "source_revision": "a" * 40,
            "platform": "linux/amd64",
            "size_bytes": next(sizes),
        },
    )
    monkeypatch.setattr(helpers, "_preflight", lambda *args: None)
    with pytest.raises(LaneError):
        helpers.benchmark(Stack(), {}, PhaseTimings())  # type: ignore[arg-type]


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_benchmark_failure_writes_summary_after_cleanup_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: bool,
) -> None:
    project = "sttest-helper-benchmark-12345678"
    path = tmp_path / "artifacts" / f"proof-helper-benchmark-{project}" / "benchmark-summary.json"
    events: list[str] = []

    class Stack:
        cleanup_complete = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> Stack:
            return self

        def __exit__(self, *args: object) -> None:
            events.append("cleanup")
            assert not path.exists()
            if cleanup_error:
                raise LaneError("cleanup", "private-key")
            self.cleanup_complete = True

    def fail(*args: object) -> None:
        raise LaneError("build", "private-key")

    monkeypatch.setattr(helpers, "ROOT", tmp_path)
    monkeypatch.setattr(helpers, "IsolatedComposeProject", Stack)
    monkeypatch.setattr(helpers, "benchmark", fail)
    assert helpers.main(["--project-name", project]) == 1
    assert events == ["cleanup"]
    report = json.loads(path.read_text())
    assert report["status"] == "failed"
    assert report["release_evidence_eligible"] is False
    assert report["disposable_volumes_removed"] is not cleanup_error
    assert "private-key" not in path.read_text()


def test_focused_preflight_uses_owned_compose_oneoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Stack:
        def run(self, step: str, *command: str, timeout_seconds: int) -> None:
            assert timeout_seconds == 60
            calls.append(command)

    helpers._preflight(Stack())  # type: ignore[arg-type]
    command = calls[0]
    assert command[:3] == ("run", "--rm", "--no-deps")
    assert command[-1] == helpers.FOCUSED_PREFLIGHT
    assert "load-profile" in command
    assert "pytest" in helpers.FOCUSED_PREFLIGHT


def test_benchmark_rejects_unsafe_project_before_creating_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helpers, "ROOT", tmp_path)
    assert helpers.main(["--project-name", "../../unsafe"]) == 2
    assert not (tmp_path / "artifacts").exists()
