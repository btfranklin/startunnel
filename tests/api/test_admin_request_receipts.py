"""Existing admin receipts keep API and browser request fingerprints."""

from __future__ import annotations

import hmac
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from accounts.admin_access import issue_admin_key
from accounts.admin_commands import derive_secret
from accounts.admin_data import agent_resource, key_resource
from accounts.models import AdminReceipt, User
from agents.services import create_credential
from core.models import AuditEvent

pytestmark = pytest.mark.django_db


def _old_receipt(actor: User, operation: str, data: dict[str, Any], key: str) -> AdminReceipt:
    canonical = json.dumps(
        {"operation": operation, "data": data},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode()
    return AdminReceipt.objects.create(
        owner=actor,
        operation=operation,
        key_digest=hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), key.encode(), "sha256"),
        request_digest=hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), canonical, "sha256"),
        derivation_nonce=b"r" * 32,
        expires_at=timezone.now() + timedelta(hours=24),
    )


def _store_response(receipt: AdminReceipt, resource: dict[str, Any]) -> None:
    receipt.response = {
        "resource": resource,
        "operation": {"id": str(receipt.id), "audit_event_id": str(uuid4())},
    }
    receipt.secret_resource_id = resource["id"]
    receipt.save(update_fields=["response", "secret_resource_id", "secret_kind"])


@pytest.mark.parametrize("channel", ["api", "browser"])
def test_existing_key_receipt_keeps_uuid_and_datetime_spelling(
    channel: str, client: Client, user_factory: Any
) -> None:
    actor = user_factory()
    _, admin_key = issue_admin_key(owner=actor, name="API caller")
    expiry = timezone.now() + timedelta(days=1)
    raw_expiry = expiry.isoformat(sep=" ")
    raw_id = str(actor.id).upper()
    request = {"admin_id": raw_id, "name": "Existing key", "expires_at": raw_expiry}
    old_data = (
        {
            **request,
            "admin_id": str(actor.id),
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        }
        if channel == "api"
        else request
    )
    key = "old-key-receipt-" + channel
    receipt = _old_receipt(actor, "keys.create", old_data, key)
    receipt.secret_kind = "admin_key"
    secret = derive_secret(receipt, "admin_key")
    issued, _ = issue_admin_key(owner=actor, name="Existing key", expires_at=expiry, key=secret)
    _store_response(receipt, key_resource(issued))
    fingerprint = bytes(receipt.request_digest)

    if channel == "api":
        response = client.post(
            "/api/v1/admin/keys/create",
            data=json.dumps(request),
            content_type="application/json",
            headers={"Authorization": f"Bearer {admin_key}", "Idempotency-Key": key},
        )
        assert response.status_code == 200, response.content
        assert response.json()["operation"]["id"] == str(receipt.id)
        assert response.json()["secret"] == secret
    else:
        client.force_login(actor)
        response = client.post(
            "/app/admins/access/",
            {**request, "action": "create-key", "idempotency_key": key},
        )
        assert response.status_code == 200, response.content
        assert response["X-Startunnel-Operation-Id"] == str(receipt.id)
        assert response.content.decode().strip() == secret
    receipt.refresh_from_db()
    assert bytes(receipt.request_digest) == fingerprint
    assert AdminReceipt.objects.count() == 1
    assert not AuditEvent.objects.filter(action="keys.create").exists()


@pytest.mark.parametrize("channel", ["api", "browser"])
def test_existing_agent_receipt_keeps_omitted_browser_defaults(
    channel: str, client: Client, user_factory: Any
) -> None:
    actor = user_factory()
    _, admin_key = issue_admin_key(owner=actor, name="API caller")
    request = {"name": "Existing agent"}
    old_data = {**request, "expires_at": None} if channel == "api" else request
    key = "old-agent-receipt-" + channel
    receipt = _old_receipt(actor, "agents.create", old_data, key)
    receipt.secret_kind = "agent_key"
    secret = derive_secret(receipt, "agent_key")
    issued = create_credential(actor=actor, name="Existing agent", key=secret)
    _store_response(receipt, agent_resource(issued.credential))

    if channel == "api":
        response = client.post(
            "/api/v1/admin/agents/create",
            data=json.dumps(request),
            content_type="application/json",
            headers={"Authorization": f"Bearer {admin_key}", "Idempotency-Key": key},
        )
        assert response.status_code == 200, response.content
        assert response.json()["operation"]["id"] == str(receipt.id)
        assert response.json()["secret"] == secret
    else:
        client.force_login(actor)
        response = client.post("/app/agents/create/", {**request, "idempotency_key": key})
        assert response.status_code == 200, response.content
        assert response["X-Startunnel-Operation-Id"] == str(receipt.id)
        assert response.content.decode().strip() == secret
    assert AdminReceipt.objects.count() == 1


def test_cleared_operation_result_is_unavailable(client: Client, user_factory: Any) -> None:
    actor = user_factory()
    _, key = issue_admin_key(owner=actor, name="Receipt reader")
    receipt = _old_receipt(actor, "agents.create", {"name": "Expired"}, "cleared-result")
    response = client.get(
        f"/api/v1/admin/operations/{receipt.id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
