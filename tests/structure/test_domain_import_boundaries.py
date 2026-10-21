"""The domain owner modules retain their transport import constraints."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_structure


@pytest.mark.parametrize(
    ("package_name", "owner"),
    [
        ("agents", "services"),
        ("tunnels", "access"),
        ("tunnels", "activity"),
        ("tunnels", "beacons"),
        ("tunnels", "context"),
        ("tunnels", "entry"),
        ("tunnels", "events"),
        ("tunnels", "messages"),
        ("tunnels", "cycles"),
        ("tunnels", "admin_actions"),
        ("tunnels", "participants"),
        ("tunnels", "payloads"),
        ("tunnels", "retention"),
        ("tunnels", "search"),
        ("accounts", "admin_access"),
        ("accounts", "admin_commands"),
        ("accounts", "admin_data"),
        ("accounts", "admin_services"),
        ("core", "audit"),
        ("core", "errors"),
        ("core", "idempotency"),
        ("core", "maintenance"),
    ],
)
@pytest.mark.parametrize(
    "dependency",
    ["api", "site_app", "ninja", "django.http", "django.shortcuts", "django.template", "openai"],
)
def test_domain_owners_reject_transport_and_provider_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_name: str, owner: str, dependency: str
) -> None:
    source = tmp_path / "src"
    package = source / package_name
    package.mkdir(parents=True)
    module = package / f"{owner}.py"
    module.write_text(f"import {dependency}\n", encoding="utf-8")
    monkeypatch.setattr(check_structure, "ROOT", tmp_path)
    monkeypatch.setattr(check_structure, "SOURCE", source)
    failures = check_structure.check_import_directions()
    assert len(failures) == 1
    module.write_text("from .models import Tunnel\n", encoding="utf-8")
    assert check_structure.check_import_directions() == []
