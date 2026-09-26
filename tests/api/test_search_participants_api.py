"""Search and participant API routes keep scope and cursor contracts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client

from tunnels.search import SearchHit, SearchPage
from tunnels.services import create_tunnel, post_reply

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
