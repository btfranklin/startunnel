"""Create an encrypted backup of one consistent production installation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .production_lock import ProductionLockBusy, production_lock
    from .start_production import (
        PRODUCTION_SECRET_NAMES,
        ROOT,
        compose_command,
        production_secret_directory,
        render_model,
        validate_production_model,
        validate_production_secret_files,
    )
else:
    from production_lock import ProductionLockBusy, production_lock
    from start_production import (
        PRODUCTION_SECRET_NAMES,
        ROOT,
        compose_command,
        production_secret_directory,
        render_model,
        validate_production_model,
        validate_production_secret_files,
    )


def capture(command: list[str]) -> str:
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def running_identity(
    command: list[str], images: Mapping[str, tuple[str, str]], source: str
) -> dict[str, list[str]]:
    running: dict[str, list[str]] = {}
    for service, (image, _platform) in images.items():
        containers = capture(
            [*command, "ps", "--status", "running", "--quiet", service]
        ).splitlines()
        if not containers and service in {"web", "maintenance", "postgres"}:
            raise ValueError(f"Required production service {service!r} is stopped.")
        for container in containers:
            reference, image_id = capture(
                ["docker", "inspect", container, "--format", "{{.Config.Image}} {{.Image}}"]
            ).split()
            if reference != image:
                raise ValueError(
                    f"Running image for {service!r} differs from selected configuration."
                )
            running.setdefault(service, []).append(image_id)
            if service == "web":
                revision = capture(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image_id,
                        "--format",
                        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                    ]
                )
                if revision != source:
                    raise ValueError("Checkout does not match the running web image revision.")
        if service in running:
            running[service].sort()
    return running


def backup(output_directory: Path, recipients: Path, env_file: Path) -> Path:
    directory = production_secret_directory()
    command = compose_command(edge=False, env_file=env_file)
    with (
        production_lock(directory),
        tempfile.TemporaryDirectory(dir=output_directory, prefix=".work.") as temporary,
    ):
        validate_production_secret_files(directory)
        work = Path(temporary)
        source = capture(["git", "rev-parse", "HEAD"])
        if capture(["git", "status", "--porcelain", "--untracked-files=no"]):
            raise ValueError("Resolve tracked source changes before taking a production backup.")
        image = os.environ.get("STARTUNNEL_APP_IMAGE", "")
        model = render_model(command)
        images = validate_production_model(model, image=image, secret_directory=directory)
        running = running_identity(command, images, source)
        config = work / "config"
        config.mkdir()
        compose_files = (
            os.environ.get("COMPOSE_FILE") or f"compose.yaml{os.pathsep}compose.prod.yaml"
        )
        selected = [ROOT / path for path in compose_files.split(os.pathsep)]
        captured: dict[Path, Path] = {env_file: work / "production.env"}
        for index, path in enumerate(selected):
            captured[path] = config / f"{index:02d}-{path.name}"
        (work / "secrets").mkdir()
        for name in PRODUCTION_SECRET_NAMES:
            captured[directory / name] = work / "secrets" / name
        for path, copy in captured.items():
            shutil.copyfile(path, copy)
        (work / "source-commit").write_text(source + "\n")
        (work / "installation.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "compose_files": [
                        {"original": str(path), "archive": str(captured[path].relative_to(work))}
                        for path in selected
                    ],
                    "profiles": os.environ.get("COMPOSE_PROFILES", ""),
                    "service_images": images,
                    "running_image_ids": running,
                },
                indent=2,
            )
            + "\n"
        )
        dump = work / "database.backup"
        subprocess.run(
            [str(ROOT / "scripts/backup-startunnel.sh"), str(dump)],
            cwd=ROOT,
            env=os.environ
            | {
                "COMPOSE_FILE": compose_files,
                "STARTUNNEL_BACKUP_ENV_FILE": str(env_file),
            },
            check=True,
        )
        if (
            capture(["git", "rev-parse", "HEAD"]) != source
            or model != render_model(command)
            or running_identity(command, images, source) != running
            or any(path.read_bytes() != copy.read_bytes() for path, copy in captured.items())
        ):
            raise ValueError("Installation changed during backup capture. Retry after maintenance.")
        archive = work / "archive.tar"
        with tarfile.open(archive, "w") as bundle:
            for member in sorted(work.iterdir()):
                if member != archive:
                    bundle.add(member, arcname=member.name)
        encrypted = work / "archive.tar.age"
        subprocess.run(
            ["age", "-R", str(recipients), "-o", str(encrypted), str(archive)],
            cwd=ROOT,
            check=True,
        )
        final = output_directory / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ.tar.age")
        os.link(encrypted, final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--recipients", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / "production.env")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        path = backup(
            args.output_directory.resolve(strict=True),
            args.recipients.resolve(strict=True),
            args.env_file.resolve(strict=True),
        )
    except ProductionLockBusy as error:
        print(f"Backup deferred: {error}")
        marker = args.output_directory / ".off-host-verified"
        try:
            verified_at = datetime.fromtimestamp(marker.stat().st_mtime, UTC)
        except OSError:
            print("The age of the last verified off-host copy is unknown.")
        else:
            hours = max(0, (datetime.now(UTC) - verified_at).total_seconds()) / 3600
            print(f"The last off-host verification was {hours:.1f} hours ago.")
        return 2
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Backup failed: {error}")
        return 2
    print(f"Created encrypted backup: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
