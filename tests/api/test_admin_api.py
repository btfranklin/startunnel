"""Admin API requests share domain rules and keep credential types separate."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from accounts.admin_access import issue_admin_key
from accounts.models import AdminOperation
from tunnels.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_admin_api_discovery_mutation_and_receipts(client: Any, user_factory: Any) -> None:
    actor = user_factory()
    credential, secret = issue_admin_key(owner=actor, name="automation")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    assert client.get("/api/v1/admin/me", **headers).json()["admin"]["id"] == str(actor.id)
    assert (
        client.get("/api/v1/admin/capabilities", **headers).json()["authority"] == "instance_admin"
    )
    status = client.get("/api/v1/admin/status", **headers)
    assert status.status_code == 200
    assert status.json()["database"]["healthy"]
    key = str(uuid4())
    body = {"username": "api-worker"}
    result = client.post(
        "/api/v1/admin/accounts/create",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
        **headers,
    )
    assert result.status_code == 200
    created = result.json()
    assert created["secret"].startswith("sta_")
    event = AuditEvent.objects.get(pk=created["operation"]["audit_event_id"])
    assert event.actor_admin_credential == credential
    assert event.channel == "admin_api"
    replay = client.post(
        "/api/v1/admin/accounts/create",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
        **headers,
    )
    assert replay.json() == created
    inspect = client.get("/api/v1/admin/operations/" + created["operation"]["id"], **headers)
    assert "secret" not in inspect.content.decode()
    assert AdminOperation.objects.count() == 1
    assert (
        client.get("/api/v1/admin/accounts/" + created["resource"]["id"], **headers).status_code
        == 200
    )
    assert client.get("/api/v1/admin/accounts?limit=1", **headers).json()["next_cursor"]
    assert client.get("/api/v1/admin/keys", **headers).json()["items"]
    assert client.get("/api/v1/admin/agents", **headers).status_code == 200
    assert client.get("/api/v1/admin/tunnels", **headers).status_code == 200
    assert client.get("/api/v1/admin/audit", **headers).json()["items"]
    other = user_factory()
    _, other_secret = issue_admin_key(owner=other, name="other")
    assert (
        client.get(
            "/api/v1/admin/operations/" + created["operation"]["id"],
            HTTP_AUTHORIZATION="Bearer " + other_secret,
        ).status_code
        == 400
    )


def test_admin_api_rejects_agent_keys_and_missing_idempotency(
    client: Any, credential_factory: Any, user_factory: Any
) -> None:
    _, agent_secret = credential_factory()
    assert (
        client.get("/api/v1/admin/me", HTTP_AUTHORIZATION="Bearer " + agent_secret).status_code
        == 401
    )
    actor = user_factory()
    _, secret = issue_admin_key(owner=actor, name="admin")
    assert client.get("/api/v1/me", HTTP_AUTHORIZATION="Bearer " + secret).status_code == 401
    response = client.post(
        "/api/v1/admin/agents/create",
        data=json.dumps({"name": "missing-header"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + secret,
    )
    assert response.status_code == 400
    response = client.post(
        "/api/v1/admin/keys/create",
        data=json.dumps({"admin_id": str(uuid4()), "name": "missing"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + secret,
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    assert response.status_code == 400


def test_admin_api_owner_shared_rate_limit(
    client: Any, user_factory: Any, monkeypatch: Any
) -> None:
    from dataclasses import replace

    from core.limits import provider

    actor = user_factory()
    _, first = issue_admin_key(owner=actor, name="first")
    _, second = issue_admin_key(owner=actor, name="second")
    limits = replace(provider.for_instance(), api_operations_per_minute=1)
    monkeypatch.setattr(provider, "for_instance", lambda: limits)
    assert client.get("/api/v1/admin/me", HTTP_AUTHORIZATION="Bearer " + first).status_code == 200
    assert client.get("/api/v1/admin/me", HTTP_AUTHORIZATION="Bearer " + second).status_code == 429
