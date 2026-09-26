"""Focused behavior tests for subtree and leaf snapshot traversal."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tunnels.errors import CycleUnavailable, InvalidRequest
from tunnels.services import create_tunnel, get_subtree, list_leaves, list_replies, post_reply

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Coordinate this work."},
    )


def _reply(credential: Any, created: Any, parent: Any, key: str) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent.id,
        content={"type": "text", "text": key},
    ).message


def _subtree_with_arguments(credential: Any, created: Any, arguments: dict[str, int]) -> Any:
    return get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=created.root.id,
        max_depth=arguments.get("max_depth", 8),
        limit=arguments.get("limit", 200),
        after_sequence=arguments.get("after_sequence", 0),
        snapshot_sequence=arguments.get("snapshot_sequence"),
    )


def _leaves_with_arguments(credential: Any, created: Any, arguments: dict[str, int]) -> Any:
    return list_leaves(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        limit=arguments.get("limit", 100),
        after_sequence=arguments.get("after_sequence", 0),
        snapshot_sequence=arguments.get("snapshot_sequence"),
    )


def _replies_with_arguments(credential: Any, created: Any, arguments: dict[str, int]) -> Any:
    return list_replies(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        cycle_id=created.cycle.id,
        after_sequence=arguments.get("after_sequence", 0),
        snapshot_sequence=arguments.get("snapshot_sequence"),
    )


def test_direct_reply_pages_keep_one_snapshot_during_later_sibling_replies(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "reply-page-create")
    first = _reply(credential, created, created.root, "reply-page-first")
    second = _reply(credential, created, created.root, "reply-page-second")
    third = _reply(credential, created, created.root, "reply-page-third")

    first_page = list_replies(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        cycle_id=created.cycle.id,
        limit=2,
    )

    assert [message.id for message in first_page.messages] == [first.id, second.id]
    assert first_page.parent.id == created.root.id
    assert first_page.complete is False
    assert first_page.next_after_sequence == second.sequence

    later = _reply(credential, created, created.root, "reply-page-later")
    second_page = list_replies(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        cycle_id=created.cycle.id,
        limit=2,
        after_sequence=first_page.next_after_sequence or 0,
        snapshot_sequence=first_page.snapshot_sequence,
    )

    assert [message.id for message in second_page.messages] == [third.id]
    assert later.id not in {message.id for message in second_page.messages}
    assert second_page.complete is True
    with pytest.raises(InvalidRequest, match="reply page position"):
        list_replies(
            credential=credential,
            address=created.address,
            message_id=created.root.id,
            cycle_id=created.cycle.id,
            after_sequence=later.sequence,
            snapshot_sequence=first_page.snapshot_sequence,
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"after_sequence": -1}, "after_sequence"),
        ({"snapshot_sequence": 0}, "snapshot sequence"),
        ({"snapshot_sequence": 2}, "snapshot sequence"),
    ],
)
def test_direct_replies_reject_invalid_cursor_bounds(
    credential_factory: Any,
    arguments: dict[str, int],
    message: str,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, f"reply-bound-{message}-{uuid4()}")

    with pytest.raises(InvalidRequest, match=message):
        _replies_with_arguments(credential, created, arguments)


def test_subtree_uses_stable_preorder_and_relative_depth(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "subtree-preorder-create")
    first = _reply(credential, created, created.root, "subtree-first-child")
    second = _reply(credential, created, created.root, "subtree-second-child")
    first_child = _reply(credential, created, first, "subtree-first-grandchild")
    first_second_child = _reply(credential, created, first, "subtree-second-grandchild")
    second_child = _reply(credential, created, second, "subtree-second-branch-child")

    page = get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=created.root.id,
        max_depth=8,
        limit=100,
    )

    assert [message.id for message in page.messages] == [
        created.root.id,
        first.id,
        first_child.id,
        first_second_child.id,
        second.id,
        second_child.id,
    ]
    assert page.root.id == created.root.id
    assert page.next_after_sequence is None
    assert page.truncated is False

    bounded = get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=created.root.id,
        max_depth=1,
        limit=100,
    )
    assert [message.id for message in bounded.messages] == [
        created.root.id,
        first.id,
        second.id,
    ]
    assert bounded.truncated is True

    relative = get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=first.id,
        max_depth=1,
        limit=100,
    )
    assert [message.id for message in relative.messages] == [
        first.id,
        first_child.id,
        first_second_child.id,
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"max_depth": -1}, "max_depth"),
        ({"max_depth": 129}, "max_depth"),
        ({"limit": 0}, "limit"),
        ({"limit": 1_001}, "limit"),
        ({"after_sequence": -1}, "after_sequence"),
        ({"snapshot_sequence": 0}, "snapshot"),
        ({"snapshot_sequence": 2}, "snapshot"),
    ],
)
def test_subtree_rejects_invalid_depth_item_and_cursor_bounds(
    credential_factory: Any,
    arguments: dict[str, int],
    message: str,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, f"subtree-bound-{message}-{uuid4()}")

    with pytest.raises(InvalidRequest, match=message):
        _subtree_with_arguments(credential, created, arguments)


def test_subtree_pages_keep_one_snapshot_during_a_concurrent_addition(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "subtree-snapshot-create")
    first = _reply(credential, created, created.root, "subtree-snapshot-first")
    second = _reply(credential, created, created.root, "subtree-snapshot-second")
    nested = _reply(credential, created, first, "subtree-snapshot-nested")

    first_page = get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=created.root.id,
        max_depth=8,
        limit=2,
    )
    assert [message.id for message in first_page.messages] == [created.root.id, first.id]
    assert first_page.next_after_sequence == first.sequence
    assert first_page.truncated is True

    later = _reply(credential, created, nested, "subtree-snapshot-later")
    second_page = get_subtree(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        message_id=created.root.id,
        max_depth=8,
        limit=2,
        after_sequence=first_page.next_after_sequence or 0,
        snapshot_sequence=first_page.snapshot_sequence,
    )

    assert [message.id for message in second_page.messages] == [nested.id, second.id]
    assert later.id not in {message.id for message in second_page.messages}
    assert second_page.next_after_sequence is None

    with pytest.raises(InvalidRequest, match="page position"):
        get_subtree(
            credential=credential,
            address=created.address,
            cycle_id=created.cycle.id,
            message_id=created.root.id,
            after_sequence=later.sequence,
            snapshot_sequence=first_page.snapshot_sequence,
        )


def test_subtree_and_leaves_reject_wrong_cycle_or_message_access(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    first = _create(credential, "traversal-scope-first")
    second = _create(credential, "traversal-scope-second")

    with pytest.raises(CycleUnavailable):
        get_subtree(
            credential=credential,
            address=first.address,
            cycle_id=first.cycle.id,
            message_id=second.root.id,
        )
    with pytest.raises(CycleUnavailable):
        get_subtree(
            credential=credential,
            address=first.address,
            cycle_id=second.cycle.id,
            message_id=first.root.id,
        )
    with pytest.raises(CycleUnavailable):
        list_leaves(
            credential=credential,
            address=first.address,
            cycle_id=second.cycle.id,
        )


def test_leaf_pages_are_sequence_ordered_and_keep_snapshot_leaf_semantics(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "leaf-snapshot-create")
    first = _reply(credential, created, created.root, "leaf-snapshot-first")
    second = _reply(credential, created, created.root, "leaf-snapshot-second")
    third = _reply(credential, created, created.root, "leaf-snapshot-third")

    first_page = list_leaves(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        limit=2,
    )
    assert [message.id for message in first_page.messages] == [first.id, second.id]
    assert first_page.next_after_sequence == second.sequence
    assert first_page.complete is False

    new_leaf = _reply(credential, created, third, "leaf-snapshot-later-child")
    second_page = list_leaves(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        limit=2,
        after_sequence=first_page.next_after_sequence or 0,
        snapshot_sequence=first_page.snapshot_sequence,
    )
    assert [message.id for message in second_page.messages] == [third.id]
    assert second_page.complete is True

    current = list_leaves(
        credential=credential,
        address=created.address,
        cycle_id=created.cycle.id,
        limit=100,
    )
    assert [message.id for message in current.messages] == [first.id, second.id, new_leaf.id]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 1_001}, "limit"),
        ({"after_sequence": -1}, "after_sequence"),
        ({"snapshot_sequence": 0}, "snapshot"),
        ({"snapshot_sequence": 2}, "snapshot"),
    ],
)
def test_leaves_reject_invalid_item_and_cursor_bounds(
    credential_factory: Any,
    arguments: dict[str, int],
    message: str,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, f"leaf-bound-{message}-{uuid4()}")

    with pytest.raises(InvalidRequest, match=message):
        _leaves_with_arguments(credential, created, arguments)
