"""Entry returns a consistent snapshot and bounds the complete wire response."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from django.test import Client

from core.cursors import decode_cursor
from tunnels.models import Message
from tunnels.services import (
    close_cycle,
    create_tunnel,
    post_reply,
    retire_tunnel_as_operator,
    retirement_confirmation,
    rotate_address_as_operator,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)
    monkeypatch.setattr(
        "api.router.consume_address_miss",
        lambda credential, limits, *, source_ip: None,
    )


def _post(client: Client, path: str, body: dict[str, Any], *, key: str) -> Any:
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}"},
    )


def _create(credential: Any, key: str = "entry-api-create") -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "A Unicode root: café 🜣."},
    )


def _reply(created: Any, credential: Any, key: str, text: str) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": text},
    )


def test_enter_returns_root_once_and_a_continuation_before_recent_messages(
    client: Client,
    credential_factory: Any,
) -> None:
    creator, key = credential_factory(name="Entry API creator")
    created = _create(creator)
    replies = [
        _reply(created, creator, f"entry-api-reply-{index:04d}", f"Reply {index}.")
        for index in range(1, 6)
    ]

    response = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "limit": 3},
        key=key,
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "active"
    assert body["cycle"]["id"] == str(created.cycle.id)
    assert body["root"]["id"] == str(created.root.id)
    assert [item["id"] for item in body["recent_messages"]] == [
        str(replies[-2].message.id),
        str(replies[-1].message.id),
    ]
    assert body["root"]["id"] not in {item["id"] for item in body["recent_messages"]}
    assert body["history_omitted"] is True
    assert body["history_cursor"]
    assert body["snapshot_sequence"] == 6
    assert len(response.content) <= 262_144

    _reply(created, creator, "entry-api-after-snapshot", "After the snapshot.")
    page = _post(
        client,
        "/api/v1/tree",
        {"address": created.address, "page_cursor": body["history_cursor"]},
        key=key,
    )
    assert page.status_code == 200, page.content
    assert [item["id"] for item in page.json()["nodes"]] == [
        str(replies[0].message.id),
        str(replies[1].message.id),
        str(replies[2].message.id),
    ]

    activity = _post(
        client,
        "/api/v1/activity",
        {"address": created.address, "after_cursor": body["activity_cursor"]},
        key=key,
    )
    assert activity.status_code == 200, activity.content
    assert [event["message"]["content"]["text"] for event in activity.json()["events"]] == [
        "After the snapshot."
    ]


def test_enter_enforces_full_response_bytes_and_fails_when_root_cannot_fit(
    client: Client,
    credential_factory: Any,
) -> None:
    creator, key = credential_factory(name="Entry budget creator")
    created = _create(creator, "entry-budget-create")
    _reply(created, creator, "entry-budget-reply", "🜣 café " * 100)

    response = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "limit": 10, "max_bytes": 1800},
        key=key,
    )

    assert response.status_code == 200, response.content
    body = response.json()
    assert len(response.content) <= 1800
    assert body["recent_messages"] == []
    assert body["history_omitted"] is True
    assert body["history_cursor"]

    too_small = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "max_bytes": 1},
        key=key,
    )
    assert too_small.status_code == 413
    assert too_small.json()["error"]["code"] == "entry_budget_too_small"


def test_enter_accepts_exact_wire_size_and_trims_one_byte_below(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator, key = credential_factory(name="Exact entry budget creator")
    created = _create(creator, "entry-exact-budget-create")
    reply = _reply(created, creator, "entry-exact-budget-reply", "Unicode: café 🜣.")
    Message.objects.filter(pk=created.root.id).update(
        created_at=datetime(2026, 10, 18, 1, 2, 3, 123, tzinfo=UTC)
    )
    Message.objects.filter(pk=reply.message.id).update(
        created_at=datetime(2026, 10, 18, 1, 2, 4, 123, tzinfo=UTC)
    )
    monkeypatch.setattr("core.cursors.time.time", lambda: 1_800_000_000)

    full = _post(client, "/api/v1/enter", {"address": created.address}, key=key)
    assert full.status_code == 200, full.content
    full_size = len(full.content)
    assert full.json()["recent_messages"]

    exact = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "max_bytes": full_size},
        key=key,
    )
    assert exact.status_code == 200, exact.content
    assert len(exact.content) == full_size
    assert exact.json()["recent_messages"]

    below = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "max_bytes": full_size - 1},
        key=key,
    )
    assert below.status_code == 200, below.content
    assert len(below.content) <= full_size - 1
    assert below.json()["recent_messages"] == []


def test_enter_returns_status_without_closed_history(
    client: Client, credential_factory: Any
) -> None:
    creator, key = credential_factory(name="Closed entry creator")
    created = _create(creator, "entry-closed-create")
    close_cycle(
        credential=creator,
        idempotency_key="entry-closed-cycle",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )

    response = _post(client, "/api/v1/enter", {"address": created.address}, key=key)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dormant"
    assert body["cycle"] is None
    assert body["root"] is None
    assert body["recent_messages"] == []
    assert body["history_cursor"] is None
    assert body["snapshot_sequence"] is None
    assert len(response.content) <= 262_144


def test_entry_rechecks_rotated_and_retired_addresses(
    client: Client,
    credential_factory: Any,
    user_factory: Any,
) -> None:
    creator, key = credential_factory(name="Lifecycle entry creator")
    operator = user_factory()
    created = _create(creator, "entry-lifecycle-create")
    rotated = rotate_address_as_operator(
        actor=operator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )

    old_address = _post(client, "/api/v1/enter", {"address": created.address}, key=key)
    current_address = _post(client, "/api/v1/enter", {"address": rotated.address}, key=key)
    assert old_address.status_code == 404
    assert current_address.status_code == 200

    retire_tunnel_as_operator(
        actor=operator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=2,
        confirmation=retirement_confirmation(created.tunnel),
    )
    retired = _post(client, "/api/v1/enter", {"address": rotated.address}, key=key)
    assert retired.status_code == 404


def test_entry_history_cursor_expires(
    client: Client, credential_factory: Any, monkeypatch: Any
) -> None:
    creator, key = credential_factory(name="Expired entry creator")
    created = _create(creator, "entry-expired-create")
    _reply(created, creator, "entry-expired-reply", "Older than the returned root.")
    response = _post(
        client,
        "/api/v1/enter",
        {"address": created.address, "limit": 1},
        key=key,
    )
    cursor = response.json()["history_cursor"]
    decoded = decode_cursor(cursor, kind="tree-page")
    monkeypatch.setattr("core.cursors.time.time", lambda: decoded["expires_at"] + 1)

    expired = _post(
        client,
        "/api/v1/tree",
        {"address": created.address, "page_cursor": cursor},
        key=key,
    )

    assert expired.status_code == 400
    assert expired.json()["error"]["code"] == "invalid_cursor"
