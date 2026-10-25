"""Run typed runtime-image probes in a checked application interpreter."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

STATIC_ASSETS = (
    "css/site.css",
    "js/site.js",
    "vendor/htmx/htmx.min.js",
    "vendor/fonts/inter-latin-400-normal.woff2",
)
RUNTIME_TOOLS = ("pip", "pip3", "perfect-doc", "cargo")
CONTAINER_PATH = "/proof/runtime.py"


class HealthResponse(Protocol):
    status: int


class HealthOpener(Protocol):
    def __call__(self, request: urllib.request.Request, *, timeout: float) -> HealthResponse: ...


def check_health(
    expected_status: int,
    path: str,
    *,
    urlopen: HealthOpener | None = None,
) -> None:
    if path not in {"/health/live", "/health/ready"}:
        raise ValueError("The health proof path is not allowed.")
    request = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        headers={"Host": "web", "X-Forwarded-Proto": "https"},
    )
    try:
        response = (
            urllib.request.urlopen(request, timeout=10)
            if urlopen is None
            else urlopen(request, timeout=10)
        )
        status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    assert status == expected_status, status


def validate_runtime_contents(
    *,
    build: Mapping[str, object],
    client_bytes: bytes,
    client_output: str,
    ca_certificate_count: int,
    static_sources: Mapping[str, bool],
    collected_assets: Mapping[str, bool],
    available_tools: Mapping[str, bool],
) -> None:
    from core.json_types import json_object

    for name in STATIC_ASSETS:
        assert static_sources.get(name, False), f"Missing static source: {name}"
        assert collected_assets.get(name, False), f"Empty collected asset: {name}"
    assert ca_certificate_count > 0, "Missing CA certificates"
    client_build = json_object(json.loads(client_output))
    assert client_build.get("version"), "Missing client version"
    assert "source_revision" in client_build, "Missing client source metadata"
    assert client_build["version"] == build["version"], "Server and client versions differ"
    assert client_build["source_revision"] == build["source_revision"], (
        "Server and client sources differ"
    )
    assert client_build.get("sha256") == hashlib.sha256(client_bytes).hexdigest()
    for command in RUNTIME_TOOLS:
        assert not available_tools.get(command, False), f"Unexpected runtime tool: {command}"


def check_no_pip() -> None:
    assert importlib.util.find_spec("pip") is None, "pip is installed"


def check_runtime_contents() -> None:
    import django

    django.setup()
    from django.contrib.staticfiles import finders
    from django.contrib.staticfiles.storage import ManifestStaticFilesStorage

    from core.build import BUILD

    collected = ManifestStaticFilesStorage()
    static_sources: dict[str, bool] = {}
    collected_assets: dict[str, bool] = {}
    for name in STATIC_ASSETS:
        source = finders.find(name)
        static_sources[name] = bool(source and Path(source).is_file())
        collected_assets[name] = False
        if source and Path(source).is_file():
            with collected.open(collected.stored_name(name)) as asset:
                collected_assets[name] = bool(asset.read(1))

    client = Path("/app/cli/star_tunnel.py")
    result = subprocess.run(
        [sys.executable, str(client), "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    validate_runtime_contents(
        build=BUILD,
        client_bytes=client.read_bytes(),
        client_output=result.stdout,
        ca_certificate_count=len(ssl.create_default_context().get_ca_certs()),
        static_sources=static_sources,
        collected_assets=collected_assets,
        available_tools={command: shutil.which(command) is not None for command in RUNTIME_TOOLS},
    )
    check_no_pip()
    subprocess.run(
        [str(Path(sys.base_prefix) / "bin" / "python3"), CONTAINER_PATH, "pip-absence"],
        check=True,
    )
    print("Runtime image contents passed.")


def metrics_file_inodes(directory: Path = Path("/tmp/startunnel-prometheus")) -> dict[str, int]:
    """Read only the inode numbers of current metrics files."""
    return {path.name: path.stat().st_ino for path in directory.glob("*.db") if path.is_file()}


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    health = actions.add_parser("health", help="check a health endpoint")
    health.add_argument("expected_status", type=int)
    health.add_argument("path")
    actions.add_parser("contents", help="check runtime image contents")
    actions.add_parser("pip-absence", help="check that pip is not installed")
    actions.add_parser("settings", help="read the selected Django settings module")
    actions.add_parser("metrics-inodes", help="read metrics file inode numbers")
    args = parser.parse_args(arguments)
    if args.action == "health":
        check_health(args.expected_status, args.path)
    elif args.action == "pip-absence":
        check_no_pip()
    elif args.action == "settings":
        print(os.environ.get("DJANGO_SETTINGS_MODULE", ""))
    elif args.action == "metrics-inodes":
        print(json.dumps(metrics_file_inodes(), sort_keys=True))
    else:
        check_runtime_contents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
