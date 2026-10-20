"""Search and participant API routes keep scope and cursor contracts."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from django.test import Client

from core.cursors import encode_cursor
from tunnels.cycles import create_tunnel
from tunnels.messages import post_reply
from tunnels.search import SearchHit, SearchPage

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)


def _post(client: Client, path: str, body: dict[str, Any], *, key: str) -> Any:
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}"},
    )


def test_search_returns_safe_metadata_and_binds_filters_to_page_cursor(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential, key = credential_factory(name="Search API agent")
    created = create_tunnel(
        credential=credential,
        idempotency_key="search-api-create",
        root_content={"type": "text", "text": "Database recovery root."},
    )
    reply = post_reply(
        credential=credential,
        idempotency_key="search-api-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Database recovery reply."},
    ).message

    def fake_search(**arguments: Any) -> SearchPage:
        if arguments["after_message_id"] is None:
            return SearchPage(
                tunnel=created.tunnel,
                cycle=created.cycle,
                hits=(
                    SearchHit(message=created.root, snippet="[Database] recovery root.", rank=90),
                ),
                snapshot_sequence=2,
                next_after_rank=90,
                next_after_sequence=1,
                next_after_message_id=created.root.id,
                complete=False,
                used_bytes=290,
            )
        assert arguments["after_rank"] == 90
        assert arguments["after_sequence"] == 1
        assert arguments["after_message_id"] == created.root.id
        return SearchPage(
            tunnel=created.tunnel,
            cycle=created.cycle,
            hits=(SearchHit(message=reply, snippet="Database [recovery] reply.", rank=80),),
            snapshot_sequence=2,
            next_after_rank=None,
            next_after_sequence=None,
            next_after_message_id=None,
            complete=True,
            used_bytes=292,
        )

    monkeypatch.setattr("api.router.search_messages", fake_search)
    body = {"address": created.address, "query": "database recovery", "limit": 1}
    first = _post(client, "/api/v1/search", body, key=key)

    assert first.status_code == 200, first.content
    first_body = first.json()
    assert first_body["results"][0]["message_id"] == str(created.root.id)
    assert "content" not in first_body["results"][0]
    assert first_body["results"][0]["snippet"] == "[Database] recovery root."
    assert first_body["next_page_cursor"]

    second = _post(
        client,
        "/api/v1/search",
        {**body, "page_cursor": first_body["next_page_cursor"]},
        key=key,
    )
    assert second.status_code == 200
    assert second.json()["results"][0]["message_id"] == str(reply.id)
    assert second.json()["complete"] is True

    changed_filter = _post(
        client,
        "/api/v1/search",
        {
            **body,
            "query": "different query",
            "page_cursor": first_body["next_page_cursor"],
        },
        key=key,
    )
    assert changed_filter.status_code == 400
    assert changed_filter.json()["error"]["code"] == "invalid_cursor"


def test_participant_route_pages_one_fixed_snapshot(
    client: Client,
    credential_factory: Any,
) -> None:
    coordinator, key = credential_factory(name="Participant coordinator")
    worker, _ = credential_factory(name="Participant worker")
    observer, _ = credential_factory(name="Participant observer")
    created = create_tunnel(
        credential=coordinator,
        idempotency_key="participants-api-create",
        root_content={"type": "text", "text": "Coordinate participants."},
    )
    post_reply(
        credential=worker,
        idempotency_key="participants-api-worker",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Worker response."},
        mentions=[observer.id],
    )

    first = _post(
        client,
        "/api/v1/participants",
        {"address": created.address, "cycle_id": str(created.cycle.id), "limit": 2},
        key=key,
    )
    assert first.status_code == 200, first.content
    first_body = first.json()
    assert len(first_body["participants"]) == 2
    assert all(item["acknowledged_event_position"] is None for item in first_body["participants"])
    assert all(item["last_advanced_at"] is None for item in first_body["participants"])
    assert first_body["next_page_cursor"]
    assert first_body["complete"] is False

    later, _ = credential_factory(name="Later participant")
    post_reply(
        credential=later,
        idempotency_key="participants-api-later",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Later response."},
    )
    second = _post(
        client,
        "/api/v1/participants",
        {
            "address": created.address,
            "cycle_id": str(created.cycle.id),
            "page_cursor": first_body["next_page_cursor"],
            "limit": 2,
        },
        key=key,
    )
    assert second.status_code == 200
    second_ids = {item["agent_id"] for item in second.json()["participants"]}
    assert str(later.id) not in second_ids
    assert second.json()["complete"] is True


@pytest.mark.django_db(transaction=True)
def test_participant_route_reports_explicit_checkpoint_state(
    client: Client,
    credential_factory: Any,
) -> None:
    coordinator, coordinator_key = credential_factory(name="Acknowledgement coordinator")
    reader, reader_key = credential_factory(name="Acknowledgement reader")
    passive, _ = credential_factory(name="Passive reader")
    created = create_tunnel(
        credential=coordinator,
        idempotency_key="participants-checkpoint-create",
        root_content={"type": "text", "text": "Record acknowledgement."},
    )
    post_reply(
        credential=reader,
        idempotency_key="participants-checkpoint-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "I will inspect the result."},
    )

    def list_page() -> dict[str, Any]:
        response = _post(
            client,
            "/api/v1/participants",
            {"address": created.address},
            key=coordinator_key,
        )
        assert response.status_code == 200, response.content
        items = {item["agent_id"]: item for item in response.json()["participants"]}
        assert str(passive.id) not in items
        return cast(dict[str, Any], items[str(reader.id)])

    before = list_page()
    assert before["acknowledged_event_position"] is None
    assert before["last_advanced_at"] is None

    activity = _post(
        client,
        "/api/v1/activity",
        {"address": created.address, "event_types": ["cycle_started"]},
        key=reader_key,
    )
    assert activity.status_code == 200, activity.content
    assert activity.json()["events"]
    assert list_page()["acknowledged_event_position"] is None

    zero = _post(
        client,
        "/api/v1/activity/checkpoint",
        {
            "address": created.address,
            "cursor": encode_cursor("activity", tunnel_id=str(created.tunnel.id), position=0),
        },
        key=reader_key,
    )
    assert zero.status_code == 200, zero.content
    assert zero.json()["advanced"] is False
    zero_summary = list_page()
    assert zero_summary["acknowledged_event_position"] == 0
    assert zero_summary["last_advanced_at"] is None

    commit = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": created.address, "cursor": activity.json()["next_cursor"]},
        key=reader_key,
    )
    assert commit.status_code == 200, commit.content
    assert commit.json()["advanced"] is True
    advanced_summary = list_page()
    assert advanced_summary["acknowledged_event_position"] == commit.json()["position"]
    assert advanced_summary["last_advanced_at"] == commit.json()["updated_at"]

    repeated = _post(
        client,
        "/api/v1/activity/checkpoint",
        {"address": created.address, "cursor": activity.json()["next_cursor"]},
        key=reader_key,
    )
    assert repeated.status_code == 200, repeated.content
    assert repeated.json()["advanced"] is False
    assert list_page()["last_advanced_at"] == advanced_summary["last_advanced_at"]
