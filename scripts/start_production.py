"""Start only a reviewed immutable production image."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+(?:/[^\s@]+)*@sha256:[0-9a-f]{64}$")
PRODUCTION_SECRET_NAMES = (
    "startunnel_secret_key",
    "startunnel_api_key_pepper",
    "startunnel_address_secret",
    "startunnel_address_derivation_secret",
    "startunnel_idempotency_secret",
    "startunnel_metrics_token",
    "postgres_admin_password",
    "postgres_runtime_password",
)
MAX_SECRET_FILE_BYTES = 8_192


def validate_image_reference(value: str, *, name: str = "STARTUNNEL_APP_IMAGE") -> str:
    """Require one registry reference pinned to a SHA-256 digest."""

    if not IMMUTABLE_IMAGE.fullmatch(value):
        raise ValueError(f"{name} must be an image reference pinned with @sha256:.")
    return value


def rendered_images(command: list[str]) -> list[str]:
    """Read the actual enabled service images from the merged Compose model."""

    result = subprocess.run(
        [*command, "config", "--images"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    images = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not images:
        raise ValueError("The production Compose model contains no service images.")
    return images


def validate_rendered_images(images: list[str]) -> None:
    for image in images:
        validate_image_reference(image, name="Each enabled production service image")


def production_secret_directory(value: str | None = None) -> Path:
    """Resolve the production secret directory as Compose resolves it."""

    configured = value if value is not None else os.environ.get("STARTUNNEL_SECRETS_DIR")
    path = Path(configured or "secrets")
    return path if path.is_absolute() else ROOT / path


def read_validated_secret_file(path: Path, *, name: str | None = None) -> str:
    """Read one small text secret after strict filesystem checks."""

    label = name or path.name
    try:
        path_status = path.lstat()
    except OSError as error:
        raise ValueError(f"Secret source {label!r} is not an accessible regular file.") from error
    if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
        raise ValueError(f"Secret source {label!r} must be a regular non-symlink file.")
    if stat.S_IMODE(path_status.st_mode) != 0o600:
        raise ValueError(f"Secret source {label!r} must have mode 0600.")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"Secret source {label!r} could not be opened safely.") from error
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise ValueError(f"Secret source {label!r} must be a regular file.")
        if stat.S_IMODE(opened_status.st_mode) != 0o600:
            raise ValueError(f"Secret source {label!r} must have mode 0600.")
        data = os.read(descriptor, MAX_SECRET_FILE_BYTES + 1)
    finally:
        os.close(descriptor)

    if len(data) > MAX_SECRET_FILE_BYTES:
        raise ValueError(f"Secret source {label!r} is too large.")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Secret source {label!r} must contain UTF-8 text.") from error
    value = value.rstrip("\r\n")
    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"Secret source {label!r} must contain one nonempty text value.")
    return value


def validate_production_secret_files(directory: Path) -> None:
    """Fail closed unless all enabled production secret sources are safe."""

    try:
        directory_status = directory.lstat()
    except OSError as error:
        raise ValueError("The production secret directory is not accessible.") from error
    if stat.S_ISLNK(directory_status.st_mode) or not stat.S_ISDIR(directory_status.st_mode):
        raise ValueError("The production secret directory must be a non-symlink directory.")
    for name in PRODUCTION_SECRET_NAMES:
        read_validated_secret_file(directory / name, name=name)


def compose_command(*, edge: bool) -> list[str]:
    command = [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.prod.yaml",
    ]
    if edge:
        command.extend(["--profile", "edge"])
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge", action="store_true", help="Start the included Caddy TLS edge.")
    args = parser.parse_args(argv)
    command = compose_command(edge=args.edge)
    try:
        validate_production_secret_files(production_secret_directory())
        validate_image_reference(os.environ.get("STARTUNNEL_APP_IMAGE", ""))
        subprocess.run([*command, "config", "--quiet"], cwd=ROOT, check=True)
        validate_rendered_images(rendered_images(command))
    except (subprocess.CalledProcessError, ValueError) as error:
        print(str(error))
        return 2

    subprocess.run([*command, "up", "-d", "--no-build"], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
