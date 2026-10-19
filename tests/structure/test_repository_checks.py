"""Repository-specific checks are executable from a fresh clone."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_local_secret_files_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert ".env.tutorial" in ignore or ".env.*" in ignore


def test_wordmark_uses_the_selected_sulfur_symbol() -> None:
    navigation = (ROOT / "templates/site/partials/navigation.html").read_text(encoding="utf-8")

    assert navigation.count("🜎") == 1
    assert "🜀" not in navigation


def test_pages_have_no_heading_or_section_eyebrows() -> None:
    forbidden_classes = {
        "app-header__context",
        "eyebrow",
        "kicker",
        "overline",
        "page-label",
        "review-flag",
        "section-label",
        "section-tag",
        "trust-line",
    }
    sources = [
        *sorted((ROOT / "templates").rglob("*.html")),
        *sorted((ROOT / "website/templates").rglob("*.html")),
        ROOT / "static/css/site.css",
        ROOT / "website/assets/css/signal-chamber.css",
    ]

    for path in sources:
        content = path.read_text(encoding="utf-8")
        for class_name in forbidden_classes:
            assert class_name not in content, (
                f"{path.relative_to(ROOT)} uses the forbidden eyebrow class {class_name}. "
                "Start the section with its heading."
            )


@pytest.mark.parametrize("path", ["Dockerfile", "compose.yaml", "compose.dev.yaml"])
def test_every_uvicorn_command_disables_implicit_proxy_trust(path: str) -> None:
    content = (ROOT / path).read_text(encoding="utf-8")
    if path == "Dockerfile":
        commands = [
            json.loads(line.removeprefix("CMD "))
            for line in content.splitlines()
            if line.startswith("CMD ")
        ]
    else:
        config = yaml.safe_load(content)
        commands = [
            service["command"]
            for service in config["services"].values()
            if "uvicorn" in str(service.get("command", []))
        ]
    assert commands
    assert all("--no-proxy-headers" in command for command in commands)


def test_web_worker_and_database_pool_defaults_preserve_headroom() -> None:
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    example = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    def default(value: str) -> int:
        match = re.fullmatch(r"\$\{([A-Z_]+):-([0-9]+)\}", value)
        assert match is not None
        name, amount = match.groups()
        assert int(example[name]) == int(amount)
        return int(amount)

    web = services["web"]
    command = web["command"]
    workers = default(command[command.index("--workers") + 1])
    web_pool = default(web["environment"]["STARTUNNEL_DATABASE_POOL_MAX_SIZE"])
    maintenance_pool = default(
        services["maintenance"]["environment"]["STARTUNNEL_DATABASE_POOL_MAX_SIZE"]
    )
    # PostgreSQL defaults to 100 connections. Keep ten for tools and recovery.
    assert workers * web_pool + maintenance_pool <= 90
