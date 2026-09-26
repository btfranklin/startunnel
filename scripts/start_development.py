"""Start the local development stack on an available web port."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8000


def port_is_available(port: int) -> bool:
    """Return true when the local web port can be bound."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port() -> int:
    """Prefer the documented port and otherwise request a free local port."""

    if port_is_available(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def compose_command(arguments: Sequence[str]) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.dev.yaml",
        "up",
        "--build",
        "--wait",
        *arguments,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start StarTunnel locally and select an available web port."
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Use this local web port instead of selecting one automatically.",
    )
    args, compose_arguments = parser.parse_known_args()

    port = args.port if args.port is not None else choose_port()
    if not 1 <= port <= 65535:
        parser.error("--port must be from 1 through 65535.")
    if not port_is_available(port):
        parser.error(f"Local web port {port} is already in use.")

    environment = os.environ.copy()
    environment["STARTUNNEL_WEB_PORT"] = str(port)
    environment["STARTUNNEL_BASE_URL"] = f"http://localhost:{port}"
    print(f"Starting StarTunnel on local port {port}...", flush=True)
    result = subprocess.run(
        compose_command(compose_arguments), cwd=PROJECT_ROOT, env=environment, check=False
    )
    if result.returncode == 0:
        print()
        print("StarTunnel is ready.")
        print(f"Open: http://localhost:{port}")
        print("Logs: scripts/container-logs.sh web")
        print("Stop: docker compose -f compose.yaml -f compose.dev.yaml down")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
