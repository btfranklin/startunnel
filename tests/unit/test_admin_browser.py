"""Browser administrator access shares API rules and keeps secrets private."""

from typing import Any
from uuid import uuid4

import pytest
from django.test import Client

from accounts.models import AdminCredential, User
from core.models import AuditEvent

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


def test_browser_can_remove_final_password_and_session_expires(
    client: Client, user_factory: Any
) -> None:
    actor = user_factory()
    client.force_login(actor)
    response = client.post(
        "/app/admins/access/",
        {"action": "remove-password", "admin_id": actor.id, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 302
    actor.refresh_from_db()
    assert not actor.has_usable_password()
    operation = actor.admin_receipts.get(operation="accounts.password")
    event = AuditEvent.objects.get(metadata__operation_id=str(operation.id))
    assert event.action == "accounts.password"
    assert event.channel == "browser"
    assert event.actor_user == actor
    next_request = client.get("/app/admins/")
    assert next_request.status_code == 302
    assert next_request["Location"].startswith("/accounts/login/")


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
    assert actor.admin_receipts.get().operation == "accounts.password"


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


@pytest.mark.parametrize(
    "data, field",
    [
        ({"action": "revoke-key", "key_id": "bad-key-id"}, "key_id"),
        ({"action": "create-key", "name": "key", "expires_at": "bad-expiry"}, "expires_at"),
    ],
)
def test_browser_validation_errors_are_plain_field_messages(
    data: dict[str, str], field: str, client: Client, user_factory: Any
) -> None:
    from django.contrib.messages import get_messages

    actor = user_factory()
    client.force_login(actor)
    response = client.post("/app/admins/access/", {**data, "idempotency_key": str(uuid4())})
    assert response.status_code == 302
    errors = [str(message) for message in get_messages(response.wsgi_request)]
    assert len(errors) == 1
    assert errors[0].startswith(field + ": ")
    assert "input_value" not in errors[0]
    assert "errors.pydantic.dev" not in errors[0]
