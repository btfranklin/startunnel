"""Admin API requests share domain rules and keep credential types separate."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import time_machine
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.admin_access import issue_admin_key
from accounts.models import AdminOperation, User
from agents.models import AgentCredential
from tunnels.maintenance import cleanup_once
from tunnels.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("name", "limit", "status", "code"),
    [
        ("", 50, 400, "invalid_request"),
        (" " * 3, 50, 400, "invalid_request"),
        ("x" * 101, 50, 400, "invalid_request"),
        ("Second", 1, 429, "quota_exceeded"),
    ],
)
def test_agent_creation_errors_are_safe_audited_and_atomic(
    client: Any,
    user_factory: Any,
    credential_factory: Any,
    settings: Any,
    name: str,
    limit: int,
    status: int,
    code: str,
) -> None:
    actor = user_factory()
    credential_factory(user=actor)
    admin_key, secret = issue_admin_key(owner=actor, name="admin")
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = limit
    response = client.post(
        "/api/v1/admin/agents/create",
        data=json.dumps({"name": name}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        HTTP_AUTHORIZATION="Bearer " + secret,
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert AgentCredential.objects.count() == 1
    assert not AdminOperation.objects.exists()
    event = AuditEvent.objects.get(action="admin.operation_failed")
    assert event.actor_admin_credential == admin_key
    assert event.channel == "admin_api"
    assert event.metadata == {"outcome": "failure", "code": code, "operation": "agents.create"}


def test_account_creation_rejects_case_only_username_collision(
    client: Any, user_factory: Any
) -> None:
    actor = user_factory(username="Operator")
    admin_key, secret = issue_admin_key(owner=actor, name="admin")
    response = client.post(
        "/api/v1/admin/accounts/create",
        data=json.dumps({"username": "operator"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
        HTTP_AUTHORIZATION="Bearer " + secret,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert User.objects.count() == 1
    assert not AdminOperation.objects.exists()
    event = AuditEvent.objects.get(action="admin.operation_failed")
    assert event.actor_admin_credential == admin_key
    assert event.metadata["operation"] == "accounts.create"


def test_admin_tunnel_reads_return_the_stored_current_address(
    client: Any, user_factory: Any, credential_factory: Any
) -> None:
    from tunnels.services import create_tunnel, rotate_address_as_admin

    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    _, secret = issue_admin_key(owner=actor, name="inspection")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    created = create_tunnel(
        credential=credential,
        idempotency_key="inspect-admin-address",
        root_content={"type": "text", "text": "Inspect this tunnel."},
    )
    path = f"/api/v1/admin/tunnels/{created.tunnel.id}"
    assert client.get(path, **headers).json()["address"] == created.address
    rotated = rotate_address_as_admin(
        actor=actor, tunnel_id=created.tunnel.id, expected_address_generation=1
    )
    listed = client.get("/api/v1/admin/tunnels", **headers).json()["items"]
    assert listed[0]["address"] == rotated.address
    assert client.get(path, **headers).json()["address"] == rotated.address
    assert rotated.address not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))


def test_admin_tunnel_list_batches_address_and_active_cycle_reads(
    client: Any, user_factory: Any, credential_factory: Any
) -> None:
    from tunnels.services import create_tunnel

    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    expected = []
    for index in range(12):
        expected.append(
            create_tunnel(
                credential=credential,
                idempotency_key=f"admin-list-{index}",
                root_content={"type": "text", "text": f"Root {index}"},
            )
        )
    _, secret = issue_admin_key(owner=actor, name="batch inspection")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    client.get("/api/v1/admin/tunnels?limit=1", **headers)
    with CaptureQueriesContext(connection) as one_tunnel_queries:
        small = client.get("/api/v1/admin/tunnels?limit=1", **headers)
    with CaptureQueriesContext(connection) as many_tunnel_queries:
        many = client.get("/api/v1/admin/tunnels?limit=20", **headers)

    assert small.status_code == many.status_code == 200
    assert len(many.json()["items"]) == 12
    assert len(many_tunnel_queries) == len(one_tunnel_queries)
    resources = {item["address"]: item for item in many.json()["items"]}
    for tunnel in expected:
        item = resources[tunnel.address]
        assert item["address_generation"] == 1
        assert item["active_cycle_id"] == str(tunnel.cycle.id)


def test_admin_key_list_uses_one_owner_read_for_each_page(client: Any, user_factory: Any) -> None:
    actor = user_factory()
    for index in range(8):
        issue_admin_key(owner=actor, name=f"owner read {index}")
    _, secret = issue_admin_key(owner=actor, name="owner read admin")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    client.get("/api/v1/admin/keys?limit=1", **headers)
    with CaptureQueriesContext(connection) as one_key_queries:
        small = client.get("/api/v1/admin/keys?limit=1", **headers)
    with CaptureQueriesContext(connection) as many_key_queries:
        many = client.get("/api/v1/admin/keys?limit=20", **headers)

    assert small.status_code == many.status_code == 200
    assert len(many.json()["items"]) == 9
    assert all(item["available"] for item in many.json()["items"])
    assert len(many_key_queries) == len(one_key_queries)


def test_admin_reports_configured_credential_limit(
    client: Any, user_factory: Any, settings: Any
) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 75
    actor = user_factory()
    _, secret = issue_admin_key(owner=actor, name="limit inspection")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}

    status = client.get("/api/v1/admin/status", **headers)
    capabilities = client.get("/api/v1/admin/capabilities", **headers)

    assert status.json()["capacity"]["active_credential_limit"] == 75
    assert status.json()["limits"]["active_credentials"] == 75
    assert capabilities.json()["limits"]["active_credentials"] == 75


def test_last_admin_access_can_be_removed_and_server_recovery_restores_it(
    client: Any, user_factory: Any, tmp_path: Path
) -> None:
    actor = user_factory()
    credential, first_secret = issue_admin_key(
        owner=actor, name="only key", expires_at=timezone.now() + timedelta(hours=1)
    )
    headers = {"HTTP_AUTHORIZATION": "Bearer " + first_secret}

    def mutate(path: str, body: dict[str, Any], key: str, auth: dict[str, str]) -> Any:
        return client.post(
            path,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=key,
            **auth,
        )

    removed_password = mutate(
        "/api/v1/admin/accounts/password",
        {"admin_id": str(actor.pk), "password": None},
        "last-password-removal",
        headers,
    )
    assert removed_password.status_code == 200, removed_password.content
    assert not type(actor).objects.get(pk=actor.pk).has_usable_password()
    revoked = mutate(
        "/api/v1/admin/keys/revoke",
        {"key_id": str(credential.pk)},
        "last-key-revocation",
        headers,
    )
    assert revoked.status_code == 200, revoked.content
    assert client.get("/api/v1/admin/me", **headers).status_code == 401

    recovery_file = tmp_path / "recovered.key"
    call_command("recover_instance_admin", actor.username, key_file=str(recovery_file))
    recovered_headers = {"HTTP_AUTHORIZATION": "Bearer " + recovery_file.read_text().strip()}
    assert client.get("/api/v1/admin/me", **recovered_headers).status_code == 200
    deactivated = mutate(
        "/api/v1/admin/accounts/state",
        {"admin_id": str(actor.pk), "active": False, "expected_active": True},
        "last-account-deactivation",
        recovered_headers,
    )
    assert deactivated.status_code == 200, deactivated.content
    assert client.get("/api/v1/admin/me", **recovered_headers).status_code == 401

    second_recovery_file = tmp_path / "recovered-again.key"
    call_command("recover_instance_admin", actor.username, key_file=str(second_recovery_file))
    final_headers = {"HTTP_AUTHORIZATION": "Bearer " + second_recovery_file.read_text().strip()}
    assert client.get("/api/v1/admin/me", **final_headers).status_code == 200


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
    other_headers = {"HTTP_AUTHORIZATION": "Bearer " + other_secret}
    other_inspect = client.get(
        "/api/v1/admin/operations/" + created["operation"]["id"], **other_headers
    )
    assert other_inspect.status_code == 200
    assert other_inspect.json() == inspect.json()
    first_page = client.get("/api/v1/admin/accounts", {"limit": 1}, **headers).json()
    query = {"limit": 1, "cursor": first_page["next_cursor"]}
    original_page = client.get("/api/v1/admin/accounts", query, **headers)
    handed_over_page = client.get("/api/v1/admin/accounts", query, **other_headers)
    assert handed_over_page.status_code == 200
    assert handed_over_page.json() == original_page.json()


def test_admin_receipt_expires_before_physical_cleanup(client: Any, user_factory: Any) -> None:
    actor = user_factory()
    _, secret = issue_admin_key(owner=actor, name="receipt inspection")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    response = client.post(
        "/api/v1/admin/accounts/create",
        data=json.dumps({"username": "receipt-expiry"}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="receipt-expiry-create",
        **headers,
    )
    assert response.status_code == 200
    result = response.json()
    receipt = AdminOperation.objects.get(pk=result["operation"]["id"])
    assert (
        timedelta(hours=24, seconds=-1)
        < receipt.expires_at - receipt.created_at
        <= timedelta(hours=24)
    )
    path = f"/api/v1/admin/operations/{receipt.pk}"
    with time_machine.travel(receipt.expires_at - timedelta(microseconds=1), tick=False):
        assert client.get(path, **headers).status_code == 200
    with time_machine.travel(receipt.expires_at, tick=False):
        assert client.get(path, **headers).status_code == 400
        assert AdminOperation.objects.filter(pk=receipt.pk).exists()
        assert cleanup_once()["admin_operations_purged"] == 1
        assert not AdminOperation.objects.filter(pk=receipt.pk).exists()
        assert AuditEvent.objects.filter(pk=result["operation"]["audit_event_id"]).exists()
        assert client.get("/api/v1/admin/me", **headers).status_code == 200


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


def test_admin_reads_do_not_consume_the_owner_shared_write_limit(
    client: Any, user_factory: Any, monkeypatch: Any
) -> None:
    from dataclasses import replace

    from core import limits as instance_limits

    actor = user_factory()
    _, first = issue_admin_key(owner=actor, name="first")
    _, second = issue_admin_key(owner=actor, name="second")
    limits = replace(instance_limits.effective_limits(), api_operations_per_minute=1)
    monkeypatch.setattr(instance_limits, "effective_limits", lambda: limits)
    for secret in (first, second):
        assert (
            client.get("/api/v1/admin/me", HTTP_AUTHORIZATION="Bearer " + secret).status_code == 200
        )
    for index, secret in enumerate((first, second)):
        response = client.post(
            "/api/v1/admin/agents/create",
            data=json.dumps({"name": f"worker-{index}"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + secret,
            HTTP_IDEMPOTENCY_KEY=f"shared-admin-write-{index}",
        )
        assert response.status_code == (200 if index == 0 else 429)
    assert (
        client.get("/api/v1/admin/status", HTTP_AUTHORIZATION="Bearer " + second).status_code == 200
    )
    assert AdminOperation.objects.count() == 1
