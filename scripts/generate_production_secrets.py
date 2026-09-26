"""Create independent production secret files without showing their values."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path


def random_urlsafe(size: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).decode("ascii").rstrip("=")


def write_secret(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(value + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("secrets"))
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    names_and_values = {
        "startunnel_secret_key": secrets.token_urlsafe(64),
        "startunnel_api_key_pepper": random_urlsafe(),
        "startunnel_address_secret": random_urlsafe(),
        "startunnel_address_derivation_secret": random_urlsafe(),
        "startunnel_idempotency_secret": random_urlsafe(),
        "startunnel_metrics_token": random_urlsafe(),
        "postgres_admin_password": secrets.token_urlsafe(32),
        "postgres_runtime_password": secrets.token_urlsafe(32),
    }
    existing = [name for name in names_and_values if (directory / name).exists()]
    if existing:
        parser.error("Refusing to replace existing secret files: " + ", ".join(sorted(existing)))
    for name, value in names_and_values.items():
        write_secret(directory / name, value)
    print(f"Created {len(names_and_values)} secret files in {directory}.")
    print("Secret values were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
