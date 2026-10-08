"""Release compatibility and production upgrade failure boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import release_record
from scripts import upgrade_production as upgrade


def record() -> dict[str, Any]:
    return {
        **release_record.specification("v0.1.0"),
        "schema": 1,
        "platform": "linux/amd64",
        "source_commit": "a" * 40,
        "image": "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64,
    }


@pytest.mark.parametrize("version", ["v1", "v01.2.3", "v1.2.3-rc1", "../../secret"])
def test_invalid_versions_are_rejected_before_file_access(version: str) -> None:
    with pytest.raises(ValueError, match=r"vMAJOR\.MINOR\.PATCH"):
        release_record.specification(version)


def test_manifest_must_match_reviewed_compatibility() -> None:
    candidate = record()
    upgrade.validate_record(candidate)
    candidate["supported_from"] = ["v0.0.1"]
    with pytest.raises(ValueError, match="compatibility specification"):
        upgrade.validate_record(candidate)


def test_nonofficial_image_is_rejected() -> None:
    candidate = record()
    candidate["image"] = "ghcr.io/other/startunnel@sha256:" + "b" * 64
    with pytest.raises(ValueError, match="official"):
        upgrade.validate_record(candidate)


def test_unversioned_upgrade_rejects_changed_migration_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade, "run", lambda command, **kwargs: "changed.py")
    with pytest.raises(ValueError, match="conversion required"):
        upgrade.check_compatibility(record(), "c" * 40, "")


def test_unversioned_upgrade_requires_ancestor_and_unchanged_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(upgrade, "run", run)
    upgrade.check_compatibility(record(), "c" * 40, "")
    assert commands[0] == ["git", "merge-base", "--is-ancestor", "c" * 40, "a" * 40]
    assert ":(glob)src/**/migrations/*.py" in commands[1]


def test_unlisted_version_is_rejected_even_without_migration_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade, "run", lambda command, **kwargs: "")
    with pytest.raises(ValueError, match="supported upgrade source"):
        upgrade.check_compatibility(record(), "c" * 40, "v0.0.1")


def test_configuration_edit_preserves_all_other_values() -> None:
    original = "STARTUNNEL_DOMAIN=tunnel.example\nSTARTUNNEL_APP_IMAGE=old\nOTHER=value\n"
    assert upgrade.replace_image(original, "new") == original.replace("=old", "=new")
    with pytest.raises(ValueError, match="exactly one"):
        upgrade.replace_image(original + "STARTUNNEL_APP_IMAGE=duplicate\n", "new")


@pytest.mark.parametrize("apply", [False, True])
def test_wrong_checkout_never_mutates_services_or_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apply: bool,
) -> None:
    candidate = tmp_path / "release.json"
    candidate.write_text(json.dumps(record()))
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        commands.append(command)
        return "d" * 40

    monkeypatch.setattr(upgrade, "run", run)
    args = [str(candidate)] + (["--apply", "--backup-verified"] if apply else [])
    assert upgrade.main(args) == 2
    assert commands == [["git", "rev-parse", "HEAD"]]


def test_release_workflow_gates_publication_and_does_not_inherit_provider_secrets() -> None:
    workflow = (release_record.ROOT / ".github/workflows/release.yml").read_text()
    assert "needs: validation" in workflow
    assert "needs: ci" in workflow
    assert "--draft" in workflow
    assert "--notes-file release-notes.md" in workflow
    assert "--verify-tag" in workflow
    assert "secrets: inherit" not in workflow
    assert "OPENAI" not in workflow
    assert "latest" not in workflow.replace("ubuntu-latest", "")


def test_nonancestor_blocks_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: list[str], **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(upgrade, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.check_compatibility(record(), "c" * 40, "")


@pytest.mark.parametrize(
    "mode", ["preview", "no-backup", "apply", "bad-image", "migration-failure"]
)
def test_upgrade_orchestration_preserves_data_and_stops_at_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    candidate = record()
    release_file = tmp_path / "release.json"
    release_file.write_text(json.dumps(candidate))
    old_image = "ghcr.io/btfranklin/startunnel@sha256:" + "c" * 64
    original = f"STARTUNNEL_APP_IMAGE={old_image}\nSTARTUNNEL_DOMAIN=tunnel.example\n"
    configuration = tmp_path / "production.env"
    configuration.write_text(original)
    configuration.chmod(0o600)
    monkeypatch.setattr(upgrade, "ROOT", tmp_path)
    monkeypatch.setenv("STARTUNNEL_APP_IMAGE", old_image)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "startunnel-team")
    monkeypatch.setattr(upgrade, "validate_production_secret_files", lambda directory: None)
    monkeypatch.setattr(upgrade, "rendered_images", lambda command: [candidate["image"]])
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return str(candidate["source_commit"])
        if "ps" in command:
            return "container-id"
        if command[:2] == ["docker", "inspect"]:
            return old_image
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] == old_image:
                return json.dumps({"org.opencontainers.image.revision": "d" * 40})
            return "e" * 40 if mode == "bad-image" else candidate["source_commit"]
        if "up" in command and mode == "migration-failure":
            raise subprocess.CalledProcessError(1, command)
        return ""

    monkeypatch.setattr(upgrade, "run", run)
    args = [str(release_file)]
    if mode != "preview":
        args.append("--apply")
    if mode not in ("preview", "no-backup"):
        args.append("--backup-verified")
    assert upgrade.main(args) == (0 if mode in ("preview", "apply") else 2)
    mutated = mode in ("apply", "migration-failure")
    assert configuration.read_text() == (
        original.replace(old_image, candidate["image"]) if mutated else original
    )
    if mutated:
        saved = tmp_path / "production.env.before-v0.1.0"
        assert saved.read_text() == original
        assert saved.stat().st_mode & 0o777 == 0o600
        stop_index = next(index for index, command in enumerate(commands) if "stop" in command)
        migration_index = next(index for index, command in enumerate(commands) if "rm" in command)
        startup_index = next(index for index, command in enumerate(commands) if "up" in command)
        assert stop_index < migration_index < startup_index
        assert commands[migration_index][-3:] == ["rm", "--force", "migrate"]
        assert "--no-build" in commands[startup_index]
        assert "--wait" in commands[startup_index]
    else:
        assert not any("stop" in command or "up" in command for command in commands)
    assert not any("down" in command or "--volumes" in command for command in commands)
