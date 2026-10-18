"""Traversal cursors keep request bindings and public failure codes."""

from __future__ import annotations

from uuid import uuid4

import pytest

from api.router import _page_cursor_values
from api.schemas import LeavesRequest, RepliesRequest, SubtreeRequest, TreeRequest
from core.cursors import encode_cursor
from tunnels.errors import InvalidCursor, InvalidRequest

PageRequest = TreeRequest | RepliesRequest | SubtreeRequest | LeavesRequest
PAGE_KINDS = [
    (TreeRequest, "tree"),
    (RepliesRequest, "replies"),
    (SubtreeRequest, "subtree"),
    (LeavesRequest, "leaves"),
]


def _request(request_type: type[PageRequest], **values: object) -> PageRequest:
    fields = request_type.model_fields
    return request_type.model_validate(
        {
            "address": "test-address",
            **{key: value for key, value in values.items() if key in fields},
        }
    )


@pytest.mark.parametrize(("request_type", "kind"), PAGE_KINDS)
def test_page_cursor_restores_one_fixed_snapshot_and_sequence(
    request_type: type[PageRequest], kind: str
) -> None:
    tunnel_id, cycle_id, message_id = uuid4(), uuid4(), uuid4()
    binding = {
        "tunnel_id": str(tunnel_id),
        "cycle_id": str(cycle_id),
        "parent_id": str(message_id),
        "message_id": str(message_id),
        "max_depth": 3,
        "snapshot_sequence": 7,
    }
    request = _request(
        request_type,
        message_id=message_id,
        max_depth=3,
        snapshot_cursor=encode_cursor(f"{kind}-snapshot", **binding),
        page_cursor=encode_cursor(f"{kind}-page", **binding, after_sequence=4),
    )

    result = _page_cursor_values(request)

    assert result.cycle_id == cycle_id
    assert result.expected_tunnel_id == tunnel_id
    assert result.snapshot_sequence == 7
    assert result.after_sequence == 4


@pytest.mark.parametrize(("request_type", "kind"), PAGE_KINDS)
@pytest.mark.parametrize("stage", ["snapshot", "page"])
def test_page_cursor_rejects_a_different_explicit_cycle(
    request_type: type[PageRequest], kind: str, stage: str
) -> None:
    message_id = uuid4()
    cursor = encode_cursor(
        f"{kind}-{stage}",
        tunnel_id=str(uuid4()),
        cycle_id=str(uuid4()),
        parent_id=str(message_id),
        message_id=str(message_id),
        max_depth=8,
        snapshot_sequence=5,
        after_sequence=1,
    )
    request = _request(
        request_type,
        cycle_id=uuid4(),
        message_id=message_id,
        **{f"{stage}_cursor": cursor},
    )

    with pytest.raises(
        InvalidRequest, match=f"The cycle and {stage} cursor do not match."
    ) as error:
        _page_cursor_values(request)

    assert error.value.code == "invalid_request"
    assert error.value.status == 400


@pytest.mark.parametrize(("request_type", "kind"), PAGE_KINDS)
@pytest.mark.parametrize("changed_field", ["snapshot_sequence", "tunnel_id"])
def test_page_and_snapshot_cursors_must_share_snapshot_and_tunnel(
    request_type: type[PageRequest], kind: str, changed_field: str
) -> None:
    message_id = uuid4()
    binding: dict[str, object] = {
        "tunnel_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "parent_id": str(message_id),
        "message_id": str(message_id),
        "max_depth": 8,
        "snapshot_sequence": 5,
    }
    changed = {changed_field: 6 if changed_field == "snapshot_sequence" else str(uuid4())}
    request = _request(
        request_type,
        message_id=message_id,
        snapshot_cursor=encode_cursor(f"{kind}-snapshot", **binding),
        page_cursor=encode_cursor(f"{kind}-page", **{**binding, **changed}, after_sequence=2),
    )
    expected_error = InvalidRequest if changed_field == "snapshot_sequence" else InvalidCursor

    with pytest.raises(expected_error) as error:
        _page_cursor_values(request)

    assert error.value.status == 400
    assert error.value.code == (
        "invalid_request" if changed_field == "snapshot_sequence" else "invalid_cursor"
    )


@pytest.mark.parametrize("stage", ["snapshot", "page"])
@pytest.mark.parametrize("changed_field", ["parent_id", "message_id", "max_depth"])
def test_page_cursor_keeps_parent_and_subtree_request_bindings(
    stage: str, changed_field: str
) -> None:
    message_id = uuid4()
    is_reply = changed_field == "parent_id"
    kind = "replies" if is_reply else "subtree"
    binding: dict[str, object] = {
        "tunnel_id": str(uuid4()),
        "cycle_id": str(uuid4()),
        "parent_id": str(message_id),
        "message_id": str(message_id),
        "max_depth": 8,
        "snapshot_sequence": 5,
        "after_sequence": 2,
    }
    binding[changed_field] = 9 if changed_field == "max_depth" else str(uuid4())
    request = _request(
        RepliesRequest if is_reply else SubtreeRequest,
        message_id=message_id,
        **{f"{stage}_cursor": encode_cursor(f"{kind}-{stage}", **binding)},
    )
    subject = "parent" if is_reply else "subtree request"

    with pytest.raises(
        InvalidRequest, match=f"The {subject} and {stage} cursor do not match."
    ) as error:
        _page_cursor_values(request)

    assert error.value.code == "invalid_request"
    assert error.value.status == 400
