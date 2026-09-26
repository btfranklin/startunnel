"""Search validation fails before PostgreSQL work and keeps resource bounds."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tunnels.errors import CycleUnavailable, DependencyUnavailable, InvalidRequest
from tunnels.search import _base_query, _branch_message_ids, _hit_response_bytes, search_messages
from tunnels.services import create_tunnel, post_reply

pytestmark = pytest.mark.django_db


def _create(credential: Any, *, key: str) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Search validation root."},
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"after_rank": 2}, "incomplete"),
        (
            {"after_rank": -1, "after_sequence": 1, "after_message_id": uuid4()},
            "rank",
        ),
        (
            {"after_rank": 1, "after_sequence": 0, "after_message_id": uuid4()},
            "sequence",
        ),
        ({"query": "x" * 513}, "query"),
        ({"query": "bad\u0000query"}, "query"),
        ({"query": None, "correlation_id": ""}, "correlation_id"),
        ({"query": None, "correlation_id": "x" * 129}, "correlation_id"),
        ({"query": None, "sequence_after": -1}, "sequence_after"),
        ({"query": None, "sequence_before": 0}, "sequence_before"),
        (
            {"query": None, "sequence_after": 4, "sequence_before": 4},
            "less than",
        ),
        ({"query": None}, "query text"),
    ],
)
def test_search_rejects_invalid_or_unbounded_inputs_before_database_search(
    credential_factory: Any,
    arguments: dict[str, Any],
    message: str,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, key=f"search-validation-{uuid4()}")

    with pytest.raises(InvalidRequest, match=message):
        search_messages(
            credential=credential,
            address=created.address,
            cycle_id=created.cycle.id,
            **arguments,
        )


def test_valid_search_fails_directly_without_the_required_postgresql_dependency(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, key="search-validation-dependency")

    with pytest.raises(DependencyUnavailable, match="PostgreSQL"):
        search_messages(
            credential=credential,
            address=created.address,
            cycle_id=created.cycle.id,
            query="validation",
        )


def test_branch_filter_collects_only_the_selected_descendants(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, key="search-validation-branch")
    selected = post_reply(
        credential=credential,
        idempotency_key="search-validation-selected",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Selected."},
    ).message
    descendant = post_reply(
        credential=credential,
        idempotency_key="search-validation-descendant",
        address=created.address,
        parent_id=selected.id,
        content={"type": "text", "text": "Descendant."},
    ).message
    outside = post_reply(
        credential=credential,
        idempotency_key="search-validation-outside",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Outside."},
    ).message

    ids = _branch_message_ids(
        cycle=created.cycle,
        root_id=selected.id,
        high_water=descendant.sequence,
    )

    assert ids == {selected.id, descendant.id}
    assert outside.id not in ids
    with pytest.raises(CycleUnavailable):
        _branch_message_ids(
            cycle=created.cycle,
            root_id=outside.id,
            high_water=descendant.sequence,
        )


def test_base_query_composes_metadata_branch_and_sequence_filters(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Filter coordinator")
    worker, _ = credential_factory(name="Filter worker")
    mentioned, _ = credential_factory(name="Filter mention")
    created = _create(coordinator, key="search-validation-filter-query")
    selected = post_reply(
        credential=worker,
        idempotency_key="search-validation-filter-selected",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Selected."},
        mentions=[mentioned.id],
        correlation_id="selected-work",
    ).message
    nested = post_reply(
        credential=worker,
        idempotency_key="search-validation-filter-nested",
        address=created.address,
        parent_id=selected.id,
        content={"type": "text", "text": "Nested."},
        mentions=[mentioned.id],
        correlation_id="selected-work",
    ).message
    post_reply(
        credential=coordinator,
        idempotency_key="search-validation-filter-outside",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Outside."},
        correlation_id="other-work",
    )

    filtered = _base_query(
        cycle=created.cycle,
        high_water=4,
        sender_id=worker.id,
        mentioned_agent_id=mentioned.id,
        correlation_id="selected-work",
        branch_root_id=selected.id,
        sequence_after=selected.sequence,
        sequence_before=nested.sequence + 1,
    )
    unfiltered = _base_query(
        cycle=created.cycle,
        high_water=4,
        sender_id=None,
        mentioned_agent_id=None,
        correlation_id=None,
        branch_root_id=None,
        sequence_after=None,
        sequence_before=None,
    )

    assert list(filtered.values_list("id", flat=True)) == [nested.id]
    assert set(unfiltered.values_list("id", flat=True)) == {
        created.root.id,
        selected.id,
        nested.id,
        *created.cycle.messages.filter(sequence=4).values_list("id", flat=True),
    }


def test_search_byte_count_uses_the_canonical_public_result_shape(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory(name="Byte count agent")
    created = _create(credential, key="search-validation-bytes")

    count = _hit_response_bytes(
        message=created.root,
        snippet="A safe snippet.",
        rank=42,
    )

    assert count > len(b"A safe snippet.")
    assert count < 2_048
