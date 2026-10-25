"""Context assembly is deterministic and obeys exact wire budgets."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import Settings
from tests.factories import CredentialFactory

from agents.models import AgentCredential
from core.errors import ContextBudgetTooSmall, CycleUnavailable
from tunnels.activity import commit_checkpoint
from tunnels.context import ContextItem, context_item_payload, get_context
from tunnels.cycles import CreatedTunnel, close_cycle, create_tunnel
from tunnels.messages import get_branch, get_message, load_branch, post_reply, read_tree
from tunnels.models import ActivityCheckpoint, Message
from tunnels.retention import delete_due_cycle_content

pytestmark = pytest.mark.django_db


def _create(credential: AgentCredential, key: str = "context-create-0001") -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Review the release."},
    )


def _reply(
    created: CreatedTunnel,
    credential: AgentCredential,
    key: str,
    text: str,
    parent_id: UUID | None = None,
) -> Message:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent_id or created.root.id,
        content={"type": "text", "text": text},
    ).message


def _canonical_size(item: ContextItem) -> int:
    return len(
        json.dumps(
            context_item_payload(item.message, item.reason),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def test_context_reports_exact_canonical_utf8_bytes(credential_factory: CredentialFactory) -> None:
    creator, _ = credential_factory(name="Créator")
    created = _create(creator)
    _reply(created, creator, "context-unicode-reply", "Copper 🜣 and café")

    view = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        include_recent_activity=False,
    )
    assert view.used_bytes == sum(_canonical_size(item) for item in view.items)
    assert view.used_items == len(view.items) == 2


def test_root_context_stays_within_its_query_budget(credential_factory: CredentialFactory) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    with CaptureQueriesContext(connection) as queries:
        view = get_context(
            credential=creator,
            address=created.address,
            focus_message_id=created.root.id,
            include_direct_replies=False,
            include_recent_activity=False,
        )
    assert [item.message.id for item in view.items] == [created.root.id]
    assert len(queries) <= 7


def test_context_requires_the_complete_root_to_focus_branch(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory(name="Creator")
    created = _create(creator)
    child = _reply(created, creator, "context-child", "Child")
    leaf = _reply(created, creator, "context-leaf", "Leaf", child.id)

    with pytest.raises(ContextBudgetTooSmall):
        get_context(
            credential=creator,
            address=created.address,
            focus_message_id=leaf.id,
            max_items=2,
        )


def test_context_snapshot_excludes_later_replies(credential_factory: CredentialFactory) -> None:
    creator, _ = credential_factory(name="Creator")
    created = _create(creator)
    first = _reply(created, creator, "context-before", "Before snapshot")
    snapshot = first.sequence
    _reply(created, creator, "context-after", "After snapshot")

    view = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        snapshot_sequence=snapshot,
        include_recent_activity=False,
    )
    assert [item.message.id for item in view.items] == [created.root.id, first.id]


def test_explicit_references_keep_request_order_and_deduplicate_other_sources(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory(name="Creator")
    created = _create(creator)
    first = _reply(created, creator, "context-ref-first", "First")
    second = _reply(created, creator, "context-ref-second", "Second")

    ordered = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        include_direct_replies=False,
        include_recent_activity=False,
        referenced_message_ids=(second.id, first.id),
    )
    assert [(item.reason, item.message.id) for item in ordered.items] == [
        ("focus", created.root.id),
        ("reference", second.id),
        ("reference", first.id),
    ]

    deduplicated = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        include_direct_replies=True,
        include_recent_activity=True,
        referenced_message_ids=(first.id,),
    )
    ids = [item.message.id for item in deduplicated.items]
    assert ids == [created.root.id, first.id, second.id]


def test_context_uses_deterministic_greedy_truncation(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory(name="Creator")
    created = _create(creator)
    first = _reply(created, creator, "context-greedy-first", "Small")
    _reply(created, creator, "context-greedy-second", "This second reply does not fit.")
    full = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        include_recent_activity=False,
    )
    budget = full.items[0].wire_bytes + full.items[1].wire_bytes

    limited = get_context(
        credential=creator,
        address=created.address,
        focus_message_id=created.root.id,
        include_recent_activity=False,
        max_bytes=budget,
    )
    assert [item.message.id for item in limited.items] == [created.root.id, first.id]
    assert limited.used_bytes == budget
    assert limited.truncated is True


@pytest.mark.parametrize("foreign_cycle", [True, False])
def test_context_rejects_wrong_cycle(
    credential_factory: CredentialFactory, foreign_cycle: bool
) -> None:
    creator, _ = credential_factory(name="Creator")
    other, _ = credential_factory(name="Other")
    first = _create(creator)
    second = _create(other, "context-other-create")
    with pytest.raises(CycleUnavailable):
        get_context(
            credential=creator,
            address=first.address,
            focus_message_id=first.root.id if foreign_cycle else second.root.id,
            cycle_id=second.cycle.id if foreign_cycle else first.cycle.id,
        )


def test_context_does_not_advance_the_saved_checkpoint(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory(name="Creator")
    worker, _ = credential_factory(name="Worker")
    created = _create(creator)
    checkpoint, _ = commit_checkpoint(
        credential=worker,
        address=created.address,
        position=2,
    )

    get_context(
        credential=worker,
        address=created.address,
        focus_message_id=created.root.id,
    )

    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == 2
    assert ActivityCheckpoint.objects.count() == 1


@pytest.mark.parametrize("depth", [1, 32])
def test_branch_and_context_query_counts_do_not_grow_with_depth(
    credential_factory: CredentialFactory, depth: int
) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    expected_ids = [created.root.id]
    focus = created.root
    for index in range(depth):
        focus = _reply(created, creator, f"context-depth-{index}", "Reply.", focus.id)
        expected_ids.append(focus.id)

    with CaptureQueriesContext(connection) as branch_queries:
        branch = get_branch(credential=creator, address=created.address, message_id=focus.id)
        assert [message.id for message in branch] == expected_ids
        for message in branch:
            context_item_payload(message, "ancestor")
    assert len(branch_queries) <= 7

    with CaptureQueriesContext(connection) as context_queries:
        context = get_context(
            credential=creator,
            address=created.address,
            focus_message_id=focus.id,
            include_direct_replies=False,
            include_recent_activity=False,
        )
    assert [item.message.id for item in context.items] == expected_ids
    assert len(context_queries) <= 10


def test_tree_counts_match_current_and_older_snapshots(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    first = _reply(created, creator, "tree-count-first", "Café 🜣.")
    second = _reply(created, creator, "tree-count-second", "Later.")

    with CaptureQueriesContext(connection) as queries:
        current = read_tree(credential=creator, address=created.address, limit=1)
    assert current.message_count == 3
    assert current.content_bytes == created.root.byte_count + first.byte_count + second.byte_count
    assert len(queries) <= 5

    snapshot = read_tree(
        credential=creator,
        address=created.address,
        snapshot_sequence=first.sequence,
        after_sequence=created.root.sequence,
    )
    assert snapshot.message_count == 2
    assert snapshot.content_bytes == created.root.byte_count + first.byte_count
    assert [message.id for message in snapshot.messages] == [first.id]


def test_branch_reports_unavailable_when_retention_removes_its_focus(
    credential_factory: CredentialFactory, settings: Settings
) -> None:
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 60
    creator, _ = credential_factory()
    created = _create(creator)
    focus = get_message(credential=creator, address=created.address, message_id=created.root.id)
    close_cycle(
        credential=creator,
        address=created.address,
        expected_cycle_id=created.cycle.id,
        idempotency_key="context-retained-close",
    )
    created.cycle.refresh_from_db()
    assert created.cycle.delete_after is not None
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=created.cycle.delete_after)

    with pytest.raises(CycleUnavailable):
        load_branch(focus)
