"""A session label is immutable message data with no authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tunnels.activity import read_activity_once
from tunnels.codec import canonical_address
from tunnels.context import context_item_payload, get_context
from tunnels.entry import enter_tunnel, entry_payload
from tunnels.errors import IdempotencyConflict, InvalidRequest
from tunnels.message_data import message_payload
from tunnels.participants import list_participants
from tunnels.services import (
    close_cycle,
    create_tunnel,
    post_reply,
    read_tree,
    rollover_cycle,
    start_cycle,
)

pytestmark = pytest.mark.django_db


def _request_digest(data: dict[str, Any]) -> bytes:
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(canonical).digest()


def test_labels_are_stored_and_shared_reads_keep_sender_identity(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory(name="Shared agent")
    created = create_tunnel(
        credential=credential,
        idempotency_key="session-labeled-root",
        root_content={"type": "text", "text": "Root"},
        root_session="chat-a",
    )
    posted = post_reply(
        credential=credential,
        idempotency_key="session-labeled-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Reply"},
        session="chat-b",
    )

    assert created.root.sender_name == posted.message.sender_name == "Shared agent"
    assert message_payload(created.root)["sender"]["session"] == "chat-a"
    assert message_payload(posted.message)["sender"]["session"] == "chat-b"
    tree = read_tree(credential=credential, address=created.address)
    assert [message_payload(item)["sender"]["session"] for item in tree.messages] == [
        "chat-a",
        "chat-b",
    ]
    context = get_context(
        credential=credential,
        address=created.address,
        focus_message_id=posted.message.id,
    )
    assert [
        context_item_payload(item.message, item.reason)["message"]["sender"]["session"]
        for item in context.items
    ] == ["chat-a", "chat-b"]
    activity = read_activity_once(credential=credential, address=created.address)
    assert [
        message_payload(item.message)["sender"]["session"]
        for item in activity.events
        if item.message is not None
    ] == ["chat-a", "chat-b"]
    entry = entry_payload(enter_tunnel(credential=credential, address=created.address))
    assert entry["root"]["sender"]["session"] == "chat-a"
    assert entry["recent_messages"][0]["sender"]["session"] == "chat-b"
    participants = list_participants(credential=credential, address=created.address)
    assert len(participants.participants) == 1
    assert participants.participants[0].message_count == 2


def test_exact_retries_include_labels_and_match_absent_session_request_data(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    common = {
        "credential": credential,
        "idempotency_key": "session-create-retry",
        "root_content": {"type": "text", "text": "Root"},
    }
    created = create_tunnel(**common)
    unlabeled_create = {
        "label": "",
        "cycle_label": "",
        "expires_in_seconds": 3600,
        "root_content": common["root_content"],
        "root_mentions": [],
        "root_correlation_id": "",
    }
    stored_create = credential.idempotency_records.get(operation="create_tunnel")
    assert bytes(stored_create.request_digest) == _request_digest(unlabeled_create)
    assert create_tunnel(**common, root_session=None).replay
    assert created.root.session is None
    with pytest.raises(IdempotencyConflict):
        create_tunnel(**common, root_session="chat-a")

    reply = {
        "credential": credential,
        "idempotency_key": "session-reply-retry",
        "address": created.address,
        "parent_id": created.root.id,
        "content": {"type": "text", "text": "Reply"},
        "session": "chat-a",
    }
    first = post_reply(**reply)
    unlabeled_reply = {
        "address": canonical_address(created.address),
        "parent_id": str(created.root.id),
        "content": reply["content"],
        "mentions": [],
        "correlation_id": "",
    }
    stored_reply = credential.idempotency_records.get(operation="post_reply")
    assert bytes(stored_reply.request_digest) != _request_digest(unlabeled_reply)
    assert post_reply(**reply).replay
    assert first.message.session == "chat-a"
    with pytest.raises(IdempotencyConflict):
        post_reply(**{**reply, "session": "chat-b"})
    unlabeled_result = post_reply(
        **{**reply, "idempotency_key": "session-reply-unlabeled", "session": None}
    )
    stored_unlabeled_reply = credential.idempotency_records.get(
        resource_id=str(unlabeled_result.message.id)
    )
    assert bytes(stored_unlabeled_reply.request_digest) == _request_digest(unlabeled_reply)
    assert post_reply(
        **{key: value for key, value in reply.items() if key not in {"session", "idempotency_key"}},
        idempotency_key="session-reply-unlabeled",
    ).replay


def test_start_and_rollover_store_root_session_and_check_retries(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="session-cycle-base",
        root_content={"type": "text", "text": "Base"},
    )
    close_cycle(
        credential=credential,
        idempotency_key="session-cycle-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    common = {
        "credential": credential,
        "idempotency_key": "session-cycle-start",
        "address": created.address,
        "expected_address_generation": 1,
        "root_content": {"type": "text", "text": "Start"},
        "root_session": "chat-a",
    }
    started = start_cycle(**common)
    assert started.root.session == "chat-a"
    assert start_cycle(**common).replay
    with pytest.raises(IdempotencyConflict):
        start_cycle(**{**common, "root_session": "chat-b"})

    rollover = {
        **common,
        "idempotency_key": "session-cycle-rollover",
        "expected_cycle_id": started.cycle.id,
        "root_content": {"type": "text", "text": "Rollover"},
        "root_session": "chat-b",
    }
    next_cycle = rollover_cycle(**rollover)
    assert next_cycle.root.session == "chat-b"
    assert rollover_cycle(**rollover).replay
    with pytest.raises(IdempotencyConflict):
        rollover_cycle(**{**rollover, "root_session": "chat-a"})


def test_unlabeled_cycle_retries_match_absent_session_request_data(
    credential_factory: Any,
) -> None:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="session-old-cycle-base",
        root_content={"type": "text", "text": "Base"},
    )
    close_cycle(
        credential=credential,
        idempotency_key="session-old-cycle-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    common = {
        "credential": credential,
        "idempotency_key": "session-old-cycle-start",
        "address": created.address,
        "expected_address_generation": 1,
        "root_content": {"type": "text", "text": "Start"},
    }
    started = start_cycle(**common)
    unlabeled_data = {
        "address": canonical_address(created.address),
        "expected_address_generation": 1,
        "expected_cycle_id": None,
        "cycle_label": "",
        "expires_in_seconds": 3600,
        "root_content": common["root_content"],
        "root_mentions": [],
        "root_correlation_id": "",
    }
    record = credential.idempotency_records.get(operation="start_cycle")
    assert bytes(record.request_digest) == _request_digest(unlabeled_data)
    assert start_cycle(**common, root_session=None).replay
    with pytest.raises(IdempotencyConflict):
        start_cycle(**common, root_session="chat-a")

    rollover = {
        **common,
        "idempotency_key": "session-old-cycle-rollover",
        "expected_cycle_id": started.cycle.id,
        "root_content": {"type": "text", "text": "Rollover"},
    }
    rolled = rollover_cycle(**rollover)
    unlabeled_data["expected_cycle_id"] = str(started.cycle.id)
    unlabeled_data["root_content"] = rollover["root_content"]
    record = credential.idempotency_records.get(operation="rollover_cycle")
    assert bytes(record.request_digest) == _request_digest(unlabeled_data)
    assert rolled.root.session is None
    assert rollover_cycle(**rollover, root_session=None).replay
    with pytest.raises(IdempotencyConflict):
        rollover_cycle(**rollover, root_session="chat-a")


@pytest.mark.parametrize(
    "label", ["", " abc", "abc ", "a\n", "a\t", "a\x00", "a\u0085", "a\u202e", "a" * 33]
)
def test_invalid_sessions_are_rejected(credential_factory: Any, label: str) -> None:
    credential, _ = credential_factory()
    with pytest.raises(InvalidRequest, match="session"):
        create_tunnel(
            credential=credential,
            idempotency_key="session-invalid-root",
            root_content={"type": "text", "text": "Root"},
            root_session=label,
        )
