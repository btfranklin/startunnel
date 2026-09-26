"""Profile-gated browser proof for local administration and agent access."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, StorageState, expect, sync_playwright

ENABLED = os.getenv("STARTUNNEL_BROWSER_TESTS") == "1"
BROWSER_NAMES = tuple(
    name.strip()
    for name in os.getenv("STARTUNNEL_BROWSERS", "chromium,firefox,webkit").split(",")
    if name.strip()
)
REQUIRED_WIDTHS = (320, 375, 768, 1440)
PUBLIC_ROUTES = (
    "/docs/",
    "/docs/agent-quickstart/",
    "/docs/tutorial/",
    "/docs/concepts/",
    "/docs/authentication/",
    "/docs/instance-tunnels/",
    "/docs/security/",
    "/docs/local-development/",
    "/accounts/login/",
)
ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not ENABLED, reason="Use the browser-test Compose profile."),
]


@dataclass(frozen=True, slots=True)
class BrowserAccount:
    username: str
    password: str
    storage_state: StorageState


@pytest.fixture(scope="module", params=BROWSER_NAMES)
def browser(request: pytest.FixtureRequest) -> Iterator[Browser]:
    browser_name = cast(str, request.param)
    with sync_playwright() as playwright:
        browser_types = {
            "chromium": playwright.chromium,
            "firefox": playwright.firefox,
            "webkit": playwright.webkit,
        }
        instance = browser_types[browser_name].launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture(scope="module")
def base_url() -> str:
    return os.getenv("STARTUNNEL_BASE_URL", "http://web:8000").rstrip("/")


def _database_shell(code: str) -> None:
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def account(browser: Browser, base_url: str) -> Iterator[BrowserAccount]:
    username = f"browser_{uuid4().hex[:20]}"
    password = f"Browser-{uuid4().hex}-Pass9!"
    _database_shell(
        "from accounts.models import User; "
        f"User.objects.create_user(username={username!r}, password={password!r})"
    )
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{base_url}/accounts/login/")
    page.get_by_label("Username").fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Start with two agents.")).to_be_visible()
    result = BrowserAccount(username, password, context.storage_state())
    context.close()
    try:
        yield result
    finally:
        _database_shell(
            f"from accounts.models import User; User.objects.filter(username={username!r}).delete()"
        )


def _authenticated_context(browser: Browser, account: BrowserAccount) -> BrowserContext:
    return browser.new_context(storage_state=account.storage_state)


def _create_agent_key(page: Page, base_url: str, name: str) -> str:
    page.goto(f"{base_url}/app/agents/")
    page.get_by_label("Agent name").fill(name)
    page.get_by_role("button", name="Create agent key").click()
    expect(page.get_by_role("heading", name="One-time agent key")).to_be_visible()
    key = page.locator("#one-time-agent-key").inner_text().strip()
    assert re.fullmatch(r"st_[A-Za-z0-9_-]{43}", key)
    return key


def _assert_widths(context: BrowserContext, base_url: str, routes: tuple[str, ...]) -> None:
    page = context.new_page()
    try:
        for route in routes:
            for width in REQUIRED_WIDTHS:
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(base_url + route)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                    route,
                    width,
                )
    finally:
        page.close()


def test_public_routes_fit_required_widths(browser: Browser, base_url: str) -> None:
    context = browser.new_context()
    try:
        _assert_widths(context, base_url, PUBLIC_ROUTES)
    finally:
        context.close()


def test_local_login_human_admin_and_password_change(
    browser: Browser, base_url: str, account: BrowserAccount
) -> None:
    context = _authenticated_context(browser, account)
    page = context.new_page()
    second_username = f"human_{uuid4().hex[:16]}"
    second_password = f"Second-{uuid4().hex}-Pass9!"
    try:
        page.goto(f"{base_url}/app/users/")
        page.get_by_label("Username").fill(second_username)
        page.locator('input[name="password1"]').fill(second_password)
        page.locator('input[name="password2"]').fill(second_password)
        page.get_by_role("button", name="Create account").click()
        expect(page.get_by_text(second_username, exact=True)).to_be_visible()

        row = page.locator("li").filter(has_text=second_username)
        row.get_by_role("button", name="Deactivate").click()
        expect(row.get_by_text("Inactive", exact=True)).to_be_visible()
        row.get_by_role("button", name="Reactivate").click()
        expect(row.get_by_text("Active", exact=True)).to_be_visible()

        page.goto(f"{base_url}/app/account/")
        page.get_by_role("link", name="Change password").click()
        expect(page.get_by_role("heading", name="Change password")).to_be_visible()
    finally:
        page.close()
        context.close()
        _database_shell(
            "from accounts.models import User; "
            f"User.objects.filter(username={second_username!r}).delete()"
        )


def test_agent_key_is_one_time_and_tunnel_address_rotates(
    browser: Browser, base_url: str, account: BrowserAccount
) -> None:
    context = _authenticated_context(browser, account)
    page = context.new_page()
    key = _create_agent_key(page, base_url, f"Browser agent {uuid4().hex[:8]}")
    label = f"Rotation proof {uuid4().hex[:10]}"
    try:
        page.goto(f"{base_url}/app/agents/")
        assert key not in page.content()
        with httpx.Client(base_url=base_url, timeout=10) as client:
            created = client.post(
                "/api/v1/tunnels",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Idempotency-Key": f"browser-{uuid4().hex}",
                },
                json={
                    "label": label,
                    "cycle": {
                        "label": "Browser proof",
                        "root": {"content": {"type": "text", "text": "Rotate this address."}},
                    },
                },
            )
            assert created.status_code == 201
            old_address = str(created.json()["tunnel"]["address"])

            page.goto(f"{base_url}/app/tunnels/")
            row = page.locator("li").filter(has_text=label)
            row.locator("summary").filter(has_text="Rotate address").click()
            row.get_by_role("button", name="Rotate address").click()
            expect(page.get_by_role("heading", name="Copy the new tunnel address.")).to_be_visible()
            new_address = page.locator("#one-time-tunnel-address").inner_text().strip()
            assert new_address != old_address

            old_read = client.post(
                "/api/v1/tree",
                headers={"Authorization": f"Bearer {key}"},
                json={"address": old_address},
            )
            new_read = client.post(
                "/api/v1/tree",
                headers={"Authorization": f"Bearer {key}"},
                json={"address": new_address},
            )
            assert old_read.status_code == 404
            assert new_read.status_code == 200
    finally:
        page.close()
        context.close()


def test_no_javascript_admin_forms_work(
    browser: Browser, base_url: str, account: BrowserAccount
) -> None:
    context = browser.new_context(java_script_enabled=False, storage_state=account.storage_state)
    page = context.new_page()
    username = f"nojs_{uuid4().hex[:16]}"
    password = f"Nojs-{uuid4().hex}-Pass9!"
    try:
        page.goto(f"{base_url}/app/users/")
        page.get_by_label("Username").fill(username)
        page.locator('input[name="password1"]').fill(password)
        page.locator('input[name="password2"]').fill(password)
        page.get_by_role("button", name="Create account").click()
        expect(page.get_by_text(username, exact=True)).to_be_visible()
    finally:
        page.close()
        context.close()
        _database_shell(
            f"from accounts.models import User; User.objects.filter(username={username!r}).delete()"
        )


def test_application_routes_fit_required_widths(
    browser: Browser, base_url: str, account: BrowserAccount
) -> None:
    context = _authenticated_context(browser, account)
    try:
        _assert_widths(
            context,
            base_url,
            (
                "/app/",
                "/app/learn/",
                "/app/users/",
                "/app/agents/",
                "/app/tunnels/",
                "/app/account/",
            ),
        )
    finally:
        context.close()
