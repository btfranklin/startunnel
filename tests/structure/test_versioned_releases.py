"""Release compatibility and production upgrade failure boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
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


def test_custom_registry_and_linux_arm64_release_is_accepted() -> None:
    candidate = record()
    candidate["image"] = "registry.example:5000/team/startunnel@sha256:" + "b" * 64
    candidate["platform"] = "linux/arm64"
    upgrade.validate_record(candidate)


def test_release_writer_records_custom_registry_and_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = "registry.example:5000/team/startunnel@sha256:" + "b" * 64
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["release_record.py", "--write", "v0.1.0", image, "--platform", "linux/arm64"]
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "a" * 40)
    assert release_record.main() == 0
    written = json.loads((tmp_path / "release.json").read_text())
    assert written["image"] == image
    assert written["platform"] == "linux/arm64"


def test_unversioned_upgrade_rejects_changed_migration_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upgrade, "run", lambda command, **kwargs: "changed.py")
    with pytest.raises(ValueError, match="conversion required"):
        upgrade.check_compatibility(record(), "c" * 40, "")


def test_unversioned_upgrade_requires_ancestor_and_unchanged_applied_migrations(
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
    assert "--diff-filter=DMRT" in commands[1]


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


def test_release_workflow_promotes_evidence_without_rebuilding() -> None:
    workflow = (release_record.ROOT / ".github/workflows/release.yml").read_text()
    candidate = (release_record.ROOT / ".github/workflows/release-candidate.yml").read_text()
    assert "--find-run" in workflow
    assert "--prefer-index=false" in workflow
    assert "--check-digest" in workflow
    assert "workflow_dispatch:" in workflow
    assert "docker/build-push-action" not in workflow
    assert "run_isolated_test_lane.py" not in workflow
    assert "btfranklin/release-notes-scribe@v0" in workflow
    assert 'create_release: "false"' in workflow
    assert "secrets: inherit" not in workflow + candidate
    assert "OPENAI" not in candidate
    assert candidate.count("docker/build-push-action") == 1
    ci = (release_record.ROOT / ".github/workflows/ci.yml").read_text()
    assert "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'" in ci


@pytest.mark.parametrize("summary", ["", "  \n", "  Generated release summary.\n"])
def test_release_notes_merge_keeps_reviewed_notes_and_validation_link(
    tmp_path: Path, summary: str
) -> None:
    workflow = (release_record.ROOT / ".github/workflows/release.yml").read_text()
    merge_step = workflow.split("- name: Keep reviewed deployment instructions", 1)[1].split(
        "- name: Create or recover the draft release", 1
    )[0]
    script = textwrap.dedent(
        merge_step.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    )
    notes = tmp_path / "release-notes.md"
    reviewed = (
        "Reviewed release notes.\n\n## Deployment record\n\n"
        "Source commit: `abc123`\n\nImage: `example/image@sha256:abc`"
    )
    notes.write_text(reviewed)
    environment = {
        **os.environ,
        "GENERATED_NOTES": summary,
        "GITHUB_REPOSITORY": "owner/repository",
        "VALIDATION_RUN_ID": "12345",
    }
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=environment, check=True)

    merged = notes.read_text()
    assert "Reviewed release notes." in merged
    assert "Source commit: `abc123`" in merged
    assert "Image: `example/image@sha256:abc`" in merged
    assert "Validation: https://github.com/owner/repository/actions/runs/12345" in merged
    if summary.strip():
        assert merged.startswith("Generated release summary.\n\n")
    else:
        assert not merged.startswith("Generated release summary.")


def test_release_summary_step_is_optional_and_uses_only_successful_output() -> None:
    workflow = (release_record.ROOT / ".github/workflows/release.yml").read_text()
    scribe_step = workflow.split("- name: Generate the change summary", 1)[1].split(
        "- name: Keep reviewed deployment instructions", 1
    )[0]
    assert "GENERATE_RELEASE_SUMMARY: ${{ secrets.OPENAI_API_KEY != '' }}" in workflow
    assert "if: env.GENERATE_RELEASE_SUMMARY == 'true'" in scribe_step
    assert "continue-on-error: true" in scribe_step
    assert "openai_api_key: ${{ secrets.OPENAI_API_KEY }}" in scribe_step
    assert (
        "GENERATED_NOTES: ${{ steps.notes.outcome == 'success' && "
        "steps.notes.outputs.release_notes || '' }}" in workflow
    )
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" not in workflow


def test_nonancestor_blocks_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: list[str], **kwargs: Any) -> str:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(upgrade, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.check_compatibility(record(), "c" * 40, "")


@pytest.mark.parametrize(
    "mode",
    [
        "preview",
        "no-backup",
        "apply",
        "apply-link",
        "bad-image",
        "migration-failure",
        "wrong-compose-image",
        "absent-web",
        "mismatched-web",
        "custom-preview",
        "custom-apply",
    ],
)
def test_upgrade_orchestration_preserves_data_and_stops_at_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    candidate = record()
    candidate["image"] = "registry.example:5000/team/startunnel@sha256:" + "b" * 64
    release_file = tmp_path / "release.json"
    release_file.write_text(json.dumps(candidate))
    old_image = "ghcr.io/btfranklin/startunnel@sha256:" + "c" * 64
    original = f"STARTUNNEL_APP_IMAGE={old_image}\nSTARTUNNEL_DOMAIN=tunnel.example\n"
    custom = mode.startswith("custom-")
    configuration = tmp_path / ("team.env" if custom else "production.env")
    if mode == "apply-link" or custom:
        managed = tmp_path / "managed"
        managed.mkdir()
        target = managed / configuration.name
        target.write_text(original)
        target.chmod(0o400)
        configuration.symlink_to(target)
    else:
        configuration.write_text(original)
        configuration.chmod(0o600)
    monkeypatch.setattr(upgrade, "ROOT", tmp_path)
    monkeypatch.setenv("STARTUNNEL_APP_IMAGE", old_image)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "customer-team" if custom else "startunnel-team")
    if custom:
        monkeypatch.setenv("COMPOSE_FILE", "/srv/team/base.yaml:/srv/team/production.yaml")
        monkeypatch.setenv("COMPOSE_PROFILES", "metrics")
    else:
        monkeypatch.delenv("COMPOSE_FILE", raising=False)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    monkeypatch.setattr(upgrade, "validate_production_secret_files", lambda directory: None)
    monkeypatch.setattr(upgrade, "rendered_images", lambda command: [candidate["image"]])
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return str(candidate["source_commit"])
        if command[-3:] == ["config", "--format", "json"]:
            rendered = os.environ["STARTUNNEL_APP_IMAGE"]
            if mode == "wrong-compose-image" and rendered == candidate["image"]:
                rendered = old_image
            return json.dumps({"services": {"web": {"image": rendered}}})
        if "ps" in command:
            assert command[-3:] == ["--all", "--quiet", "web"]
            return "" if mode == "absent-web" else "container-id"
        if command[:2] == ["docker", "inspect"]:
            return "other-image" if mode == "mismatched-web" else old_image
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] == old_image:
                return json.dumps({"org.opencontainers.image.revision": "d" * 40})
            return "e" * 40 if mode == "bad-image" else str(candidate["source_commit"])
        if "up" in command and mode == "migration-failure":
            raise subprocess.CalledProcessError(1, command)
        return ""

    monkeypatch.setattr(upgrade, "run", run)
    args = [str(release_file)] + (["--env-file", str(configuration)] if custom else [])
    if mode not in ("preview", "custom-preview"):
        args.append("--apply")
    if mode not in ("preview", "custom-preview", "no-backup"):
        args.append("--backup-verified")
    assert upgrade.main(args) == (
        0
        if mode
        in (
            "preview",
            "apply",
            "apply-link",
            "absent-web",
            "custom-preview",
            "custom-apply",
        )
        else 2
    )
    mutated = mode in (
        "apply",
        "apply-link",
        "migration-failure",
        "absent-web",
        "custom-apply",
    )
    assert configuration.read_text() == (
        original.replace(old_image, candidate["image"]) if mutated else original
    )
    if mutated:
        saved = configuration.resolve().with_name(f"{configuration.name}.before-v0.1.0")
        assert saved.read_text() == original
        assert saved.stat().st_mode & 0o777 == 0o600
        if mode == "apply-link" or custom:
            assert configuration.is_symlink()
        stop_index = next(index for index, command in enumerate(commands) if "stop" in command)
        migration_index = next(index for index, command in enumerate(commands) if "rm" in command)
        startup_index = next(index for index, command in enumerate(commands) if "up" in command)
        assert stop_index < migration_index < startup_index
        assert commands[migration_index][-3:] == ["rm", "--force", "migrate"]
        assert "--no-build" in commands[startup_index]
        assert "--wait" in commands[startup_index]
    else:
        assert not any("stop" in command or "up" in command for command in commands)
    if mode != "mismatched-web":
        migration_check = next(
            command for command in commands if "migrate" in command and "--check" in command
        )
        assert migration_check[-6:] == [
            "--no-deps",
            "web",
            "/app/.venv/bin/python",
            "manage.py",
            "migrate",
            "--check",
        ]
        assert "--rm" in migration_check
        assert "exec" not in migration_check
    else:
        assert not any("--check" in command for command in commands)
    assert not any("down" in command or "--volumes" in command for command in commands)
    if custom:
        compose_calls = [command for command in commands if command[:2] == ["docker", "compose"]]
        assert compose_calls
        assert all(
            command[2:4] == ["--env-file", str(configuration.resolve())]
            for command in compose_calls
        )
        assert all("-f" not in command and "--profile" not in command for command in compose_calls)
