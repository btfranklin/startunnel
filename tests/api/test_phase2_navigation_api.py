"""The Phase 2 navigation API keeps traversal and resume state safe."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_navigation_api_from_database_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)
    monkeypatch.setattr("api.router.consume_tunnel_creation", lambda credential, limits: None)
    monkeypatch.setattr(
        "api.router.consume_address_miss",
        lambda credential, limits, *, source_ip: None,
    )


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _post(
    client: Client,
    path: str,
    body: dict[str, Any],
    *,
    key: str,
    idempotency_key: str | None = None,
) -> Any:
    headers = _auth(key)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers=headers,
    )


def _create(
    client: Client,
    *,
    key: str,
    operation: str,
) -> dict[str, Any]:
    response = _post(
        client,
        "/api/v1/tunnels",
        {
            "label": operation,
            "cycle": {
                "label": "First cycle",
                "root": {"content": {"type": "text", "text": "Coordinate this work."}},
            },
        },
        key=key,
        idempotency_key=f"{operation}-create",
    )
    assert response.status_code == 201, response.content
    return cast(dict[str, Any], response.json())


def _reply(
    client: Client,
    *,
    key: str,
    address: str,
    parent_id: str,
    operation: str,
) -> dict[str, Any]:
    response = _post(
        client,
        "/api/v1/messages",
        {
            "address": address,
            "parent_id": parent_id,
            "content": {"type": "text", "text": operation},
        },
        key=key,
        idempotency_key=f"{operation}-reply",
    )
    assert response.status_code == 201, response.content
    return cast(dict[str, Any], response.json())


def _safe_error(response: Any, *, status: int, code: str) -> None:
    assert response.status_code == status, response.content
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response["X-Request-ID"]
    assert body["error"]["message"]


def test_navigation_routes_return_coherent_tree_activity_and_context(
    client: Client, credential_factory: Any
) -> None:
    _, sender_key = credential_factory(name="Navigation sender")
    _, reader_key = credential_factory(name="Navigation reader")
    created = _create(client, key=sender_key, operation="navigation-happy")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    first = _reply(
        client,
        key=reader_key,
        address=address,
        parent_id=root_id,
        operation="navigation-first",
    )
    first_id = first["message"]["id"]
    second = _reply(
        client,
        key=sender_key,
        address=address,
        parent_id=first_id,
        operation="navigation-second",
    )

    subtree = _post(
        client,
        "/api/v1/tree/subtree",
        {"address": address, "message_id": root_id},
        key=reader_key,
    )
    assert subtree.status_code == 200
    assert [node["id"] for node in subtree.json()["nodes"]] == [
        root_id,
        first_id,
        second["message"]["id"],
    ]
    assert subtree.json()["truncated"] is False

    leaves = _post(
        client,
        "/api/v1/tree/leaves",
        {"address": address},
        key=reader_key,
    )
    assert leaves.status_code == 200
    assert [message["id"] for message in leaves.json()["leaves"]] == [second["message"]["id"]]

    activity = _post(
        client,
        "/api/v1/activity",
        {"address": address, "after_cursor": created["activity_cursor"]},
        key=reader_key,
    )
    assert activity.status_code == 200
    assert [event["type"] for event in activity.json()["events"]] == [
        "message_posted",
        "message_posted",
    ]
    assert activity.json()["events"][0]["message"]["id"] == first_id

    context = _post(
        client,
        "/api/v1/context",
        {
            "address": address,
            "focus_message_id": first_id,
            "include_recent_activity": False,
        },
        key=reader_key,
    )
    assert context.status_code == 200
    assert [item["reason"] for item in context.json()["items"]] == [
        "ancestor",
        "focus",
        "direct_reply",
    ]

    empty = _post(
        client,
        "/api/v1/activity",
        {"address": address, "after_cursor": activity.json()["next_cursor"], "wait_seconds": 0},
        key=reader_key,
    )
    assert empty.status_code == 200
    assert empty.json()["events"] == []
    assert empty.json()["has_more"] is False


def test_subtree_pages_keep_preorder_snapshot_during_a_later_reply(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create(client, key=key, operation="subtree-page")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    first = _reply(
        client, key=key, address=address, parent_id=root_id, operation="subtree-page-first"
    )
    second = _reply(
        client, key=key, address=address, parent_id=root_id, operation="subtree-page-second"
    )
    nested = _reply(
        client,
        key=key,
        address=address,
        parent_id=first["message"]["id"],
        operation="subtree-page-nested",
    )

    first_page = _post(
        client,
        "/api/v1/tree/subtree",
        {"address": address, "message_id": root_id, "limit": 2},
        key=key,
    )
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert [node["id"] for node in first_body["nodes"]] == [root_id, first["message"]["id"]]
    assert first_body["next_page_cursor"]

    later = _reply(
        client,
        key=key,
        address=address,
        parent_id=nested["message"]["id"],
        operation="subtree-page-later",
    )
    second_page = _post(
        client,
        "/api/v1/tree/subtree",
        {
            "address": address,
            "message_id": root_id,
            "limit": 2,
            "snapshot_cursor": first_body["snapshot_cursor"],
            "page_cursor": first_body["next_page_cursor"],
        },
        key=key,
    )
    assert second_page.status_code == 200
    assert [node["id"] for node in second_page.json()["nodes"]] == [
        nested["message"]["id"],
        second["message"]["id"],
    ]
    assert later["message"]["id"] not in {node["id"] for node in second_page.json()["nodes"]}


def test_leaf_pages_keep_leaf_status_at_the_first_snapshot(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create(client, key=key, operation="leaves-page")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    leaves = [
        _reply(
            client,
            key=key,
            address=address,
            parent_id=root_id,
            operation=f"leaves-page-{index}",
        )
        for index in range(3)
    ]

    first_page = _post(
        client,
        "/api/v1/tree/leaves",
        {"address": address, "limit": 2},
        key=key,
    )
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert [message["id"] for message in first_body["leaves"]] == [
        leaves[0]["message"]["id"],
        leaves[1]["message"]["id"],
    ]

    _reply(
        client,
        key=key,
        address=address,
        parent_id=leaves[2]["message"]["id"],
        operation="leaves-page-later-child",
    )
    second_page = _post(
        client,
        "/api/v1/tree/leaves",
        {
            "address": address,
            "limit": 2,
            "snapshot_cursor": first_body["snapshot_cursor"],
            "page_cursor": first_body["next_page_cursor"],
        },
        key=key,
    )
    assert second_page.status_code == 200
    assert [message["id"] for message in second_page.json()["leaves"]] == [
        leaves[2]["message"]["id"]
    ]
    assert second_page.json()["complete"] is True


def test_cursors_reject_tampering_wrong_purpose_oversize_and_wrong_tunnel(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    first = _create(client, key=key, operation="cursor-first")
    second = _create(client, key=key, operation="cursor-second")
    address = first["tunnel"]["address"]
    root_id = first["cycle"]["root"]["id"]
    subtree = _post(
        client,
        "/api/v1/tree/subtree",
        {"address": address, "message_id": root_id},
        key=key,
    )
    assert subtree.status_code == 200
    snapshot = subtree.json()["snapshot_cursor"]
    changed_last = "A" if snapshot[-1] != "A" else "B"

    tampered = _post(
        client,
        "/api/v1/tree/subtree",
        {
            "address": address,
            "message_id": root_id,
            "snapshot_cursor": snapshot[:-1] + changed_last,
        },
        key=key,
    )
    _safe_error(tampered, status=400, code="invalid_cursor")

    wrong_purpose = _post(
        client,
        "/api/v1/tree/subtree",
        {
            "address": address,
            "message_id": root_id,
            "snapshot_cursor": first["activity_cursor"],
        },
        key=key,
    )
    _safe_error(wrong_purpose, status=400, code="invalid_cursor")

    oversized = _post(
        client,
        "/api/v1/activity",
        {"address": address, "after_cursor": "x" * 4_097},
        key=key,
    )
    _safe_error(oversized, status=400, code="invalid_cursor")

    wrong_tunnel = _post(
        client,
        "/api/v1/activity",
        {
            "address": second["tunnel"]["address"],
            "after_cursor": first["activity_cursor"],
        },
        key=key,
    )
    _safe_error(wrong_tunnel, status=404, code="tunnel_unavailable")


def test_checkpoint_accepts_forward_and_exact_positions_but_rejects_regression(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create(client, key=key, operation="checkpoint")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]

    first = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": address, "cursor": created["activity_cursor"]},
        key=key,
    )
    assert first.status_code == 200
    assert first.json()["advanced"] is True

    reply = _reply(
        client,
        key=key,
        address=address,
        parent_id=root_id,
        operation="checkpoint-forward",
    )
    forward = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": address, "cursor": reply["activity_cursor"]},
        key=key,
    )
    assert forward.status_code == 200
    assert forward.json()["advanced"] is True

    exact = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": address, "cursor": reply["activity_cursor"]},
        key=key,
    )
    assert exact.status_code == 200
    assert exact.json()["advanced"] is False

    regression = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": address, "cursor": created["activity_cursor"]},
        key=key,
    )
    _safe_error(regression, status=409, code="checkpoint_regression")


def test_context_reports_selection_reasons_and_required_branch_budget_failure(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create(client, key=key, operation="context")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    focus = _reply(client, key=key, address=address, parent_id=root_id, operation="context-focus")
    direct = _reply(
        client,
        key=key,
        address=address,
        parent_id=focus["message"]["id"],
        operation="context-direct",
    )
    reference = _reply(
        client, key=key, address=address, parent_id=root_id, operation="context-reference"
    )
    recent = _reply(
        client,
        key=key,
        address=address,
        parent_id=reference["message"]["id"],
        operation="context-recent",
    )

    response = _post(
        client,
        "/api/v1/context",
        {
            "address": address,
            "focus_message_id": focus["message"]["id"],
            "referenced_message_ids": [reference["message"]["id"]],
            "after_cursor": reference["activity_cursor"],
        },
        key=key,
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert [item["reason"] for item in body["items"]] == [
        "ancestor",
        "focus",
        "direct_reply",
        "reference",
        "recent_activity",
    ]
    assert [item["message"]["id"] for item in body["items"]] == [
        root_id,
        focus["message"]["id"],
        direct["message"]["id"],
        reference["message"]["id"],
        recent["message"]["id"],
    ]
    assert body["used_items"] == 5
    assert body["used_bytes"] > 0
    assert body["truncated"] is False

    too_small = _post(
        client,
        "/api/v1/context",
        {
            "address": address,
            "focus_message_id": focus["message"]["id"],
            "max_bytes": 1,
        },
        key=key,
    )
    _safe_error(too_small, status=413, code="context_budget_too_small")


@pytest.mark.parametrize(
    "content",
    [
        {"type": "text", "text": "Review café 🜣."},
        {"type": "json", "value": {"finding": ["café", None, 3]}},
    ],
)
def test_context_preserves_message_data_across_read_routes(
    client: Client, credential_factory: Any, content: dict[str, Any]
) -> None:
    _, key = credential_factory(name="Reviewer café")
    mentioned, _ = credential_factory(name="Reader 🜣")
    created = _create(client, key=key, operation="context-message-data")
    address = created["tunnel"]["address"]
    posted = _post(
        client,
        "/api/v1/messages",
        {
            "address": address,
            "parent_id": created["cycle"]["root"]["id"],
            "content": content,
            "mentions": [str(mentioned.id)],
            "correlation_id": "review-café",
        },
        key=key,
        idempotency_key="context-message-data-reply",
    )
    assert posted.status_code == 201
    message_id = posted.json()["message"]["id"]
    exact = _post(
        client, "/api/v1/tree/message", {"address": address, "message_id": message_id}, key=key
    )
    context = _post(
        client,
        "/api/v1/context",
        {"address": address, "focus_message_id": message_id},
        key=key,
    )
    assert exact.status_code == context.status_code == 200
    focus = next(item for item in context.json()["items"] if item["reason"] == "focus")
    assert focus["message"] == exact.json()["message"]
    assert focus["message"]["content"] == content
    assert focus["message"]["mentions"] == [{"id": str(mentioned.id), "name": "Reader 🜣"}]
    assert focus["message"]["correlation_id"] == "review-café"
