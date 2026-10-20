"""Administrators can inspect and retire selected tunnels from revoked keys."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from accounts.admin_access import issue_admin_key
from accounts.models import AdminReceipt
from agents.services import revoke_credential
from core.cursors import decode_cursor, encode_cursor
from core.models import AuditEvent
from tunnels.admin_actions import retire_tunnel_as_admin, retirement_confirmation
from tunnels.beacons import publish_beacon
from tunnels.cycles import create_tunnel
from tunnels.models import Beacon, Message, Tunnel

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        label="Shared recovery work",
        root_content={"type": "text", "text": "Keep this retained history."},
    )


def _selection(created: Any) -> dict[str, Any]:
    return {
        "tunnel_id": str(created.tunnel.id),
        "expected_address_generation": 1,
        "confirmation": retirement_confirmation(created.tunnel),
    }


def _retire(client: Any, secret: str, data: dict[str, Any], key: str) -> Any:
    return client.post(
        "/api/v1/admin/tunnels/retire-for-credential",
        data=json.dumps(data),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer " + secret,
        HTTP_IDEMPOTENCY_KEY=key,
    )


def test_creator_filter_paginates_and_keeps_old_unfiltered_cursors(
    client: Any, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    _, secret = issue_admin_key(owner=actor, name="recovery")
    headers = {"HTTP_AUTHORIZATION": "Bearer " + secret}
    first, _ = credential_factory()
    other, _ = credential_factory()
    created = [_create(first, "filter-first"), _create(first, "filter-second")]
    created.append(_create(other, "filter-other"))
    expected = sorted(str(item.tunnel.id) for item in created[:2])
    query = {"creator_id": str(first.id), "limit": 1}
    page = client.get("/api/v1/admin/tunnels", query, **headers).json()
    assert [row["id"] for row in page["items"]] == expected[:1]
    rest = client.get(
        "/api/v1/admin/tunnels", {**query, "cursor": page["next_cursor"]}, **headers
    ).json()
    assert [row["id"] for row in rest["items"]] == expected[1:]
    assert rest["next_cursor"] is None
    for filters in ({}, {"creator_id": str(other.id)}):
        response = client.get(
            "/api/v1/admin/tunnels", {**filters, "cursor": page["next_cursor"]}, **headers
        )
        assert response.status_code == 400

    ordered = sorted(str(item.tunnel.id) for item in created)
    old_filters = dict.fromkeys(
        (
            "state",
            "admin_id",
            "tunnel_id",
            "action",
            "actor_id",
            "target_type",
            "target_id",
            "credential_id",
        )
    )
    old_cursor = encode_cursor(
        "admin_tunnels", after=ordered[0], after_at=None, filters=old_filters
    )
    response = client.get("/api/v1/admin/tunnels", {"cursor": old_cursor}, **headers)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ordered[1:]
    default_page = client.get("/api/v1/admin/tunnels", {"limit": 1}, **headers).json()
    decoded = decode_cursor(default_page["next_cursor"], kind="admin_tunnels")
    assert "creator_id" not in decoded["filters"]


def test_bulk_retirement_keeps_history_and_replays_safe_result(
    client: Any, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    admin_key, secret = issue_admin_key(owner=actor, name="recovery")
    credential, _ = credential_factory()
    created = [_create(credential, "recovery-first"), _create(credential, "recovery-second")]
    beacon_data, _ = publish_beacon(
        credential=credential,
        address=created[0].address,
        text="Shared work needs recovery.",
        idempotency_key="recovery-beacon",
    )
    revoke_credential(actor=actor, credential=credential)
    assert Tunnel.objects.filter(state="active").count() == 2
    body: dict[str, Any] = {
        "credential_id": str(credential.id),
        "tunnels": [_selection(row) for row in created],
    }
    key = "recovery-selected-tunnels"
    response = _retire(client, secret, body, key)
    assert response.status_code == 200, response.content
    result = response.json()
    assert result["resource"] == {
        "credential_id": str(credential.id),
        "retired_tunnel_ids": sorted(str(row.tunnel.id) for row in created),
    }
    assert Tunnel.objects.filter(state="retired").count() == 2
    assert Message.objects.count() == 2
    for row in created:
        row.cycle.refresh_from_db()
        assert row.cycle.state == "closed"
        assert Message.objects.get(pk=row.root.id).text_payload == "Keep this retained history."
        assert not row.tunnel.addresses.filter(state="current").exists()
    assert Beacon.objects.get(pk=beacon_data["id"]).ended_at is not None
    assert _retire(client, secret, body, key).json() == result
    changed = {**body, "tunnels": body["tunnels"][:1]}
    assert _retire(client, secret, changed, key).status_code == 409
    operation = client.get(
        "/api/v1/admin/operations/" + result["operation"]["id"],
        HTTP_AUTHORIZATION="Bearer " + secret,
    )
    assert operation.status_code == 200
    assert operation.json()["result"]["resource"] == result["resource"]
    assert AdminReceipt.objects.count() == 1
    audit = AuditEvent.objects.get(pk=result["operation"]["audit_event_id"])
    assert audit.action == "tunnels.retire-for-credential"
    assert audit.target_id == credential.id
    assert audit.actor_admin_credential == admin_key
    assert AuditEvent.objects.filter(action="tunnel.retired").count() == 2


@pytest.mark.parametrize(
    ("fault", "status"),
    [
        ("active_credential", 400),
        ("missing_credential", 400),
        ("wrong_creator", 400),
        ("missing_tunnel", 404),
        ("duplicate", 400),
        ("confirmation", 400),
        ("generation", 409),
        ("retired", 404),
    ],
)
def test_bulk_retirement_failure_rolls_back_every_target(
    client: Any, user_factory: Any, credential_factory: Any, fault: str, status: int
) -> None:
    actor = user_factory()
    _, secret = issue_admin_key(owner=actor, name="recovery")
    credential, _ = credential_factory()
    created = sorted(
        [_create(credential, "rollback-first"), _create(credential, "rollback-second")],
        key=lambda row: row.tunnel.id,
    )
    body: dict[str, Any] = {
        "credential_id": str(credential.id),
        "tunnels": [_selection(row) for row in created],
    }
    if fault != "active_credential":
        revoke_credential(actor=actor, credential=credential)
    if fault == "missing_credential":
        body["credential_id"] = str(uuid4())
    elif fault == "wrong_creator":
        other, _ = credential_factory()
        body["tunnels"][1] = _selection(_create(other, "rollback-other"))
    elif fault == "missing_tunnel":
        body["tunnels"][1]["tunnel_id"] = str(uuid4())
    elif fault == "duplicate":
        body["tunnels"][1] = body["tunnels"][0]
    elif fault == "confirmation":
        body["tunnels"][1]["confirmation"] = "wrong confirmation"
    elif fault == "generation":
        body["tunnels"][1]["expected_address_generation"] = 2
    elif fault == "retired":
        retire_tunnel_as_admin(actor=actor, **_selection(created[1]))
    before = list(Tunnel.objects.order_by("id").values("id", "state", "next_event_position"))
    retired_audits = AuditEvent.objects.filter(action="tunnel.retired").count()
    response = _retire(client, secret, body, "recovery-rollback")
    assert response.status_code == status, response.content
    assert (
        list(Tunnel.objects.order_by("id").values("id", "state", "next_event_position")) == before
    )
    assert not AdminReceipt.objects.exists()
    assert AuditEvent.objects.filter(action="tunnel.retired").count() == retired_audits
    assert AuditEvent.objects.filter(action="admin.operation_failed").count() == 1


@pytest.mark.parametrize("count", [0, 501])
def test_bulk_retirement_request_is_bounded(client: Any, user_factory: Any, count: int) -> None:
    _, secret = issue_admin_key(owner=user_factory(), name="recovery")
    selection = {"tunnel_id": str(uuid4()), "expected_address_generation": 1, "confirmation": "x"}
    response = _retire(
        client,
        secret,
        {"credential_id": str(uuid4()), "tunnels": [selection] * count},
        "recovery-bounds",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["field_errors"]
    assert not AdminReceipt.objects.exists()
