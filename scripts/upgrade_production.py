"""Preview or apply a supported release to the documented production stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

if __package__:
    from .release_record import VERSION, specification
    from .start_production import (
        ROOT,
        compose_command,
        production_secret_directory,
        rendered_images,
        validate_image_reference,
        validate_production_secret_files,
        validate_rendered_images,
    )
else:
    from release_record import VERSION, specification  # type: ignore[import-not-found,no-redef]
    from start_production import (  # type: ignore[import-not-found,no-redef]
        ROOT,
        compose_command,
        production_secret_directory,
        rendered_images,
        validate_image_reference,
        validate_production_secret_files,
        validate_rendered_images,
    )


def run(command: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def validate_record(record: dict[str, Any]) -> None:
    if record.get("schema") != 1 or record.get("platform") != "linux/amd64":
        raise ValueError("Unsupported release record schema or platform.")
    if not VERSION.fullmatch(record.get("version", "")):
        raise ValueError("Invalid release version.")
    if not re.fullmatch(r"[0-9a-f]{40}", record.get("source_commit", "")):
        raise ValueError("Invalid source commit.")
    expected = specification(record["version"])
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Release record differs from the checked-out compatibility specification.")
    validate_image_reference(record.get("image", ""))
    if not record["image"].startswith("ghcr.io/btfranklin/startunnel@sha256:"):
        raise ValueError("Expected the official StarTunnel image repository.")


def check_compatibility(record: dict[str, Any], old_commit: str, old_version: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", old_commit):
        raise ValueError("Installed image needs a full source revision label or build record.")
    run(["git", "merge-base", "--is-ancestor", old_commit, record["source_commit"]])
    if old_version:
        if old_version not in record["supported_from"]:
            raise ValueError("Installed version is not listed as a supported upgrade source.")
    else:
        changed = run(
            [
                "git",
                "diff",
                "--name-only",
                old_commit,
                record["source_commit"],
                "--",
                ":(glob)src/**/migrations/*.py",
            ],
            capture=True,
        )
        if changed:
            raise ValueError("Unversioned database migration graph differs; conversion required.")


def replace_image(text: str, image: str) -> str:
    updated, count = re.subn(
        r"(?m)^STARTUNNEL_APP_IMAGE=.*$", f"STARTUNNEL_APP_IMAGE={image}", text
    )
    if count != 1:
        raise ValueError("production.env must contain exactly one STARTUNNEL_APP_IMAGE line.")
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "record", type=Path, help="release.json downloaded from the selected release"
    )
    parser.add_argument("--apply", action="store_true", help="Apply after a successful preview")
    parser.add_argument(
        "--backup-verified",
        action="store_true",
        help="Attest that a fresh backup was restored successfully and copied off the server",
    )
    args = parser.parse_args(argv)
    writers_stopped = False
    try:
        record = json.loads(args.record.read_text())
        validate_record(record)
        if run(["git", "rev-parse", "HEAD"], capture=True) != record["source_commit"]:
            raise ValueError(
                "Check out the release's exact source commit before running this helper."
            )
        if run(["git", "status", "--porcelain", "--untracked-files=no"], capture=True):
            raise ValueError("Resolve tracked source changes before upgrading.")
        env_file = ROOT / "production.env"
        if env_file.is_symlink() or stat.S_IMODE(env_file.stat().st_mode) != 0o600:
            raise ValueError("production.env must be a regular file with mode 0600.")
        text = env_file.read_text()
        old_image = validate_image_reference(os.environ.get("STARTUNNEL_APP_IMAGE", ""))
        if replace_image(text, old_image) != text:
            raise ValueError("Load production.env in this shell before running the helper.")
        if os.environ.get("COMPOSE_PROJECT_NAME") != "startunnel-team":
            raise ValueError("Load the documented startunnel-team production configuration.")
        validate_production_secret_files(production_secret_directory())
        command = compose_command(edge=True)
        container = run([*command, "ps", "--quiet", "web"], capture=True)
        if not container or "\n" in container:
            raise ValueError("Expected exactly one running web container before upgrade.")
        running_image = run(
            ["docker", "inspect", container, "--format", "{{.Config.Image}}"], capture=True
        )
        if running_image != old_image:
            raise ValueError("Running web image differs from production.env; reconcile first.")
        run(
            [
                *command,
                "exec",
                "-T",
                "web",
                "/app/.venv/bin/python",
                "manage.py",
                "migrate",
                "--check",
            ]
        )
        labels = (
            json.loads(
                run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        old_image,
                        "--format",
                        "{{json .Config.Labels}}",
                    ],
                    capture=True,
                )
            )
            or {}
        )
        old_commit = labels.get("org.opencontainers.image.revision", "")
        old_version = labels.get("org.opencontainers.image.version", "")
        check_compatibility(record, old_commit, old_version)
        new_text = replace_image(text, record["image"])
        os.environ["STARTUNNEL_APP_IMAGE"] = record["image"]
        command = compose_command(edge=True)
        run([*command, "config", "--quiet"])
        validate_rendered_images(rendered_images(command))
        print(f"Upgrade {old_version or old_commit} -> {record['version']}")
        print(f"Source: {record['source_commit']}\nImage: {record['image']}")
        print(f"Database: {record['database']}\nRollback: {record['rollback']}")
        if not args.apply:
            print("Preview passed. No configuration or services changed.")
            return 0
        if not args.backup_verified:
            raise ValueError("Applying requires --backup-verified after off-server restore proof.")
        run(["docker", "pull", record["image"]])
        revision = run(
            [
                "docker",
                "image",
                "inspect",
                record["image"],
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            ],
            capture=True,
        )
        if revision != record["source_commit"]:
            raise ValueError("New image revision label does not match the release record.")
        saved = ROOT / f"production.env.before-{record['version']}"
        with saved.open("x") as stream:
            os.chmod(saved, 0o600)
            stream.write(text)
        temporary = ROOT / "production.env.upgrade"
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            stream.write(new_text)
        run([*command, "stop", "web", "maintenance"])
        writers_stopped = True
        temporary.replace(env_file)
        # Remove the completed old migration container so startup must rerun migrations.
        run([*command, "rm", "--force", "migrate"])
        run([*command, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180"])
        print("Services started. Repeat external HTTPS, login, and two-agent verification.")
        print("Reload production.env in this shell before further Compose commands.")
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Upgrade failed: {error}")
        if writers_stopped:
            print("Application writers may be stopped. Inspect migrations before recovery.")
            print("Do not switch images against a changed database without compatibility proof.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
