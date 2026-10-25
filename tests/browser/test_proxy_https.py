"""The bundled Caddy edge preserves production HTTPS and browser CSRF rules."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("STARTUNNEL_PROXY_TESTS") != "1", reason="Use the isolated proxy lane."
    ),
]
ORIGIN = "https://proxy-proof.internal"


def _database(code: str) -> object:
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
    environment["DATABASE_URL"] = environment["DATABASE_ADMIN_URL"]
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, "Proxy proof database operation failed."
    return json.loads(result.stdout.splitlines()[-1])


def test_caddy_https_login_forwarded_headers_and_csrf() -> None:
    username = f"proxy_{uuid4().hex[:20]}"
    password = f"Proxy-{uuid4().hex}-Pass9!"
    replacement = f"Changed-{uuid4().hex}-Pass9!"
    _database(
        "from accounts.models import User; "
        f"User.objects.create_user(username={username!r}, password={password!r}); print('true')"
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("proxy-proof.internal", 443))
        source_ip = route.getsockname()[0]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            # This disposable Caddy issuer is not installed in the browser trust store.
            context = browser.new_context(
                ignore_https_errors=True,
                extra_http_headers={
                    "X-Forwarded-For": "198.51.100.77",
                    "X-Forwarded-Proto": "http",
                },
            )
            try:
                page = context.new_page()
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = page.goto(f"{ORIGIN}/accounts/login/", timeout=5_000)
                        assert response is not None and response.status == 200
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(1)
                details = response.security_details()
                assert details is not None and "Caddy" in (details["issuer"] or "")
                page.get_by_label("Username").fill(username)
                page.get_by_label("Password", exact=True).fill(password)
                page.get_by_role("button", name="Sign in", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Instance overview", exact=True)
                ).to_be_visible()
                cookies = {cookie["name"]: cookie for cookie in context.cookies()}
                assert cookies["sessionid"]["secure"] and cookies["csrftoken"]["secure"]
                assert page.url.startswith(ORIGIN)

                # Caddy must replace the supplied address before the login limiter uses it.
                counts = _database(
                    "import hashlib, json; from core.models import RateLimitBucket; "
                    "from core.rate_limits import _key_digest; "
                    f"username={username!r}; "
                    "username_digest=hashlib.sha256(username.casefold().encode()).hexdigest(); "
                    f"sources=[{source_ip!r}, '198.51.100.77']; "
                    "print(json.dumps([RateLimitBucket.objects.filter(key_digest=_key_digest("
                    "'login:'+hashlib.sha256(source.encode()).hexdigest()+':'+username_digest"
                    ")).count() for source in sources]))"
                )
                assert counts == [1, 0]
                page.goto(f"{ORIGIN}/accounts/password/change/")
                token = page.locator('[name="csrfmiddlewaretoken"]').input_value()
                rejected = context.request.post(
                    f"{ORIGIN}/accounts/password/change/",
                    headers={"Origin": "https://attacker.invalid", "X-CSRFToken": token},
                    form={
                        "old_password": password,
                        "new_password1": replacement,
                        "new_password2": replacement,
                        "idempotency_key": uuid4().hex,
                    },
                )
                assert rejected.status == 403
                page.locator('[name="old_password"]').fill(password)
                page.locator('[name="new_password1"]').fill(replacement)
                page.locator('[name="new_password2"]').fill(replacement)
                page.get_by_role("button", name="Change password", exact=True).click()
                expect(page).to_have_url(f"{ORIGIN}/app/account/")
                assert (
                    _database(
                        "from accounts.models import User; import json; "
                        f"print(json.dumps(User.objects.get(username={username!r}).check_password({replacement!r})))"
                    )
                    is True
                )
            finally:
                context.close()
                browser.close()
    finally:
        _database(
            "from accounts.models import User; "
            f"User.objects.filter(username={username!r}).delete(); print('true')"
        )
