"""Automatic image cleanup stays within owned disposable build outputs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence

import pytest
from scripts import compose_isolation as isolation


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="Compose rendering is checked on the host."
)
def test_test_overlay_labels_every_build_and_no_pulled_images() -> None:
    command = ["docker", "compose", "--env-file", "/dev/null", "-f", "compose.yaml"]
    for overlay in (False, True):
        result = subprocess.run(
            [
                *command,
                *(["-f", "compose.test.yaml"] if overlay else []),
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            cwd=isolation.ROOT,
            env=isolation.safe_environment(os.environ),
            text=True,
            capture_output=True,
            check=True,
        )
        services = json.loads(result.stdout)["services"]
        for definition in services.values():
            build = definition.get("build")
            if build and overlay:
                assert build["labels"]["net.startunnel.disposable-build"] == "true"
            else:
                assert "net.startunnel.disposable-build" not in (build or {}).get("labels", {})
            assert "net.startunnel.disposable-build" not in definition.get("labels", {})


def test_failed_lane_prunes_owned_dangling_images_before_and_after_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    monkeypatch.setattr(isolation, "assert_local_docker", lambda env: events.append("local"))
    monkeypatch.setattr(
        isolation, "assert_project_unused", lambda name, env: events.append("unused")
    )

    def run(
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout_seconds: int,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert environment == {}
        if "prune" in command:
            assert list(command) == [
                "docker",
                "image",
                "prune",
                "--force",
                "--filter",
                "label=net.startunnel.disposable-build=true",
            ]
            assert capture_output and timeout_seconds == 180
            events.append("prune")
        else:
            assert "down" in command
            assert "--volumes" in command
            assert command[command.index("--project-name") + 1] == "sttest-storage-12345678"
            events.append("down")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(isolation, "_run_process", run)
    with (
        pytest.raises(RuntimeError, match="test lane failure"),
        isolation.IsolatedComposeProject("sttest-storage-12345678", source_environment={}),
    ):
        events.append("lane")
        raise RuntimeError("test lane failure")
    assert events == ["local", "unused", "prune", "lane", "down", "prune"]


@pytest.mark.parametrize("failed_check", ["assert_local_docker", "assert_project_unused"])
def test_rejected_target_never_prunes_images(
    monkeypatch: pytest.MonkeyPatch, failed_check: str
) -> None:
    monkeypatch.setattr(isolation, "assert_local_docker", lambda env: None)
    monkeypatch.setattr(isolation, "assert_project_unused", lambda name, env: None)

    def reject(*args: object) -> None:
        raise isolation.IsolationError("The target is not safe.")

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Docker must not mutate a rejected target.")

    monkeypatch.setattr(isolation, failed_check, reject)
    monkeypatch.setattr(isolation, "_run_process", unexpected)
    with (
        pytest.raises(isolation.IsolationError, match="not safe"),
        isolation.IsolatedComposeProject("sttest-storage-12345678", source_environment={}),
    ):
        pytest.fail("The rejected project must not start.")


@pytest.mark.parametrize(
    "failure",
    [
        OSError("test process error"),
        subprocess.TimeoutExpired("docker", 180),
        "Docker is not available.",
        "permission denied: a prune operation is already running",
    ],
)
def test_image_cleanup_reports_errors_without_ignoring_them(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | str
) -> None:
    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(command, 1, "", failure)

    monkeypatch.setattr(isolation, "_run_process", run)
    with pytest.raises(isolation.IsolationError, match="Could not remove unused"):
        isolation.prune_disposable_images({})


def test_concurrent_daemon_cleanup_is_a_narrow_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "Error response from daemon: a prune operation is already running\n"
        )

    monkeypatch.setattr(isolation, "_run_process", run)
    isolation.prune_disposable_images({})
