"""Real PostgreSQL search tests cover ranking, filters, indexes, and isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import BigIntegerField, TextField, Value
from django.db.models.functions import Cast, Coalesce
from django.test import Client
from django.test.utils import CaptureQueriesContext

from core.limits import effective_limits
from tunnels.cycles import create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Message
from tunnels.search import SearchHit, search_hit_payload, search_messages

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def allow_api_operations(monkeypatch: pytest.MonkeyPatch) -> None:
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
    session: str | None = None,
) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent.id,
        content=content,
        mentions=[item.id for item in mentions or []],
        correlation_id=correlation_id,
        session=session,
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


@pytest.mark.parametrize("kind", ["text", "json"])
@pytest.mark.parametrize("query", [None, "needle"])
def test_search_bounds_large_payloads_before_model_loading(
    credential_factory: Any, kind: str, query: str | None
) -> None:
    coordinator, _ = credential_factory()
    worker, _ = credential_factory()
    mentioned, _ = credential_factory()
    created = _create(coordinator, key="search-bounded-create")
    seed = "needle " + "café" * 80 + " "
    payload_bytes = 65536 - (2 if kind == "json" else 0)
    repeats, remainder = divmod(payload_bytes, len(seed.encode("utf-8")))
    value = seed * repeats + "x" * remainder
    if kind == "text":
        raw = value
        content = {"type": "text", "text": value}
    else:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        content = {"type": "json", "value": value}
    assert len(raw.encode("utf-8")) == 65536
    reply = _reply(
        worker,
        created,
        created.root,
        key="search-bounded-reply",
        content=content,
        mentions=[mentioned],
        correlation_id="bounded-payload",
        session="bounded-session",
    )
    if query is None:
        full_snippet, expected_rank = raw, 0
    else:
        parsed_query = SearchQuery(query, config="simple", search_type="websearch")
        vector = SearchVector("text_payload", "json_payload", config="simple")
        full_snippet, expected_rank = (
            Message.objects.filter(pk=reply.id)
            .annotate(
                reference_snippet=SearchHeadline(
                    Coalesce("text_payload", "json_payload", Value(""), output_field=TextField()),
                    parsed_query,
                    config="simple",
                    start_sel="[",
                    stop_sel="]",
                    min_words=8,
                    max_words=32,
                    short_word="3",
                    highlight_all=False,
                    max_fragments=2,
                    fragment_delimiter=" … ",
                ),
                reference_rank=Cast(
                    SearchRank(vector, parsed_query, cover_density=True) * Value(1_000_000.0),
                    output_field=BigIntegerField(),
                ),
            )
            .values_list("reference_snippet", "reference_rank")
            .get()
        )
        assert expected_rank > 0
    assert len(full_snippet) > 280

    page = search_messages(
        credential=coordinator,
        address=created.address,
        query=query,
        sender_id=worker.id,
        mentioned_agent_id=mentioned.id,
        correlation_id="bounded-payload",
    )

    assert [hit.message.id for hit in page.hits] == [reply.id]
    hit = page.hits[0]
    assert hit.message.get_deferred_fields() == {"text_payload", "json_payload"}
    assert "search_vector" not in hit.message.__dict__
    assert getattr(hit.message, "search_snippet", None) == full_snippet[:280]
    assert hit.snippet == full_snippet[:280]
    assert hit.rank == expected_rank
    with CaptureQueriesContext(connection) as queries:
        payload = search_hit_payload(hit)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    assert len(queries) == 0
    assert payload["mention_ids"] == [str(mentioned.id)]
    assert payload["sender"]["session"] == "bounded-session"
    assert payload["correlation_id"] == "bounded-payload"
    assert page.used_bytes == len(encoded)
    assert page.snapshot_sequence == reply.sequence
    assert page.complete is True
    assert page.next_after_rank is None
    assert page.next_after_sequence is None
    assert page.next_after_message_id is None


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


def test_search_byte_budget_stops_before_the_next_full_hit(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, _ = credential_factory(name="Budget coordinator")
    worker, _ = credential_factory(name="Budget worker")
    created = _create(coordinator, key="search-budget-create")
    content = {"type": "text", "text": "Budget boundary match."}
    first = _reply(
        worker,
        created,
        created.root,
        key="search-budget-first",
        content=content,
        session="budget-session",
    )
    second = _reply(
        worker,
        created,
        created.root,
        key="search-budget-second",
        content=content,
        session="budget-session",
    )
    first_hit = SearchHit(message=first, snippet=content["text"], rank=0)
    response_budget = len(
        json.dumps(
            search_hit_payload(first_hit),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    monkeypatch.setattr(
        "core.limits.effective_limits",
        lambda: replace(
            effective_limits(),
            maximum_search_response_bytes=response_budget,
        ),
    )

    page = search_messages(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        sender_id=worker.id,
        limit=10,
    )

    assert [hit.message.id for hit in page.hits] == [first.id]
    assert second.id not in {hit.message.id for hit in page.hits}
    assert page.used_bytes == response_budget
    assert page.complete is False
    assert page.next_after_message_id == first.id


@pytest.mark.parametrize(
    ("microsecond", "expected_wire_timestamp"),
    [
        (123456, "2026-10-18T12:34:56.123Z"),
        (0, "2026-10-18T12:34:56Z"),
    ],
)
def test_public_search_preserves_timestamp_wire_format_and_counts_response_bytes(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    microsecond: int,
    expected_wire_timestamp: str,
) -> None:
    coordinator, _ = credential_factory(name="Timestamp coordinator")
    worker, key = credential_factory(name="Timestamp worker")
    mentioned, _ = credential_factory(name="Timestamp mention")
    created = _create(coordinator, key="search-timestamp-create")
    stored_timestamp = datetime(2026, 10, 18, 12, 34, 56, microsecond, tzinfo=UTC)
    with monkeypatch.context() as timestamp_clock:
        timestamp_clock.setattr("django.utils.timezone.now", lambda: stored_timestamp)
        reply = _reply(
            worker,
            created,
            created.root,
            key="search-timestamp-reply",
            content={"type": "text", "text": "A café timestamp match."},
            mentions=[mentioned],
            correlation_id="timestamp-check",
            session="session-é",
        )

    response = client.post(
        "/api/v1/search",
        data=json.dumps(
            {
                "address": created.address,
                "cycle_id": str(created.cycle.id),
                "sender_id": str(worker.id),
                "query": None,
            }
        ),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200, response.content
    result = response.json()["results"][0]
    assert result["message_id"] == str(reply.id)
    assert result["sender"]["session"] == "session-é"
    assert result["mention_ids"] == [str(mentioned.id)]
    assert result["created_at"] == expected_wire_timestamp
    hit = SearchHit(message=reply, snippet="A café timestamp match.", rank=0)
    expected_bytes = len(
        json.dumps(
            search_hit_payload(hit),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    assert response.json()["used_bytes"] == expected_bytes
    assert expected_bytes == len(
        json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )


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
