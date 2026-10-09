"""Prove administrator work through the downloaded CLI without a browser login."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Browser, expect
from tests.browser.test_browser_experience import (  # noqa: F401
    _database_shell,
    _download_key,
    browser,
)
from tests.docs.test_agent_cli_live import _loopback_proxy

ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.getenv("STARTUNNEL_BROWSER_TESTS") == "1"
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not ENABLED, reason="Use the isolated browser-test Compose profile."),
]


class UnattendedClient:
    """Run the downloadable CLI and keep secret values out of test diagnostics."""

    def __init__(self, path: Path, base_url: str, admin_key_file: Path) -> None:
        self.path = path
        self.base_url = base_url
        self.admin_key_file = admin_key_file
        self.receipts: list[dict[str, str]] = []

    def command(
        self,
        *arguments: str,
        body: dict[str, Any] | None = None,
        agent_key_file: Path | None = None,
        admin_key_file: Path | None = None,
        success: bool = True,
    ) -> dict[str, Any]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        for name in ("STARTUNNEL_ADMIN_KEY", "STARTUNNEL_ADMIN_KEY_FILE", "STARTUNNEL_AGENT_KEY"):
            environment.pop(name, None)
        environment["STARTUNNEL_BASE_URL"] = self.base_url
        if agent_key_file is not None:
            environment["STARTUNNEL_AGENT_KEY"] = agent_key_file.read_text().strip()
        else:
            environment["STARTUNNEL_ADMIN_KEY_FILE"] = str(admin_key_file or self.admin_key_file)
        completed = subprocess.run(
            [sys.executable, "-I", str(self.path), *arguments],
            env=environment,
            input=json.dumps(body) if body is not None else None,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if success:
            assert completed.returncode == 0, completed.stderr
            assert completed.stderr == ""
            result = json.loads(completed.stdout)
            assert isinstance(result, dict)
            if arguments[0] == "admin":
                # Admin stdout can contain file paths and metadata, but no raw secret.
                safe_result = "secret" not in result
                assert safe_result, "Admin output must contain only secret-free metadata."
            return cast(dict[str, Any], result)
        assert completed.returncode == 1
        assert completed.stdout == ""
        return cast(dict[str, Any], json.loads(completed.stderr))

    def mutation(
        self, group: str, action: str, body: dict[str, Any], *, secret_output: Path | None = None
    ) -> dict[str, Any]:
        arguments = ["admin", group, action, "--idempotency-key", uuid4().hex]
        if secret_output is not None:
            arguments.extend(("--secret-output", str(secret_output)))
        result = self.command(*arguments, body=body)
        self.receipts.append(result["operation"])
        if secret_output is not None:
            assert secret_output.stat().st_mode & 0o777 == 0o600
        return cast(dict[str, Any], result["resource"])


def test_admin_cli_completes_unattended_administration(tmp_path: Path) -> None:
    username = "unattended_" + uuid4().hex[:20]
    bootstrap_file = tmp_path / "bootstrap.key"
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
    created = subprocess.run(
        [
            sys.executable,
            "manage.py",
            "create_instance_admin",
            username,
            "--key-file",
            str(bootstrap_file),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    assert bootstrap_file.stat().st_mode & 0o777 == 0o600
    target_url = os.getenv("STARTUNNEL_BASE_URL", "http://web:8000")
    with _loopback_proxy(target_url) as base_url:
        download = httpx.get(base_url + "/downloads/star_tunnel.py", timeout=30)
        assert download.status_code == 200
        cli_path = tmp_path / "star_tunnel.py"
        cli_path.write_bytes(download.content)
        client = UnattendedClient(cli_path, base_url, bootstrap_file)
        identity = client.command("admin", "me")
        admin_id = identity["admin"]["id"]
        old_key_id = identity["credential_id"]
        assert identity["admin"]["browser_access"] is False
        assert identity["authority"] == "instance_admin"
        assert client.command("admin", "doctor")["healthy"] is True
        assert "keys.create" in client.command("admin", "capabilities")["operations"]

        second_file = tmp_path / "second-admin.key"
        second = client.mutation(
            "accounts",
            "create",
            {
                "username": "second_" + uuid4().hex[:20],
                "key_name": "initial",
            },
            secret_output=second_file,
        )
        assert second["browser_access"] is False
        assert (
            client.command("admin", "me", admin_key_file=second_file)["admin"]["id"] == second["id"]
        )
        assert client.command("admin", "accounts", "get", second["id"])["active"] is True

        agent_file = tmp_path / "worker.key"
        agent = client.mutation(
            "agents", "create", {"name": "Unattended worker"}, secret_output=agent_file
        )
        assert client.command("me", agent_key_file=agent_file)["agent"]["id"] == agent["id"]
        created_tunnel = client.command(
            "create",
            "--idempotency-key",
            uuid4().hex,
            agent_key_file=agent_file,
            body={
                "label": "Unattended administration proof",
                "cycle": {"root": {"content": {"type": "text", "text": "Review this note."}}},
            },
        )
        tunnel_id = created_tunnel["tunnel"]["id"]
        address = created_tunnel["tunnel"]["address"]
        cycle_id = created_tunnel["cycle"]["id"]
        root_id = created_tunnel["cycle"]["root"]["id"]
        reply = client.command(
            "reply",
            "--idempotency-key",
            uuid4().hex,
            agent_key_file=agent_file,
            body={
                "address": address,
                "parent_id": root_id,
                "content": {"type": "text", "text": "The note is reviewed."},
            },
        )
        context = client.command(
            "read-context",
            agent_key_file=agent_file,
            body={
                "address": address,
                "cycle_id": cycle_id,
                "focus_message_id": reply["message"]["id"],
                "max_items": 10,
                "max_bytes": 32768,
            },
        )
        assert {root_id, reply["message"]["id"]}.issubset(
            {item["message"]["id"] for item in context["items"]}
        )
        tunnel = client.command("admin", "tunnels", "get", tunnel_id)
        generation = tunnel["address_generation"]
        client.mutation(
            "tunnels",
            "close",
            {
                "tunnel_id": tunnel_id,
                "expected_cycle_id": cycle_id,
            },
        )
        started = client.mutation(
            "tunnels",
            "start",
            {
                "tunnel_id": tunnel_id,
                "expected_address_generation": generation,
                "root_content": {"type": "text", "text": "Start the next review."},
            },
        )
        rolled = client.mutation(
            "tunnels",
            "rollover",
            {
                "tunnel_id": tunnel_id,
                "expected_cycle_id": started["active_cycle_id"],
                "expected_address_generation": generation,
                "root_content": {"type": "text", "text": "Start the final review."},
            },
        )
        assert rolled["active_cycle_id"] != started["active_cycle_id"]
        cycles = client.command("admin", "tunnels", "cycles", tunnel_id)
        assert len(cycles["items"]) == 3
        rotated_address_file = tmp_path / "rotated.address"
        rotated = client.mutation(
            "tunnels",
            "rotate",
            {
                "tunnel_id": tunnel_id,
                "expected_address_generation": generation,
            },
            secret_output=rotated_address_file,
        )
        assert rotated["address_generation"] == generation + 1
        inspected = client.command("admin", "tunnels", "get", tunnel_id)
        retired = client.mutation(
            "tunnels",
            "retire",
            {
                "tunnel_id": tunnel_id,
                "expected_address_generation": generation + 1,
                "confirmation": inspected["retirement_confirmation"],
            },
        )
        assert retired["state"] == "retired"

        replacement_file = tmp_path / "replacement.key"
        replacement = client.mutation(
            "keys",
            "create",
            {
                "admin_id": admin_id,
                "name": "replacement",
            },
            secret_output=replacement_file,
        )
        client.admin_key_file = replacement_file
        assert client.command("admin", "me")["credential_id"] == replacement["id"]
        revoked = client.mutation("keys", "revoke", {"key_id": old_key_id})
        assert revoked["available"] is False
        rejected = client.command("admin", "me", admin_key_file=bootstrap_file, success=False)
        assert rejected["error"]["status"] == 401
        agent_revoked = client.mutation("agents", "revoke", {"credential_id": agent["id"]})
        assert agent_revoked["available"] is False
        rejected_agent = client.command("me", agent_key_file=agent_file, success=False)
        assert rejected_agent["error"]["status"] == 401

        audit = client.command("admin", "audit", "list", "--limit", "100")
        by_id = {event["id"]: event for event in audit["items"]}
        for receipt in client.receipts:
            operation = client.command("admin", "operations", "get", receipt["id"])
            assert operation["result"]["operation"] == receipt
            event = by_id[receipt["audit_event_id"]]
            assert event["actor_id"] == admin_id
            assert event["channel"] == "admin_api"
            assert event["credential_id"] in {old_key_id, replacement["id"]}
        assert any(
            event["action"] == "admin.bootstrap"
            and event["channel"] == "server"
            and event["actor_id"] == admin_id
            for event in audit["items"]
        )


def test_admin_pages_and_key_controls_work_without_javascript(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
) -> None:
    username = "visual_" + uuid4().hex[:20]
    password = "Visual-" + uuid4().hex + "-Pass9!"
    _database_shell(
        "from accounts.models import User; "
        f"User.objects.create_user(username={username!r}, password={password!r})"
    )
    base_url = os.getenv("STARTUNNEL_BASE_URL", "http://web:8000").rstrip("/")
    artifact_dir = Path(os.getenv("STARTUNNEL_BROWSER_ARTIFACTS_DIR", str(tmp_path)))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(java_script_enabled=False, accept_downloads=True)
    page = context.new_page()
    try:
        page.goto(base_url + "/accounts/login/")
        page.get_by_label("Username", exact=True).fill(username)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("heading", name="Instance overview", exact=True)).to_be_visible()
        page.goto(base_url + "/app/account/")
        page.get_by_text("Manage access for " + username, exact=True).click()
        key_name = "No JavaScript admin key"
        page.get_by_label("Admin key name", exact=True).fill(key_name)
        downloaded = _download_key(
            page, button="Download new admin key", endpoint="/app/admins/access/"
        ).path()
        assert downloaded is not None
        key = Path(downloaded).read_text().strip()
        with httpx.Client(base_url=base_url, headers={"Authorization": "Bearer " + key}) as client:
            assert client.get("/api/v1/admin/me").status_code == 200
        page.reload()
        page.get_by_text("Manage access for " + username, exact=True).click()
        expect(page.get_by_text(key_name, exact=True)).to_be_visible()
        safe_page = key not in page.content()
        assert safe_page, "The browser must not embed the admin secret."
        for label, route in (
            ("overview", "/app/"),
            ("admins", "/app/admins/"),
            ("audit", "/app/audit/"),
            ("account", "/app/account/"),
        ):
            for width in (375, 1440):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto(base_url + route)
                if label in {"admins", "account"}:
                    page.get_by_text("Manage access for " + username, exact=True).click()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                    label,
                    width,
                )
                page.screenshot(
                    path=str(
                        artifact_dir / f"admin-{browser.browser_type.name}-{label}-{width}.jpg"
                    ),
                    full_page=True,
                    type="jpeg",
                )
        page.goto(base_url + "/app/account/")
        page.get_by_text("Manage access for " + username, exact=True).click()
        page.get_by_role("button", name="Revoke admin key", exact=True).click()
        page.get_by_text("Manage access for " + username, exact=True).click()
        key_row = page.get_by_text(key_name, exact=True).locator("xpath=ancestor::li[1]")
        expect(key_row.get_by_text("Revoked", exact=True)).to_be_visible()
        with httpx.Client(base_url=base_url, headers={"Authorization": "Bearer " + key}) as client:
            assert client.get("/api/v1/admin/me").status_code == 401
    finally:
        context.close()
        _database_shell(
            f"from accounts.models import User; User.objects.filter(username={username!r}).delete()"
        )
