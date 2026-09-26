"""Real PostgreSQL search tests cover ranking, filters, indexes, and isolation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from tunnels.search import search_messages
from tunnels.services import create_tunnel, post_reply

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL search tests.")
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)


def _create(credential: Any, *, key: str) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        label="Search board",
        root_content={"type": "text", "text": "Coordinate the search test."},
    )


def _reply(
    credential: Any,
    created: Any,
    parent: Any,
    *,
    key: str,
    content: dict[str, Any],
    mentions: list[Any] | None = None,
    correlation_id: str = "recovery-plan",
) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent.id,
        content=content,
        mentions=[item.id for item in mentions or []],
        correlation_id=correlation_id,
    ).message


def test_search_finds_text_and_canonical_json_with_safe_ranked_snippets(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Search coordinator")
    worker, _ = credential_factory(name="Search worker")
    created = _create(coordinator, key="search-content-create")
    text = _reply(
        worker,
        created,
        created.root,
        key="search-content-text",
        content={"type": "text", "text": "The database restore needs checksum validation."},
    )
    structured = _reply(
        worker,
        created,
        text,
        key="search-content-json",
        content={
            "type": "json",
            "value": {"database": "restore", "check": "checksum", "ready": False},
        },
    )

    page = search_messages(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        query="database restore checksum",
    )

    assert {hit.message.id for hit in page.hits} == {text.id, structured.id}
    assert all(hit.rank > 0 for hit in page.hits)
    assert all(len(hit.snippet) <= 280 for hit in page.hits)
    assert all("<" not in hit.snippet and ">" not in hit.snippet for hit in page.hits)
    assert page.complete is True
    assert page.used_bytes > 0


def test_search_applies_metadata_branch_and_sequence_filters(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Filter coordinator")
    worker, _ = credential_factory(name="Filter worker")
    mentioned, _ = credential_factory(name="Filter mentioned")
    created = _create(coordinator, key="search-filter-create")
    branch = _reply(
        worker,
        created,
        created.root,
        key="search-filter-branch",
        content={"type": "text", "text": "Inspect the backup database."},
        mentions=[mentioned],
        correlation_id="selected-work",
    )
    nested = _reply(
        worker,
        created,
        branch,
        key="search-filter-nested",
        content={"type": "text", "text": "The backup database is valid."},
        mentions=[mentioned],
        correlation_id="selected-work",
    )
    _reply(
        coordinator,
        created,
        created.root,
        key="search-filter-other",
        content={"type": "text", "text": "A different database branch."},
        correlation_id="other-work",
    )

    page = search_messages(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        query=None,
        sender_id=worker.id,
        mentioned_agent_id=mentioned.id,
        correlation_id="selected-work",
        branch_root_id=branch.id,
        sequence_after=branch.sequence,
        sequence_before=nested.sequence + 1,
    )

    assert [hit.message.id for hit in page.hits] == [nested.id]
    assert page.hits[0].rank == 0


def test_search_cursor_keeps_snapshot_when_a_later_match_is_posted(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Page coordinator")
    worker, _ = credential_factory(name="Page worker")
    created = _create(coordinator, key="search-page-create")
    first = _reply(
        worker,
        created,
        created.root,
        key="search-page-first",
        content={"type": "text", "text": "Search marker one."},
    )
    second = _reply(
        worker,
        created,
        created.root,
        key="search-page-second",
        content={"type": "text", "text": "Search marker two."},
    )

    first_page = search_messages(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        query="marker",
        limit=1,
    )
    assert len(first_page.hits) == 1
    assert first_page.complete is False
    later = _reply(
        worker,
        created,
        created.root,
        key="search-page-later",
        content={"type": "text", "text": "Search marker later."},
    )
    second_page = search_messages(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        query="marker",
        snapshot_sequence=first_page.snapshot_sequence,
        after_rank=first_page.next_after_rank,
        after_sequence=first_page.next_after_sequence,
        after_message_id=first_page.next_after_message_id,
        limit=1,
    )

    assert {first_page.hits[0].message.id, second_page.hits[0].message.id} == {
        first.id,
        second.id,
    }
    assert later.id not in {hit.message.id for hit in second_page.hits}
    assert second_page.complete is True


def test_postgresql_installs_the_expression_gin_index() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'message_content_search'")
        row = cursor.fetchone()

    assert row is not None
    definition = str(row[0]).lower()
    assert "using gin" in definition
    assert "to_tsvector" in definition
    assert "text_payload" in definition
    assert "json_payload" in definition


def test_public_search_and_participant_routes_use_the_postgresql_services(
    client: Client,
    credential_factory: Any,
) -> None:
    coordinator, key = credential_factory(name="HTTP search coordinator")
    worker, _ = credential_factory(name="HTTP search worker")
    created = _create(coordinator, key="search-http-create")
    reply = _reply(
        worker,
        created,
        created.root,
        key="search-http-reply",
        content={"type": "text", "text": "Verify the database restore procedure."},
    )
    headers = {"Authorization": f"Bearer {key}"}

    search_response = client.post(
        "/api/v1/search",
        data=json.dumps(
            {
                "address": created.address,
                "cycle_id": str(created.cycle.id),
                "query": "database restore",
            }
        ),
        content_type="application/json",
        headers=headers,
    )
    assert search_response.status_code == 200, search_response.content
    assert str(reply.id) in {item["message_id"] for item in search_response.json()["results"]}

    participant_response = client.post(
        "/api/v1/participants",
        data=json.dumps({"address": created.address, "cycle_id": str(created.cycle.id)}),
        content_type="application/json",
        headers=headers,
    )
    assert participant_response.status_code == 200, participant_response.content
    assert {str(coordinator.id), str(worker.id)} <= {
        item["agent_id"] for item in participant_response.json()["participants"]
    }


def test_exclusion_search_keeps_zero_rank_matches_across_pages(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, key="search-exclusion-create")
    included = _reply(
        creator,
        created,
        created.root,
        key="search-exclusion-included",
        content={"type": "text", "text": "Included message."},
    )
    _reply(
        creator,
        created,
        created.root,
        key="search-exclusion-excluded",
        content={"type": "text", "text": "Excluded message."},
    )
    first = search_messages(credential=creator, address=created.address, query="-excluded", limit=1)
    assert [hit.message.id for hit in first.hits] == [created.root.id]
    assert first.hits[0].rank == 0
    assert first.complete is False
    second = search_messages(
        credential=creator,
        address=created.address,
        query="-excluded",
        limit=1,
        snapshot_sequence=first.snapshot_sequence,
        after_rank=first.next_after_rank,
        after_sequence=first.next_after_sequence,
        after_message_id=first.next_after_message_id,
    )
    assert [hit.message.id for hit in second.hits] == [included.id]
    assert second.hits[0].rank == 0
    assert second.complete is True
