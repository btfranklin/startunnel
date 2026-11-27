"""Start only a reviewed immutable production image."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from ipaddress import IPv4Network
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .json_data import json_array, json_object, json_string
else:
    from json_data import json_array, json_object, json_string

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+(?:/[^\s@]+)*@sha256:[0-9a-f]{64}$")
PRODUCTION_SECRET_NAMES = (
    "startunnel_secret_key",
    "startunnel_api_key_pepper",
    "startunnel_idempotency_secret",
    "startunnel_metrics_token",
    "postgres_admin_password",
    "postgres_runtime_password",
)
MAX_SECRET_FILE_BYTES = 8_192
APP_SERVICES = ("web", "migrate", "maintenance")
APP_PLATFORM = "linux/amd64"
ADMIN_SECRET = "postgres_admin_password"
RUNTIME_SECRET = "postgres_runtime_password"


def render_model(command: list[str]) -> Mapping[str, object]:
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json_object(json.loads(result.stdout))


def validate_production_model(
    model: Mapping[str, object],
    *,
    image: str,
    secret_directory: Path,
    platform: str = APP_PLATFORM,
) -> dict[str, tuple[str, str]]:
    services = json_object(model.get("services"))
    secrets = json_object(model.get("secrets"))
    sources = set(PRODUCTION_SECRET_NAMES)
    contracts = {
        "postgres": {ADMIN_SECRET, RUNTIME_SECRET},
        "web": sources - {ADMIN_SECRET},
        "migrate": sources - {RUNTIME_SECRET},
        "maintenance": sources - {RUNTIME_SECRET},
    }
    for name in PRODUCTION_SECRET_NAMES:
        source = json_object(secrets.get(name)).get("file")
        if (
            not isinstance(source, str)
            or Path(source).resolve() != (secret_directory / name).resolve()
        ):
            raise ValueError(f"Production secret {name!r} has the wrong file source.")
    images: dict[str, tuple[str, str]] = {}
    for name, value in services.items():
        service = json_object(value)
        reference = validate_image_reference(
            json_string(service.get("image")), name=f"Image for {name!r}"
        )
        service_platform = json_string(service.get("platform", ""))
        images[name] = reference, service_platform
        if name not in contracts:
            continue
        if name in APP_SERVICES:
            environment = json_object(service.get("environment"))
            if (
                reference != image
                or service_platform not in {"", platform}
                or service.get("build")
                or environment.get("DJANGO_SETTINGS_MODULE") != "startunnel.settings.production"
                or environment.get("STARTUNNEL_ENV") != "production"
                or environment.get("DATABASE_URL", "")
                or environment.get("DATABASE_ADMIN_URL", "")
                or environment.get("STARTUNNEL_DATABASE_ROLE", "runtime")
                != ("runtime" if name == "web" else "admin")
            ):
                raise ValueError(f"Invalid production settings, image, or platform for {name!r}.")
        mounted: set[str] = set()
        for item in json_array(service.get("secrets", [])):
            if isinstance(item, str):
                mounted.add(item)
            else:
                mount = json_object(item)
                secret = json_string(mount.get("source"))
                if mount.get("target", secret) not in (secret, f"/run/secrets/{secret}"):
                    raise ValueError(f"Invalid production secret target for {name!r}.")
                mounted.add(secret)
        if mounted != contracts[name]:
            raise ValueError(f"Invalid production secret mounts for {name!r}.")
    if not contracts.keys() <= images.keys():
        raise ValueError("Required production services are missing.")
    return images


def validate_proxy_prefix(value: str) -> str:
    """Require three IPv4 octets for the dedicated proxy network."""

    try:
        network = IPv4Network(f"{value}.0/24", strict=True)
    except ValueError as error:
        raise ValueError("STARTUNNEL_PROXY_IPV4_PREFIX must contain three IPv4 octets.") from error
    private = any(
        network.subnet_of(IPv4Network(cidr))
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    if str(network.network_address).rsplit(".", 1)[0] != value or not private:
        raise ValueError("STARTUNNEL_PROXY_IPV4_PREFIX must select a private IPv4 network.")
    return value


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


def production_secret_directory(*, root: Path = ROOT) -> Path:
    """Resolve the production secret directory as Compose resolves it."""

    configured = os.environ.get("STARTUNNEL_SECRETS_DIR")
    path = Path(configured or "secrets")
    if path.is_absolute():
        return path
    compose_files = os.environ.get("COMPOSE_FILE")
    first_file = (
        Path(compose_files.split(os.pathsep, 1)[0]) if compose_files else root / "compose.yaml"
    )
    return (root / first_file).parent / path


def read_validated_secret_file(path: Path, *, name: str | None = None) -> str:
    """Read one small text secret from an accessible regular file."""

    label = name or path.name
    flags = os.O_RDONLY | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"Secret source {label!r} could not be opened.") from error
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise ValueError(f"Secret source {label!r} must be a regular file.")
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
    """Check each enabled production secret source."""

    if not directory.is_dir():
        raise ValueError("The production secret directory is not accessible.")
    for name in PRODUCTION_SECRET_NAMES:
        read_validated_secret_file(directory / name, name=name)


def compose_command(*, edge: bool, env_file: Path | None = None) -> list[str]:
    command = ["docker", "compose"]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    if not os.environ.get("COMPOSE_FILE"):
        command.extend(["-f", "compose.yaml", "-f", "compose.prod.yaml"])
    if edge:
        command.extend(["--profile", "edge"])
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge", action="store_true", help="Start the included Caddy TLS edge.")
    args = parser.parse_args(argv)
    command = compose_command(edge=args.edge)
    try:
        validate_proxy_prefix(os.environ.get("STARTUNNEL_PROXY_IPV4_PREFIX", "172.30.254"))
        validate_production_secret_files(production_secret_directory())
        image = validate_image_reference(os.environ.get("STARTUNNEL_APP_IMAGE", ""))
        validate_production_model(
            render_model(command), image=image, secret_directory=production_secret_directory()
        )
    except (subprocess.CalledProcessError, TypeError, ValueError) as error:
        print(str(error))
        return 2

    subprocess.run([*command, "up", "-d", "--no-build"], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
