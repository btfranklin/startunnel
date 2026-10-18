"""PostgreSQL subtree pages preserve deep preorder and continuation bounds."""

from __future__ import annotations

from typing import Any

import pytest

from core.limits import Limits
from tunnels.errors import InvalidRequest
from tunnels.services import create_tunnel, get_subtree, post_reply

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def branched_tree(credential_factory: Any) -> tuple[Any, Any, list[Any]]:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="pg-deep-tree-create",
        root_content={"type": "text", "text": "Root."},
    )
    messages = [created.root]
    for parent_index in [0, 0, 1, 2, 3]:
        reply = post_reply(
            credential=credential,
            idempotency_key=f"pg-deep-tree-reply-{len(messages)}",
            address=created.address,
            parent_id=messages[parent_index].id,
            content={"type": "text", "text": f"Message {len(messages) + 1}."},
        )
        messages.append(reply.message)
    return credential, created, messages


def test_deep_preorder_pages_keep_one_snapshot_and_bounded_queries(
    branched_tree: tuple[Any, Any, list[Any]], django_assert_num_queries: Any
) -> None:
    credential, created, messages = branched_tree
    after = 0
    snapshot = None
    sequences: list[int] = []
    while True:
        with django_assert_num_queries(8, exact=False):
            page = get_subtree(
                credential=credential,
                address=created.address,
                message_id=created.root.id,
                limit=2,
                after_sequence=after,
                snapshot_sequence=snapshot,
            )
        sequences.extend(message.sequence for message in page.messages)
        if snapshot is None:
            snapshot = page.snapshot_sequence
            post_reply(
                credential=credential,
                idempotency_key="pg-deep-tree-after-snapshot",
                address=created.address,
                parent_id=messages[-1].id,
                content={"type": "text", "text": "Outside the selected snapshot."},
            )
        assert page.snapshot_sequence == 6
        if page.next_after_sequence is None:
            assert not page.truncated
            break
        assert page.truncated
        after = page.next_after_sequence
    assert sequences == [1, 2, 4, 6, 3, 5]
    terminal = get_subtree(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        after_sequence=5,
        snapshot_sequence=6,
    )
    assert terminal.messages == ()
    assert terminal.next_after_sequence is None and not terminal.truncated


@pytest.mark.parametrize(
    ("depth", "sequences", "truncated"),
    [
        (0, [1], True),
        (1, [1, 2, 3], True),
        (Limits().maximum_tree_depth, [1, 2, 4, 6, 3, 5], False),
    ],
)
def test_subtree_depth_boundaries(
    branched_tree: tuple[Any, Any, list[Any]], depth: int, sequences: list[int], truncated: bool
) -> None:
    credential, created, _ = branched_tree
    page = get_subtree(
        credential=credential,
        address=created.address,
        message_id=created.root.id,
        max_depth=depth,
    )
    assert [message.sequence for message in page.messages] == sequences
    assert page.truncated is truncated
    assert page.next_after_sequence is None


@pytest.mark.parametrize(("after", "depth"), [(3, 8), (6, 1)])
def test_subtree_continuation_must_belong_to_selected_descendants(
    branched_tree: tuple[Any, Any, list[Any]], after: int, depth: int
) -> None:
    credential, created, messages = branched_tree
    with pytest.raises(InvalidRequest):
        get_subtree(
            credential=credential,
            address=created.address,
            message_id=messages[1].id,
            max_depth=depth,
            after_sequence=after,
        )
