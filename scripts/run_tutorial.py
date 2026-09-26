"""Run the guided tutorial in Docker without showing agent keys."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TUTORIAL_ENV = PROJECT_ROOT / ".env.tutorial"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def compose_command(*, use_local_service: bool) -> list[str]:
    command = [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.dev.yaml",
        "--profile",
        "tutorial",
        "run",
        "--rm",
    ]
    if not use_local_service:
        command.append("--no-deps")
    return [*command, "tutorial"]


def main() -> int:
    if not TUTORIAL_ENV.is_file():
        raise SystemExit(
            "Create .env.tutorial from examples/.env.tutorial.example before you continue."
        )
    if os.name == "posix" and stat.S_IMODE(TUTORIAL_ENV.stat().st_mode) & 0o077:
        raise SystemExit("Limit .env.tutorial to your user with: chmod 0600 .env.tutorial")
    values = read_env(TUTORIAL_ENV)
    required = {"STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY"}
    if missing := sorted(name for name in required if not values.get(name)):
        raise SystemExit("Add these setting names to .env.tutorial: " + ", ".join(missing))
    if any(values[name].startswith("replace-") for name in required):
        raise SystemExit("Replace every tutorial placeholder before you continue.")
    base_url = values["STARTUNNEL_BASE_URL"]
    host = urlparse(base_url).hostname
    use_local_service = host in {"localhost", "127.0.0.1"}
    tutorial_base_url = "http://web:8000" if use_local_service else base_url
    environment = os.environ.copy()
    environment.update({name: values[name] for name in required})
    if priority := values.get("STARTUNNEL_TUTORIAL_PRIORITY"):
        environment["STARTUNNEL_TUTORIAL_PRIORITY"] = priority
    environment["STARTUNNEL_TUTORIAL_BASE_URL"] = tutorial_base_url
    command = compose_command(use_local_service=use_local_service)
    try:
        return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False).returncode
    except FileNotFoundError as error:
        raise SystemExit(
            "Docker is not available. Start Docker and run this command again."
        ) from error


if __name__ == "__main__":
    raise SystemExit(main())
