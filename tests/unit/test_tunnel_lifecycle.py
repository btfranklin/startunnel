"""High-value domain tests for stable tunnels and immutable cycle trees."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError
from django.utils import timezone

from tunnels.errors import CycleClosed, IdempotencyConflict, InvalidRequest, TunnelUnavailable
from tunnels.models import Cycle, Message, Tunnel, TunnelAddress, TunnelEvent
from tunnels.services import close_cycle, create_tunnel, post_reply, read_tree

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str = "create-tree-0001", **kwargs: Any) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        label="Release coordination",
        cycle_label="Initial review",
        root_content={"type": "text", "text": "Review release 42."},
        **kwargs,
    )


def test_create_is_atomic_and_builds_one_rooted_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Coordinator")

    created = _create(creator)

    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert created.tunnel.state == Tunnel.State.ACTIVE
    assert created.tunnel.next_cycle_number == 2
    assert created.tunnel.next_event_position == 3
    assert created.cycle.number == 1
    assert created.cycle.root_message_id == created.root.id
    assert created.cycle.next_sequence == 2
    assert created.cycle.message_count == 1
    assert created.root.parent_id is None
    assert created.root.sequence == 1
    assert created.root.depth == 0
    assert created.root.sender_name == "Coordinator"
    assert created.tunnel.addresses.get().state == TunnelAddress.State.CURRENT
    assert list(created.tunnel.events.values_list("event_type", "position")) == [
        (TunnelEvent.Type.CYCLE_STARTED, 1),
        (TunnelEvent.Type.MESSAGE_POSTED, 2),
    ]


def test_create_replay_returns_same_tree_and_never_stores_address(credential_factory: Any) -> None:
    creator, _ = credential_factory()

    first = _create(creator, key="create-replay-0001")
    replay = _create(creator, key="create-replay-0001")

    assert replay.replay is True
    assert replay.tunnel.id == first.tunnel.id
    assert replay.cycle.id == first.cycle.id
    assert replay.root.id == first.root.id
    assert replay.address == first.address
    record = creator.idempotency_records.get()
    assert first.address not in str(record.response)
    assert len(bytes(record.derivation_nonce)) == 32

    with pytest.raises(IdempotencyConflict):
        create_tunnel(
            credential=creator,
            idempotency_key="create-replay-0001",
            label="Changed",
            cycle_label="Initial review",
            root_content={"type": "text", "text": "Review release 42."},
        )


def test_create_replay_rejects_an_invalid_historical_response(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    _create(creator, key="create-invalid-response")
    record = creator.idempotency_records.get()
    record.response = {**record.response, "message_count": False}
    record.save(update_fields=["response"])

    with pytest.raises(TunnelUnavailable):
        _create(creator, key="create-invalid-response")


def test_failed_root_creation_rolls_back_every_resource(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, _ = credential_factory()

    def fail_store(message: Message, mentions: list[Any]) -> None:
        del message, mentions
        raise RuntimeError("injected root failure")

    monkeypatch.setattr("tunnels.services._store_mentions", fail_store)
    with pytest.raises(RuntimeError, match="injected root failure"):
        _create(creator, key="create-rollback-0001")

    assert not Tunnel.objects.exists()
    assert not TunnelAddress.objects.exists()
    assert not Cycle.objects.exists()
    assert not Message.objects.exists()
    assert not TunnelEvent.objects.exists()
    assert not creator.idempotency_records.exists()


def test_replies_form_an_immutable_ordered_tree_and_allow_self_reply(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Coordinator")
    worker, _ = credential_factory(name="Research worker")
    created = _create(creator)

    first = post_reply(
        credential=worker,
        idempotency_key="reply-tree-0001",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "json", "value": {"finding": "Restore role is incomplete."}},
        correlation_id="release-42",
    )
    nested = post_reply(
        credential=worker,
        idempotency_key="reply-tree-0002",
        address=created.address,
        parent_id=first.message.id,
        content={"type": "text", "text": "I confirmed the missing grant."},
    )

    assert (first.message.sequence, first.message.depth) == (2, 1)
    assert (nested.message.sequence, nested.message.depth) == (3, 2)
    assert nested.message.parent_id == first.message.id
    created.cycle.refresh_from_db()
    created.tunnel.refresh_from_db()
    assert created.cycle.message_count == 3
    assert created.cycle.next_sequence == 4
    assert created.tunnel.next_event_position == 5
    assert Message.objects.get(pk=first.message.id).content_digest


def test_parent_must_belong_to_the_active_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    first = _create(creator, key="parent-first-create")
    second = _create(creator, key="parent-second-create")

    with pytest.raises(InvalidRequest, match="parent message"):
        post_reply(
            credential=creator,
            idempotency_key="cross-cycle-parent",
            address=second.address,
            parent_id=first.root.id,
            content={"type": "text", "text": "This must not cross tunnels."},
        )

    assert second.cycle.message_count == 1


def test_mentions_are_deduplicated_and_snapshot_names_are_stable(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    mentioned, _ = credential_factory(name="Original name")
    created = _create(creator)

    replay_value = "mention-reply-0001"
    posted = post_reply(
        credential=creator,
        idempotency_key=replay_value,
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Please inspect this result."},
        mentions=[mentioned.id],
    )
    mention = posted.message.mentions.get()
    mentioned.name = "Renamed agent"
    mentioned.save(update_fields=["name"])

    assert mention.display_name == "Original name"
    with pytest.raises(InvalidRequest, match="more than once"):
        post_reply(
            credential=creator,
            idempotency_key="mention-reply-duplicate",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Duplicate mention."},
            mentions=[mentioned.id, mentioned.id],
        )


def test_tree_snapshot_excludes_later_replies_and_read_does_not_mutate(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator)
    first = post_reply(
        credential=creator,
        idempotency_key="snapshot-reply-0001",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "First branch."},
    )

    page_one = read_tree(credential=reader, address=created.address, limit=1)
    assert [message.id for message in page_one.messages] == [created.root.id]
    assert page_one.snapshot_sequence == 2
    assert page_one.next_after_sequence == 1

    post_reply(
        credential=creator,
        idempotency_key="snapshot-reply-0002",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Later branch."},
    )
    page_two = read_tree(
        credential=reader,
        address=created.address,
        limit=10,
        after_sequence=page_one.next_after_sequence or 0,
        snapshot_sequence=page_one.snapshot_sequence,
    )

    assert [message.id for message in page_two.messages] == [first.message.id]
    assert page_two.complete is True
    assert Message.objects.filter(cycle=created.cycle).count() == 3


def test_close_makes_cycle_read_only_but_keeps_address_for_history(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator)

    close_cycle(
        credential=creator,
        idempotency_key="close-cycle-001",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )

    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert created.tunnel.state == Tunnel.State.DORMANT
    assert created.cycle.state == Cycle.State.CLOSED
    assert created.cycle.final_sequence == 1
    assert created.tunnel.addresses.get().state == TunnelAddress.State.CURRENT
    assert read_tree(credential=reader, address=created.address).messages[0].id == created.root.id
    with pytest.raises(CycleClosed):
        post_reply(
            credential=creator,
            idempotency_key="closed-cycle-reply",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Too late."},
        )


def test_only_creator_can_close_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    outsider, _ = credential_factory()
    created = _create(creator)

    with pytest.raises(TunnelUnavailable):
        close_cycle(
            credential=outsider,
            idempotency_key="close-cycle-outside-001",
            address=created.address,
            expected_cycle_id=created.cycle.id,
        )

    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.ACTIVE


def test_close_cycle_replays_exact_request_and_rejects_changed_request(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    first = _create(creator)
    second = _create(creator, key="tree-create-second")

    close_cycle(
        credential=creator,
        idempotency_key="close-cycle-replay",
        address=first.address,
        expected_cycle_id=first.cycle.id,
    )
    close_cycle(
        credential=creator,
        idempotency_key="close-cycle-replay",
        address=first.address,
        expected_cycle_id=first.cycle.id,
    )
    with pytest.raises(IdempotencyConflict):
        close_cycle(
            credential=creator,
            idempotency_key="close-cycle-replay",
            address=second.address,
            expected_cycle_id=second.cycle.id,
        )


def test_expired_idempotency_key_creates_a_new_random_address(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    first = _create(creator, key="expired-create-key")
    record = creator.idempotency_records.get()
    record.expires_at = timezone.now() - timedelta(seconds=1)
    record.save(update_fields=["expires_at"])

    second = _create(creator, key="expired-create-key")

    assert second.replay is False
    assert second.tunnel.id != first.tunnel.id
    assert second.address != first.address


def test_database_rejects_second_root_in_one_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator)

    with pytest.raises(IntegrityError):
        Message.objects.create(
            tunnel=created.tunnel,
            cycle=created.cycle,
            parent=None,
            sender=creator,
            sender_name=creator.name,
            sequence=99,
            depth=0,
            payload_type=Message.PayloadType.TEXT,
            text_payload="Another root",
            byte_count=12,
            content_digest=b"x" * 32,
        )


@pytest.mark.parametrize("operation", ["close", "rollover"])
def test_lifecycle_rechecks_replay_after_initial_miss(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from tunnels import services

    creator, _ = credential_factory()
    created = _create(creator, "replay-race-create")
    arguments = dict(
        credential=creator,
        address=created.address,
        expected_cycle_id=created.cycle.id,
        idempotency_key="replay-race-operation",
    )
    if operation == "rollover":
        arguments.update(
            expected_address_generation=1,
            root_content={"type": "text", "text": "Next root."},
        )
    action = services.close_cycle if operation == "close" else services.rollover_cycle
    first = action(**arguments)
    monkeypatch.setattr(services, "find_idempotency_replay", lambda **kwargs: None)
    second = action(**arguments)
    if operation == "rollover":
        assert first is not None
        assert second is not None
        assert second.cycle.id == first.cycle.id
    assert Cycle.objects.filter(tunnel=created.tunnel).count() == (
        2 if operation == "rollover" else 1
    )
    assert (
        TunnelEvent.objects.filter(
            tunnel=created.tunnel, event_type=TunnelEvent.Type.CYCLE_CLOSED
        ).count()
        == 1
    )
