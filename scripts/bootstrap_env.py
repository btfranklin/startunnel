"""Create a local .env file without showing secret values."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / ".env"
EXAMPLE = PROJECT_ROOT / ".env.example"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def random_urlsafe(size: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).decode("ascii").rstrip("=")


def render_env(values: dict[str, str]) -> str:
    """Use the example's sections without copying placeholder values."""

    remaining = dict(values)
    sections = []
    for section in EXAMPLE.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = []
        has_setting = False
        template_has_setting = False
        for line in section.splitlines():
            if line.startswith("#"):
                lines.append(line)
                continue
            key, separator, _ = line.partition("=")
            if separator:
                template_has_setting = True
                if key in remaining:
                    lines.append(f"{key}={remaining.pop(key)}")
                    has_setting = True
        if has_setting or not template_has_setting:
            sections.append("\n".join(lines))
    if remaining:
        sections.append(
            "# Additional local settings\n"
            + "\n".join(f"{key}={value}" for key, value in remaining.items())
        )
    return "\n\n".join(sections) + "\n"


def write_env(path: Path, content: str) -> None:
    """Replace one regular environment file without following a link."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError("The environment target must be a regular file.")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
            env_file.write(content)
            env_file.flush()
            os.fsync(env_file.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy-openai-from", type=Path)
    parser.add_argument(
        "--rotate-local-secrets",
        action="store_true",
        help="Replace local application secrets without showing them.",
    )
    args = parser.parse_args()

    current = read_env(TARGET)
    source = read_env(args.copy_openai_from) if args.copy_openai_from else {}
    defaults = {
        "STARTUNNEL_ENV": current.get("STARTUNNEL_ENV", "development"),
        "STARTUNNEL_SECRET_KEY": current.get("STARTUNNEL_SECRET_KEY", secrets.token_urlsafe(64)),
        "STARTUNNEL_API_KEY_PEPPER": current.get("STARTUNNEL_API_KEY_PEPPER", random_urlsafe()),
        "STARTUNNEL_ADDRESS_SECRET": current.get("STARTUNNEL_ADDRESS_SECRET", random_urlsafe()),
        "STARTUNNEL_ADDRESS_DERIVATION_SECRET": current.get(
            "STARTUNNEL_ADDRESS_DERIVATION_SECRET", random_urlsafe()
        ),
        "STARTUNNEL_IDEMPOTENCY_SECRET": current.get(
            "STARTUNNEL_IDEMPOTENCY_SECRET", random_urlsafe()
        ),
        "STARTUNNEL_ALLOWED_HOSTS": current.get("STARTUNNEL_ALLOWED_HOSTS", "localhost,127.0.0.1"),
        "STARTUNNEL_BASE_URL": current.get("STARTUNNEL_BASE_URL", "http://localhost:8000"),
        "STARTUNNEL_TRUSTED_PROXY_CIDRS": current.get(
            "STARTUNNEL_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,172.16.0.0/12"
        ),
        "STARTUNNEL_MAX_REQUEST_BODY_BYTES": current.get(
            "STARTUNNEL_MAX_REQUEST_BODY_BYTES", "131072"
        ),
        "DATABASE_URL": current.get(
            "DATABASE_URL", "postgresql://startunnel:startunnel@postgres:5432/startunnel"
        ),
        "DATABASE_ADMIN_URL": current.get(
            "DATABASE_ADMIN_URL",
            "postgresql://startunnel_admin:startunnel_admin@postgres:5432/startunnel",
        ),
        "STARTUNNEL_LOG_LEVEL": current.get("STARTUNNEL_LOG_LEVEL", "INFO"),
        "STARTUNNEL_METRICS_TOKEN": current.get("STARTUNNEL_METRICS_TOKEN", random_urlsafe()),
    }
    values = {**current, **defaults}
    for key in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        value = current.get(key) or source.get(key)
        if value:
            values[key] = value

    if args.rotate_local_secrets:
        values.update(
            {
                "STARTUNNEL_SECRET_KEY": secrets.token_urlsafe(64),
                "STARTUNNEL_API_KEY_PEPPER": random_urlsafe(),
                "STARTUNNEL_ADDRESS_SECRET": random_urlsafe(),
                "STARTUNNEL_ADDRESS_DERIVATION_SECRET": random_urlsafe(),
                "STARTUNNEL_IDEMPOTENCY_SECRET": random_urlsafe(),
                "STARTUNNEL_METRICS_TOKEN": random_urlsafe(),
            }
        )
    content = render_env(values)
    try:
        write_env(TARGET, content)
    except OSError as error:
        parser.error(str(error))
    action = "Rotated" if args.rotate_local_secrets else "Created"
    print(f"{action} .env local settings. Secret values were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
