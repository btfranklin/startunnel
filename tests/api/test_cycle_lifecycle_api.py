"""The public lifecycle API preserves one stable address across bounded cycles."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import pytest
from django.test import Client

from tunnels.admin_actions import rotate_address_as_admin
from tunnels.cycles import create_tunnel

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)


def _post(
    client: Client,
    path: str,
    body: dict[str, Any],
    *,
    key: str,
    idempotency_key: str | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers=headers,
    )


def _create(
    client: Client,
    key: str,
    *,
    idempotency_key: str = "api-lifecycle-create",
) -> dict[str, Any]:
    response = _post(
        client,
        "/api/v1/tunnels",
        {
            "label": "Stable release board",
            "cycle": {
                "label": "First review",
                "root": {"content": {"type": "text", "text": "Review release 42."}},
            },
        },
        key=key,
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 201, response.content
    return cast(dict[str, Any], response.json())


def _cycle_body(address: str, *, text: str) -> dict[str, Any]:
    return {
        "address": address,
        "expected_address_generation": 1,
        "cycle": {
            "label": text,
            "root": {"content": {"type": "text", "text": text}},
        },
    }


@pytest.mark.parametrize(
    ("kind", "character", "length"),
    [
        (kind, character, length)
        for kind in ("text", "json")
        for character in ("x", "🜣")
        for length in (279, 280, 281)
    ]
    + [(kind, "x", 65536) for kind in ("text", "json")]
    + [("text", "x", 0), ("json", "x", 2)],
)
def test_cycle_directory_preserves_content_previews_and_metadata(
    client: Client, credential_factory: Any, kind: str, character: str, length: int
) -> None:
    creator, key = credential_factory(name="Coordinator")
    if kind == "text":
        raw = character * length
        content = {"type": "text", "text": raw}
    else:
        value = character * (length - 2)
        content = {"type": "json", "value": value}
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    created = create_tunnel(
        credential=creator,
        idempotency_key="cycle-preview-create",
        root_content=content,
        cycle_label="Preview boundary",
        root_correlation_id="preview-root",
        root_session="review",
    )

    response = _post(client, "/api/v1/cycles", {"address": created.address}, key=key)

    assert response.status_code == 200, response.content
    page = response.json()
    assert page["complete"] is True
    assert page["next_page_cursor"] is None
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["root"] == {
        "id": str(created.root.id),
        "sender": {"id": str(creator.id), "name": "Coordinator", "session": "review"},
        "correlation_id": "preview-root",
        "content_preview": raw if len(raw) <= 280 else raw[:279] + "…",
    }
    assert item["id"] == str(created.cycle.id)
    assert item["number"] == 1
    assert item["label"] == "Preview boundary"
    assert item["state"] == "active"
    assert item["message_count"] == 1
    assert item["content_bytes"] == len(raw.encode("utf-8"))
    for field in ("created_at", "expires_at"):
        expected = getattr(created.cycle, field)
        assert datetime.fromisoformat(item[field]) == expected.replace(
            microsecond=expected.microsecond // 1000 * 1000
        )
    assert item["closed_at"] is None
    assert item["delete_after"] is None


def test_cycle_directory_preserves_json_null(client: Client, credential_factory: Any) -> None:
    creator, key = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="cycle-preview-null",
        root_content={"type": "json", "value": None},
    )

    response = _post(client, "/api/v1/cycles", {"address": created.address}, key=key)

    assert response.status_code == 200, response.content
    assert response.json()["items"][0]["root"]["content_preview"] == "null"


def test_status_close_start_rollover_and_cycle_history(
    client: Client, credential_factory: Any
) -> None:
    _, coordinator_key = credential_factory(name="Coordinator")
    _, reader_key = credential_factory(name="Reader")
    created = _create(client, coordinator_key)
    address = created["tunnel"]["address"]
    first_cycle_id = created["cycle"]["id"]

    active_status = _post(
        client,
        "/api/v1/tunnel/status",
        {"address": address},
        key=reader_key,
    )
    assert active_status.status_code == 200
    assert active_status.json()["state"] == "active"
    assert active_status.json()["address_generation"] == 1
    assert active_status.json()["current_cycle"]["id"] == first_cycle_id

    closed = _post(
        client,
        "/api/v1/cycles/close",
        {"address": address, "expected_cycle_id": first_cycle_id},
        key=coordinator_key,
        idempotency_key="api-lifecycle-close",
    )
    assert closed.status_code == 204
    dormant_status = _post(
        client,
        "/api/v1/tunnel/status",
        {"address": address},
        key=reader_key,
    )
    assert dormant_status.json()["state"] == "dormant"
    assert dormant_status.json()["current_cycle"] is None

    started = _post(
        client,
        "/api/v1/cycles/start",
        _cycle_body(address, text="Review release 43."),
        key=coordinator_key,
        idempotency_key="api-lifecycle-start",
    )
    assert started.status_code == 201, started.content
    assert started.json()["cycle"]["number"] == 2
    second_cycle_id = started.json()["cycle"]["id"]
    replay = _post(
        client,
        "/api/v1/cycles/start",
        _cycle_body(address, text="Review release 43."),
        key=coordinator_key,
        idempotency_key="api-lifecycle-start",
    )
    assert replay.status_code == 201
    assert replay.json()["cycle"]["id"] == second_cycle_id
    assert replay.json()["idempotent_replay"] is True

    rollover_body = {
        **_cycle_body(address, text="Review release 44."),
        "expected_cycle_id": second_cycle_id,
    }
    rollover = _post(
        client,
        "/api/v1/cycles/rollover",
        rollover_body,
        key=coordinator_key,
        idempotency_key="api-lifecycle-rollover",
    )
    assert rollover.status_code == 201, rollover.content
    assert rollover.json()["cycle"]["number"] == 3

    first_page = _post(
        client,
        "/api/v1/cycles",
        {"address": address, "limit": 2},
        key=reader_key,
    )
    assert first_page.status_code == 200, first_page.content
    page = first_page.json()
    assert [item["number"] for item in page["items"]] == [3, 2]
    assert page["items"][0]["root"]["content_preview"] == "Review release 44."
    assert page["items"][1]["state"] == "closed"
    assert page["next_page_cursor"]
    second_page = _post(
        client,
        "/api/v1/cycles",
        {
            "address": address,
            "limit": 2,
            "page_cursor": page["next_page_cursor"],
        },
        key=reader_key,
    )
    assert [item["number"] for item in second_page.json()["items"]] == [1]
    assert second_page.json()["complete"] is True


def test_lifecycle_writes_require_the_creator_and_current_expected_state(
    client: Client, credential_factory: Any
) -> None:
    _, coordinator_key = credential_factory()
    _, outside_key = credential_factory()
    created = _create(client, coordinator_key)
    address = created["tunnel"]["address"]
    body = {
        **_cycle_body(address, text="Unauthorized rollover."),
        "expected_cycle_id": created["cycle"]["id"],
    }

    read_status = _post(
        client,
        "/api/v1/tunnel/status",
        {"address": address},
        key=outside_key,
    )
    assert read_status.status_code == 200
    outside_rollover = _post(
        client,
        "/api/v1/cycles/rollover",
        body,
        key=outside_key,
        idempotency_key="api-lifecycle-outside",
    )
    assert outside_rollover.status_code == 404
    assert outside_rollover.json()["error"]["code"] == "tunnel_unavailable"
    stale = _post(
        client,
        "/api/v1/cycles/rollover",
        {**body, "expected_address_generation": 2},
        key=coordinator_key,
        idempotency_key="api-lifecycle-stale-generation",
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "lifecycle_conflict"


def test_start_replay_keeps_its_original_response_until_address_rotation(
    client: Client, credential_factory: Any
) -> None:
    coordinator, key = credential_factory()
    creation_replay_value = "api-exact-start-create"
    created = _create(client, key, idempotency_key=creation_replay_value)
    address = created["tunnel"]["address"]
    close = _post(
        client,
        "/api/v1/cycles/close",
        {"address": address, "expected_cycle_id": created["cycle"]["id"]},
        key=key,
        idempotency_key="api-exact-start-close-first",
    )
    assert close.status_code == 204
    body = _cycle_body(address, text="Exact second cycle.")
    first = _post(
        client,
        "/api/v1/cycles/start",
        body,
        key=key,
        idempotency_key="api-exact-start-request",
    )
    assert first.status_code == 201
    original = first.json()
    close = _post(
        client,
        "/api/v1/cycles/close",
        {"address": address, "expected_cycle_id": original["cycle"]["id"]},
        key=key,
        idempotency_key="api-exact-start-close-second",
    )
    assert close.status_code == 204
    replay = _post(
        client,
        "/api/v1/cycles/start",
        body,
        key=key,
        idempotency_key="api-exact-start-request",
    )

    assert replay.status_code == 201
    replay_body = replay.json()
    original["idempotent_replay"] = True
    assert replay_body == original

    rotate_address_as_admin(
        actor=coordinator.created_by,
        tunnel_id=UUID(created["tunnel"]["id"]),
        expected_address_generation=1,
    )
    retired_address_replay = _post(
        client,
        "/api/v1/cycles/start",
        body,
        key=key,
        idempotency_key="api-exact-start-request",
    )
    assert retired_address_replay.status_code == 404
    assert retired_address_replay.json()["error"]["code"] == "tunnel_unavailable"


def test_every_lifecycle_read_and_write_with_a_retired_address_is_unavailable(
    client: Client, credential_factory: Any
) -> None:
    coordinator, coordinator_key = credential_factory()
    created = _create(client, coordinator_key)
    retired_address = created["tunnel"]["address"]
    rotate_address_as_admin(
        actor=coordinator.created_by,
        tunnel_id=UUID(created["tunnel"]["id"]),
        expected_address_generation=1,
    )
    requests = [
        ("/api/v1/tunnel/status", {"address": retired_address}, None),
        ("/api/v1/cycles", {"address": retired_address}, None),
        (
            "/api/v1/cycles/start",
            _cycle_body(retired_address, text="Must not start."),
            "retired-address-start",
        ),
        (
            "/api/v1/cycles/rollover",
            {
                **_cycle_body(retired_address, text="Must not roll over."),
                "expected_cycle_id": created["cycle"]["id"],
            },
            "retired-address-rollover",
        ),
        (
            "/api/v1/cycles/close",
            {
                "address": retired_address,
                "expected_cycle_id": created["cycle"]["id"],
            },
            "retired-address-close",
        ),
    ]

    for path, body, idempotency_key in requests:
        response = _post(
            client,
            path,
            body,
            key=coordinator_key,
            idempotency_key=idempotency_key,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "tunnel_unavailable"
