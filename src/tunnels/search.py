"""Bounded PostgreSQL full-text search for authorized message trees."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank, SearchVector
from django.db import connection, transaction
from django.db.models import BigIntegerField, F, Q, QuerySet, TextField, Value
from django.db.models.functions import Cast, Coalesce

from agents.models import AgentCredential

from .access import limits_for, resolve_tunnel, select_cycle
from .errors import CycleUnavailable, DependencyUnavailable, InvalidRequest
from .models import Cycle, Message, Tunnel
from .payloads import validate_storable_text

SNIPPET_MAX_CHARACTERS = 280


@dataclass(frozen=True, slots=True)
class SearchHit:
    message: Message
    snippet: str
    rank: int


@dataclass(frozen=True, slots=True)
class SearchPage:
    tunnel: Tunnel
    cycle: Cycle
    hits: tuple[SearchHit, ...]
    snapshot_sequence: int
    next_after_rank: int | None
    next_after_sequence: int | None
    next_after_message_id: UUID | None
    complete: bool
    used_bytes: int


def _branch_message_ids(*, cycle: Cycle, root_id: UUID, high_water: int) -> set[UUID]:
    rows = list(
        Message.objects.filter(cycle=cycle, sequence__lte=high_water)
        .order_by("sequence", "id")
        .values_list("id", "parent_id")
    )
    if not any(message_id == root_id for message_id, _parent_id in rows):
        raise CycleUnavailable()
    descendants = {root_id}
    for message_id, parent_id in rows:
        if parent_id in descendants:
            descendants.add(message_id)
    return descendants


def _base_query(
    *,
    cycle: Cycle,
    high_water: int,
    sender_id: UUID | None,
    mentioned_agent_id: UUID | None,
    correlation_id: str | None,
    branch_root_id: UUID | None,
    sequence_after: int | None,
    sequence_before: int | None,
) -> QuerySet[Message]:
    result = (
        Message.objects.select_related("sender", "parent")
        .prefetch_related("mentions__credential")
        .filter(cycle=cycle, sequence__lte=high_water)
    )
    if sender_id is not None:
        result = result.filter(sender_id=sender_id)
    if mentioned_agent_id is not None:
        result = result.filter(mentions__credential_id=mentioned_agent_id)
    if correlation_id is not None:
        result = result.filter(correlation_id=correlation_id)
    if branch_root_id is not None:
        result = result.filter(
            id__in=_branch_message_ids(cycle=cycle, root_id=branch_root_id, high_water=high_water)
        )
    if sequence_after is not None:
        result = result.filter(sequence__gt=sequence_after)
    if sequence_before is not None:
        result = result.filter(sequence__lt=sequence_before)
    return result.distinct()


def _validated_search_values(
    *,
    query: str | None,
    correlation_id: str | None,
    sequence_after: int | None,
    sequence_before: int | None,
    maximum_query_characters: int,
) -> tuple[str, str | None]:
    query_text = (query or "").strip()
    validate_storable_text(query_text, field="query")
    if len(query_text) > maximum_query_characters:
        raise InvalidRequest(f"query can contain at most {maximum_query_characters} characters.")
    correlation = correlation_id.strip() if correlation_id is not None else None
    if correlation is not None:
        validate_storable_text(correlation, field="correlation_id")
        if not correlation or len(correlation) > 128:
            raise InvalidRequest("correlation_id must contain from 1 through 128 characters.")
    if sequence_after is not None and sequence_after < 0:
        raise InvalidRequest("sequence_after cannot be negative.")
    if sequence_before is not None and sequence_before < 1:
        raise InvalidRequest("sequence_before must be positive.")
    if (
        sequence_after is not None
        and sequence_before is not None
        and sequence_after >= sequence_before
    ):
        raise InvalidRequest("sequence_after must be less than sequence_before.")
    return query_text, correlation


def _hit_response_bytes(*, message: Message, snippet: str, rank: int) -> int:
    public_item = {
        "message_id": str(message.id),
        "cycle_id": str(message.cycle_id),
        "parent_id": str(message.parent_id) if message.parent_id else None,
        "sender": {"id": str(message.sender_id), "name": message.sender_name},
        "mention_ids": sorted(str(item.credential_id) for item in message.mentions.all()),
        "correlation_id": message.correlation_id or None,
        "sequence": message.sequence,
        "depth": message.depth,
        "created_at": message.created_at.isoformat(),
        "rank": rank,
        "snippet": snippet,
    }
    canonical = json.dumps(public_item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(canonical.encode("utf-8"))


def search_messages(
    *,
    credential: AgentCredential,
    address: str,
    query: str | None = None,
    cycle_id: UUID | None = None,
    sender_id: UUID | None = None,
    mentioned_agent_id: UUID | None = None,
    correlation_id: str | None = None,
    branch_root_id: UUID | None = None,
    sequence_after: int | None = None,
    sequence_before: int | None = None,
    snapshot_sequence: int | None = None,
    after_rank: int | None = None,
    after_sequence: int | None = None,
    after_message_id: UUID | None = None,
    limit: int = 50,
) -> SearchPage:
    """Search one authorized cycle with fixed snapshot and resource bounds."""

    limits = limits_for(credential)
    if limit < 1 or limit > limits.maximum_search_results:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_search_results}.")
    missing_cursor_values = (after_rank is None, after_sequence is None, after_message_id is None)
    if missing_cursor_values.count(True) not in {0, 3}:
        raise InvalidRequest("The search page position is incomplete.")
    if after_rank is not None and after_rank < 0:
        raise InvalidRequest("The search page rank is not valid.")
    if after_sequence is not None and after_sequence < 1:
        raise InvalidRequest("The search page sequence is not valid.")
    query_text, correlation = _validated_search_values(
        query=query,
        correlation_id=correlation_id,
        sequence_after=sequence_after,
        sequence_before=sequence_before,
        maximum_query_characters=limits.maximum_search_query_characters,
    )
    if not query_text and all(
        value is None
        for value in (
            sender_id,
            mentioned_agent_id,
            correlation,
            branch_root_id,
            sequence_after,
            sequence_before,
        )
    ):
        raise InvalidRequest("Provide query text or at least one metadata filter.")
    if connection.vendor != "postgresql":
        raise DependencyUnavailable("PostgreSQL search is not available.")

    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    current_sequence = max(cycle.next_sequence - 1, 0)
    high_water = current_sequence if snapshot_sequence is None else snapshot_sequence
    if high_water < 1 or high_water > current_sequence:
        raise InvalidRequest("The search snapshot is not valid for this cycle.")

    with transaction.atomic(), connection.cursor() as database_cursor:
        database_cursor.execute(
            "SET LOCAL statement_timeout = %s",
            [limits.search_statement_timeout_milliseconds],
        )
        rows = _base_query(
            cycle=cycle,
            high_water=high_water,
            sender_id=sender_id,
            mentioned_agent_id=mentioned_agent_id,
            correlation_id=correlation,
            branch_root_id=branch_root_id,
            sequence_after=sequence_after,
            sequence_before=sequence_before,
        )
        searchable_content = Coalesce(
            "text_payload",
            "json_payload",
            Value(""),
            output_field=TextField(),
        )
        if query_text:
            parsed_query = SearchQuery(query_text, config="simple", search_type="websearch")
            vector = SearchVector("text_payload", "json_payload", config="simple")
            rows = rows.annotate(
                search_vector=vector,
                search_rank=Cast(
                    SearchRank(vector, parsed_query, cover_density=True) * Value(1_000_000.0),
                    output_field=BigIntegerField(),
                ),
                search_snippet=SearchHeadline(
                    searchable_content,
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
            ).filter(search_vector=parsed_query)
        else:
            rows = rows.annotate(
                search_rank=Value(0, output_field=BigIntegerField()),
                search_snippet=searchable_content,
            )
        if after_rank is not None and after_sequence is not None and after_message_id is not None:
            rows = rows.filter(
                Q(search_rank__lt=after_rank)
                | Q(search_rank=after_rank, sequence__gt=after_sequence)
                | Q(
                    search_rank=after_rank,
                    sequence=after_sequence,
                    id__gt=after_message_id,
                )
            )
        selected = list(rows.order_by(F("search_rank").desc(), "sequence", "id")[: limit + 1])

    has_more = len(selected) > limit
    hits: list[SearchHit] = []
    used_bytes = 0
    byte_truncated = False
    for message in selected[:limit]:
        snippet = str(message.search_snippet)[:SNIPPET_MAX_CHARACTERS]  # type: ignore[attr-defined]
        rank = int(message.search_rank)  # type: ignore[attr-defined]
        hit_bytes = _hit_response_bytes(message=message, snippet=snippet, rank=rank)
        if hits and used_bytes + hit_bytes > limits.maximum_search_response_bytes:
            byte_truncated = True
            break
        hits.append(
            SearchHit(
                message=message,
                snippet=snippet,
                rank=rank,
            )
        )
        used_bytes += hit_bytes

    confirmed = resolve_tunnel(credential=credential, address=address)
    if confirmed.id != tunnel.id:
        raise InvalidRequest("The tunnel changed during the search.")
    page_has_more = has_more or byte_truncated
    last = hits[-1] if page_has_more and hits else None
    return SearchPage(
        tunnel=tunnel,
        cycle=cycle,
        hits=tuple(hits),
        snapshot_sequence=high_water,
        next_after_rank=last.rank if last else None,
        next_after_sequence=last.message.sequence if last else None,
        next_after_message_id=last.message.id if last else None,
        complete=not page_has_more,
        used_bytes=used_bytes,
    )
