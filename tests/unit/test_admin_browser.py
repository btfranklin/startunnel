"""Browser administrator access shares API rules and keeps secrets private."""

from typing import Any
from uuid import uuid4

import pytest
from django.test import Client

from accounts.models import AdminCredential, User

pytestmark = pytest.mark.django_db


def test_passwordless_admin_creation_downloads_first_key(client: Client, user_factory: Any) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post(
        "/app/admins/create/",
        {
            "username": "worker-admin",
            "password1": "",
            "password2": "",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["X-Startunnel-Operation-Id"]
    assert response["X-Startunnel-Audit-Event-Id"]
    assert response["Cache-Control"] == "no-store, private"
    account = User.objects.get(username="worker-admin")
    assert not account.has_usable_password()
    assert account.admin_credentials.count() == 1
    secret = response.content.decode().strip()
    assert secret.startswith("sta_")
    assert secret.encode() not in client.get("/app/admins/").content


def test_browser_cannot_remove_last_recovery_path(client: Client, user_factory: Any) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post(
        "/app/admins/access/",
        {"action": "remove-password", "admin_id": actor.id, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 302
    actor.refresh_from_db()
    assert actor.has_usable_password()


def test_browser_key_download_and_revoke(client: Client, user_factory: Any) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post(
        "/app/admins/access/",
        {
            "action": "create-key",
            "admin_id": actor.id,
            "name": "build",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 200
    key = AdminCredential.objects.get(name="build")
    secret = response.content.decode().strip()
    assert secret.encode() not in client.get("/app/account/").content
    assert (
        client.post(
            "/app/admins/access/",
            {"action": "revoke-key", "key_id": key.id, "idempotency_key": str(uuid4())},
        ).status_code
        == 302
    )
    key.refresh_from_db()
    assert key.revoked_at is not None
    audit = client.get("/app/audit/")
    assert audit.status_code == 200
    assert b"keys.revoke" in audit.content
    assert secret.encode() not in audit.content


def test_browser_mutation_requires_retry_key(client: Client, user_factory: Any) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post("/app/agents/create/", {"name": "Missing retry key"})
    assert response.status_code == 400
    assert not actor.created_agent_credentials.filter(name="Missing retry key").exists()


def test_password_form_records_operation_and_preserves_session(
    client: Client, user_factory: Any
) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post(
        "/accounts/password/change/",
        {
            "old_password": "test-password-42",
            "new_password1": "New-safe-browser-password-67!",
            "new_password2": "New-safe-browser-password-67!",
            "idempotency_key": str(uuid4()),
        },
    )
    assert response.status_code == 302
    assert response["X-Startunnel-Operation-Id"]
    actor.refresh_from_db()
    assert actor.check_password("New-safe-browser-password-67!")
    assert client.get("/app/account/").status_code == 200
    assert actor.admin_operations.get().operation == "accounts.password"


def test_django_model_admin_cannot_bypass_product_services(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    actor.is_staff = actor.is_superuser = True
    actor.save()
    client.force_login(actor)
    credential, _ = credential_factory()
    route = f"/admin/agents/agentcredential/{credential.id}/"
    assert client.get(route + "change/").status_code == 200
    assert client.post(route + "change/", {"name": "Bypassed"}).status_code == 403
    assert client.post(route + "delete/", {"post": "yes"}).status_code == 403
    assert (
        client.post("/admin/agents/agentcredential/add/", {"name": "Bypassed"}).status_code == 403
    )
    credential.refresh_from_db()
    assert credential.name != "Bypassed"
