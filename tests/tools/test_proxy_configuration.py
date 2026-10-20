"""Proxy trust and its dedicated network stay consistent in Compose models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any

import pytest
from scripts import compose_isolation

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI is required.")
@pytest.mark.parametrize("trust", [None, "127.0.0.0/8", "10.4.3.2/32", "172.16.0.0/12"])
@pytest.mark.parametrize("prefix", ["172.30.254", "192.168.241"])
def test_production_proxy_has_one_reserved_peer_and_preserves_explicit_trust(
    tmp_path: Path,
    trust: str | None,
    prefix: str,
) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text(f"STARTUNNEL_TRUSTED_PROXY_CIDRS={trust}\n" if trust else "")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "STARTUNNEL_TRUSTED_PROXY_CIDRS",
            "COMPOSE_FILE",
            "COMPOSE_PROFILES",
        }
    }
    environment.update(
        STARTUNNEL_DOMAIN="proxy.example.test",
        STARTUNNEL_APP_IMAGE="test:local",
        STARTUNNEL_PROXY_IPV4_PREFIX=prefix,
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "compose.yaml",
            "-f",
            "compose.prod.yaml",
            "--profile",
            "edge",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    services = model["services"]
    address = f"{prefix}.254"
    assert services["web"]["environment"]["STARTUNNEL_TRUSTED_PROXY_CIDRS"] == (
        f"{trust or '127.0.0.0/8'},{address}/32"
    )
    assert services["caddy"]["networks"] == {"proxy": {"ipv4_address": address}}
    assert set(services["web"]["networks"]) == {"startunnel", "proxy"}
    for service in ("postgres", "migrate", "maintenance"):
        assert set(services[service]["networks"]) == {"startunnel"}
    network = model["networks"]["proxy"]["ipam"]["config"][0]
    assert ip_address(address) in ip_network(network["subnet"])
    assert ip_address(address) not in ip_network(network["ip_range"])
    source = next(
        volume
        for volume in services["caddy"]["volumes"]
        if volume["target"] == "/etc/caddy/Caddyfile"
    )
    assert source["source"] == str(ROOT / "docker/caddy/Caddyfile")


def test_isolated_proxy_subnet_skips_occupied_docker_networks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def output(command: list[str], **kwargs: Any) -> str:
        if command[2] == "ls":
            return "first\nsecond"
        return json.dumps(
            [
                {"IPAM": {"Config": [{"Subnet": "172.16.0.0/16"}]}},
                {"IPAM": {"Config": [{"Subnet": "172.17.1.0/24"}, {"Subnet": "fd00::/64"}]}},
            ]
        )

    monkeypatch.setattr(compose_isolation, "required_output", output)
    assert compose_isolation.available_proxy_prefix({}) == "172.17.2"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI is required.")
def test_proxy_proof_uses_production_settings_without_published_ports() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "compose.yaml",
            "-f",
            "compose.test.yaml",
            "--profile",
            "proxy-proof",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=compose_isolation.safe_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("proxy-web", "proxy-caddy", "proxy-tests"):
        assert not services[name].get("ports")
    assert services["proxy-web"]["environment"]["DJANGO_SETTINGS_MODULE"] == (
        "startunnel.settings.production"
    )
    assert services["proxy-web"]["networks"]["proxy"]["aliases"] == ["web"]
    assert set(services["proxy-caddy"]["networks"]) == {"proxy"}
    source = next(
        volume
        for volume in services["proxy-caddy"]["volumes"]
        if volume["target"] == "/etc/caddy/Caddyfile"
    )
    assert source["source"] == str(ROOT / "docker/caddy/Caddyfile")
