"""Context assembly is deterministic and obeys exact wire budgets."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tunnels.activity import commit_checkpoint
from tunnels.context import context_item_payload, get_context
from tunnels.errors import ContextBudgetTooSmall, CycleUnavailable
from tunnels.models import ActivityCheckpoint
from tunnels.services import create_tunnel, post_reply

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str = "context-create-0001") -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Review the release."},
    )


def _reply(created: Any, credential: Any, key: str, text: str, parent_id: Any = None) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent_id or created.root.id,
        content={"type": "text", "text": text},
    ).message


def _canonical_size(item: Any) -> int:
    return len(
        json.dumps(
            context_item_payload(item.message, item.reason),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def test_context_reports_exact_canonical_utf8_bytes(credential_factory: Any) -> None:
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


def test_context_requires_the_complete_root_to_focus_branch(credential_factory: Any) -> None:
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


def test_context_snapshot_excludes_later_replies(credential_factory: Any) -> None:
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
    credential_factory: Any,
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


def test_context_uses_deterministic_greedy_truncation(credential_factory: Any) -> None:
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


def test_context_rejects_wrong_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Creator")
    other, _ = credential_factory(name="Other")
    first = _create(creator)
    second = _create(other, "context-other-create")
    with pytest.raises(CycleUnavailable):
        get_context(
            credential=creator,
            address=first.address,
            focus_message_id=first.root.id,
            cycle_id=second.cycle.id,
        )


def test_context_does_not_advance_the_saved_checkpoint(credential_factory: Any) -> None:
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
