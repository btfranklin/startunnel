"""Automatic image cleanup stays within owned disposable build outputs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

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


def test_failed_lane_removes_its_project_after_preflight_dangling_prune(
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
        tag = environment["STARTUNNEL_PROOF_IMAGE_TAG"]
        assert tag.startswith("sttest-storage-12345678-")
        assert environment["STARTUNNEL_APP_IMAGE"] == f"startunnel-app:{tag}"
        runtime = {
            key: value
            for key, value in environment.items()
            if key not in {"STARTUNNEL_PROOF_IMAGE_TAG", "STARTUNNEL_APP_IMAGE"}
        }
        assert runtime in (
            {},
            {
                "STARTUNNEL_CONTAINER_UID": str(
                    (os.getuid() or 10001) if sys.platform == "linux" else 10001
                ),
                "STARTUNNEL_CONTAINER_GID": str(
                    (os.getgid() or 10001) if sys.platform == "linux" else 10001
                ),
            },
        )
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
    assert events == ["local", "unused", "prune", "lane", "down"]


@pytest.mark.parametrize("ownership", ["owned", "moved", "missing"])
def test_image_removal_checks_the_recorded_tag_and_id(
    monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    tag = "startunnel-test:sttest-alpha-12345678"
    image = "sha256:" + "a" * 64
    commands: list[Sequence[str]] = []

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert command[-1] == tag
        if "inspect" in command:
            actual = "sha256:" + "b" * 64 if ownership == "moved" else image
            return subprocess.CompletedProcess(command, int(ownership == "missing"), actual, "")
        assert "rm" in command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(isolation, "_run_process", run)
    if ownership == "owned":
        isolation.remove_owned_image(tag, image, {})
        assert commands[-1] == ["docker", "image", "rm", tag]
    else:
        with pytest.raises(isolation.IsolationError, match="ownership"):
            isolation.remove_owned_image(tag, image, {})
        assert len(commands) == 1


def test_two_projects_select_unique_source_tags_and_reuse_is_explicit() -> None:
    first = isolation.IsolatedComposeProject("sttest-alpha-12345678", source_environment={})
    second = isolation.IsolatedComposeProject("sttest-beta-12345678", source_environment={})
    reused = isolation.IsolatedComposeProject(
        "sttest-reuse-12345678", reuse_current_images=True, source_environment={}
    )
    assert first.environment["STARTUNNEL_APP_IMAGE"] != second.environment["STARTUNNEL_APP_IMAGE"]
    assert (
        first.environment["STARTUNNEL_PROOF_IMAGE_TAG"]
        != second.environment["STARTUNNEL_PROOF_IMAGE_TAG"]
    )
    assert "STARTUNNEL_APP_IMAGE" not in reused.environment
    assert "STARTUNNEL_PROOF_IMAGE_TAG" not in reused.environment


@pytest.mark.parametrize("matches", [True, False])
def test_started_containers_are_checked_against_their_build_ids(
    monkeypatch: pytest.MonkeyPatch, matches: bool
) -> None:
    stack = isolation.IsolatedComposeProject("sttest-alpha-12345678", source_environment={})
    tag = stack.environment["STARTUNNEL_APP_IMAGE"]
    image = "sha256:" + "a" * 64
    stack._owned_images[tag] = image

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[-1] == "label=com.docker.compose.project=sttest-alpha-12345678"
        return subprocess.CompletedProcess(command, 0, "local-container\n", "")

    def inspect(command: Sequence[str], **kwargs: object) -> str:
        assert command[-1] == "local-container"
        started = image if matches else "sha256:" + "b" * 64
        return f"{tag} {started}"

    monkeypatch.setattr(isolation, "_run_process", run)
    monkeypatch.setattr(isolation, "required_output", inspect)
    if matches:
        stack.verify_started_images()
    else:
        with pytest.raises(isolation.LaneError, match="did not use owned image"):
            stack.verify_started_images()


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


@pytest.mark.parametrize("linked", [False, True])
def test_source_helpers_can_write_private_host_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, linked: bool
) -> None:
    target = tmp_path / "reports" if linked else tmp_path / "artifacts"
    if linked:
        target.mkdir(mode=0o700)
        (tmp_path / "artifacts").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(isolation, "ROOT", tmp_path)
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    with isolation.IsolatedComposeProject(
        "sttest-browser-artifacts", profiles=("browser",), source_environment={}
    ) as stack:
        expected_uid = (os.getuid() or 10001) if sys.platform == "linux" else 10001
        expected_gid = (os.getgid() or 10001) if sys.platform == "linux" else 10001
        assert stack.environment["STARTUNNEL_CONTAINER_UID"] == str(expected_uid)
        assert stack.environment["STARTUNNEL_CONTAINER_GID"] == str(expected_gid)
        artifacts = tmp_path / "artifacts"
        assert artifacts.stat().st_mode & 0o777 == 0o700
        assert artifacts.stat().st_uid == (expected_uid if os.getuid() == 0 else os.getuid())
        (artifacts / "proof.json").write_text("completed")
    assert (target / "proof.json").read_text() == "completed"


def test_docker_desktop_helpers_do_not_use_the_macos_staff_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(isolation, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(isolation, "assert_local_docker", lambda environment: "unix:///safe")
    monkeypatch.setattr(isolation, "assert_project_unused", lambda *args: None)
    monkeypatch.setattr(isolation, "prune_disposable_images", lambda environment: None)
    monkeypatch.setattr(isolation.IsolatedComposeProject, "cleanup", lambda self: None)
    with isolation.IsolatedComposeProject(
        "sttest-desktop-artifacts", source_environment={}
    ) as stack:
        assert stack.environment["STARTUNNEL_CONTAINER_UID"] == "10001"
        assert stack.environment["STARTUNNEL_CONTAINER_GID"] == "10001"
