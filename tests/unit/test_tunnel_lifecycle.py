"""High-value domain tests for stable tunnels and immutable cycle trees."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import TypedDict
from uuid import UUID, uuid4

import psycopg
import pytest
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pytest_django.fixtures import DjangoAssertNumQueries, Settings
from tests.factories import CredentialFactory

from agents.models import AgentCredential
from core.errors import (
    CycleClosed,
    IdempotencyConflict,
    InvalidCredential,
    InvalidRequest,
    TunnelUnavailable,
)
from tunnels.cycles import CreatedTunnel, StartedCycle, close_cycle, create_tunnel
from tunnels.messages import _resolve_mentions, post_reply, read_tree
from tunnels.models import Cycle, Message, Tunnel, TunnelAddress, TunnelEvent

pytestmark = pytest.mark.django_db


class PostReplyArguments(TypedDict):
    credential: AgentCredential
    idempotency_key: str
    address: str
    parent_id: UUID
    content: Mapping[str, object]


class CloseCycleArguments(TypedDict):
    credential: AgentCredential
    address: str
    expected_cycle_id: UUID
    idempotency_key: str


def _create(
    credential: AgentCredential,
    key: str = "create-tree-0001",
    *,
    expires_in_seconds: int | None = None,
) -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        label="Release coordination",
        cycle_label="Initial review",
        root_content={"type": "text", "text": "Review release 42."},
        expires_in_seconds=expires_in_seconds,
    )


def test_create_is_atomic_and_builds_one_rooted_cycle(
    credential_factory: CredentialFactory,
) -> None:
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


def test_create_replay_returns_same_tree_and_keeps_address_out_of_receipt(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()

    first = _create(creator, key="create-replay-0001")
    replay = _create(creator, key="create-replay-0001")

    assert replay.replay is True
    assert replay.tunnel.id == first.tunnel.id
    assert replay.cycle.id == first.cycle.id
    assert replay.root.id == first.root.id
    assert replay.address == first.address
    assert first.tunnel.addresses.get().address == first.address
    record = creator.agent_receipts.get()
    assert first.address not in str(record.response)

    with pytest.raises(IdempotencyConflict):
        create_tunnel(
            credential=creator,
            idempotency_key="create-replay-0001",
            label="Changed",
            cycle_label="Initial review",
            root_content={"type": "text", "text": "Review release 42."},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tunnel_state", "unknown"),
        ("cycle_state", "unknown"),
        ("message_count", None),
        ("message_count", False),
        ("message_count", 0),
        ("content_bytes", None),
        ("content_bytes", False),
        ("content_bytes", -1),
        ("event_position", None),
        ("event_position", False),
        ("event_position", 0),
    ],
)
def test_create_replay_rejects_an_invalid_historical_response(
    credential_factory: CredentialFactory, field: str, value: object
) -> None:
    creator, _ = credential_factory()
    _create(creator, key="create-invalid-response")
    record = creator.agent_receipts.get()
    record.response = {**record.response, field: value}
    record.save(update_fields=["response"])

    with pytest.raises(TunnelUnavailable):
        _create(creator, key="create-invalid-response")


def test_failed_root_creation_rolls_back_every_resource(
    credential_factory: CredentialFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, _ = credential_factory()

    def fail_store(message: Message, mentions: list[AgentCredential]) -> None:
        del message, mentions
        raise RuntimeError("injected root failure")

    monkeypatch.setattr("tunnels.cycles._store_mentions", fail_store)
    with pytest.raises(RuntimeError, match="injected root failure"):
        _create(creator, key="create-rollback-0001")

    assert not Tunnel.objects.exists()
    assert not TunnelAddress.objects.exists()
    assert not Cycle.objects.exists()
    assert not Message.objects.exists()
    assert not TunnelEvent.objects.exists()
    assert not creator.agent_receipts.exists()


def test_replies_form_an_immutable_ordered_tree_and_allow_self_reply(
    credential_factory: CredentialFactory,
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


def test_reply_replay_keeps_original_position_without_event_reads_or_writes(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, key="reply-position-create")
    arguments: PostReplyArguments = {
        "credential": creator,
        "idempotency_key": "reply-position-original",
        "address": created.address,
        "parent_id": created.root.id,
        "content": {"type": "text", "text": "Original reply."},
    }
    original = post_reply(**arguments)
    later = post_reply(
        credential=creator,
        idempotency_key="reply-position-later",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Later reply."},
    )
    assert later.event_position > original.event_position
    receipt = creator.agent_receipts.get(operation="post_reply", resource_id=original.message.id)
    original_response = receipt.response.copy()
    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    counters = (
        created.tunnel.next_event_position,
        created.cycle.next_sequence,
        created.cycle.message_count,
        created.cycle.content_bytes,
    )

    with CaptureQueriesContext(connection) as queries:
        replay = post_reply(**arguments)

    assert replay.replay is True
    assert replay.message.id == original.message.id
    assert replay.event_position == original.event_position
    assert not any(TunnelEvent._meta.db_table in query["sql"] for query in queries)
    assert not any(
        query["sql"].lstrip().split(maxsplit=1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}
        for query in queries
    )
    receipt.refresh_from_db()
    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert receipt.response == original_response
    assert (
        created.tunnel.next_event_position,
        created.cycle.next_sequence,
        created.cycle.message_count,
        created.cycle.content_bytes,
    ) == counters


@pytest.mark.parametrize(
    "position_fields",
    [
        {},
        {"event_position": None},
        {"event_position": False},
        {"event_position": True},
        {"event_position": 0},
        {"event_position": -1},
        {"event_position": "3"},
        {"event_position": 3.0},
    ],
    ids=["missing", "null", "false", "true", "zero", "negative", "string", "float"],
)
def test_reply_replay_rejects_an_invalid_stored_event_position(
    credential_factory: CredentialFactory, position_fields: dict[str, object]
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, key="reply-invalid-position-create")
    arguments: PostReplyArguments = {
        "credential": creator,
        "idempotency_key": "reply-invalid-position",
        "address": created.address,
        "parent_id": created.root.id,
        "content": {"type": "text", "text": "Original reply."},
    }
    original = post_reply(**arguments)
    receipt = creator.agent_receipts.get(operation="post_reply", resource_id=original.message.id)
    receipt.response.pop("event_position")
    receipt.response.update(position_fields)
    receipt.save(update_fields=["response"])

    with pytest.raises(TunnelUnavailable):
        post_reply(**arguments)


@pytest.mark.parametrize("field", ["revoked_at", "expires_at"])
def test_reply_replay_rejects_an_unavailable_credential_from_a_stale_object(
    credential_factory: CredentialFactory, field: str
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, key="reply-credential-create")
    arguments: PostReplyArguments = {
        "credential": creator,
        "idempotency_key": "reply-credential-original",
        "address": created.address,
        "parent_id": created.root.id,
        "content": {"type": "text", "text": "Original reply."},
    }
    post_reply(**arguments)
    AgentCredential.objects.filter(pk=creator.pk).update(
        **{field: timezone.now() - timedelta(seconds=1)}
    )
    assert creator.is_available is True

    with pytest.raises(InvalidCredential):
        post_reply(**arguments)


def test_parent_must_belong_to_the_active_cycle(credential_factory: CredentialFactory) -> None:
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
    credential_factory: CredentialFactory,
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
    credential_factory: CredentialFactory,
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
    credential_factory: CredentialFactory,
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


def test_only_creator_can_close_cycle(credential_factory: CredentialFactory) -> None:
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
    credential_factory: CredentialFactory,
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


def test_expired_idempotency_key_creates_a_new_random_address(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    first = _create(creator, key="expired-create-key")
    record = creator.agent_receipts.get()
    record.expires_at = timezone.now() - timedelta(seconds=1)
    record.save(update_fields=["expires_at"])

    second = _create(creator, key="expired-create-key")

    assert second.replay is False
    assert second.tunnel.id != first.tunnel.id
    assert second.address != first.address


def test_database_rejects_second_root_in_one_cycle(credential_factory: CredentialFactory) -> None:
    creator, _ = credential_factory()
    created = _create(creator)

    with pytest.raises((IntegrityError, ProgrammingError)) as error, transaction.atomic():
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
        )
    if connection.vendor == "postgresql":
        assert isinstance(error.value.__cause__, psycopg.errors.RaiseException)
        assert error.value.__cause__.sqlstate == "P0001"
    else:
        assert isinstance(error.value, IntegrityError)
    assert Message.objects.filter(cycle=created.cycle).count() == 1


@pytest.mark.parametrize("operation", ["close", "rollover"])
def test_identical_lifecycle_retry_returns_the_original_result(
    credential_factory: CredentialFactory, operation: str
) -> None:
    from tunnels import cycles

    creator, _ = credential_factory()
    created = _create(creator, "replay-race-create")

    def action() -> StartedCycle | None:
        if operation == "close":
            return cycles.close_cycle(
                credential=creator,
                address=created.address,
                expected_cycle_id=created.cycle.id,
                idempotency_key="replay-race-operation",
            )
        return cycles.rollover_cycle(
            credential=creator,
            address=created.address,
            expected_cycle_id=created.cycle.id,
            idempotency_key="replay-race-operation",
            expected_address_generation=1,
            root_content={"type": "text", "text": "Next root."},
        )

    first = action()
    second = action()
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


def test_close_replay_checks_current_creator_authority(
    credential_factory: CredentialFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tunnels import cycles

    creator, _ = credential_factory()
    created = _create(creator, "close-replay-authority")
    arguments: CloseCycleArguments = {
        "credential": creator,
        "address": created.address,
        "expected_cycle_id": created.cycle.id,
        "idempotency_key": "close-replay-authority-operation",
    }
    cycles.close_cycle(**arguments)
    monkeypatch.setattr(cycles, "_can_control_with_credential", lambda **kwargs: False)
    with pytest.raises(TunnelUnavailable):
        cycles.close_cycle(**arguments)


def test_all_mentions_are_resolved_in_one_query(
    credential_factory: CredentialFactory, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    credentials = [credential_factory(name=f"Mention {index}")[0] for index in range(32)]
    ids = [credential.id for credential in reversed(credentials)]
    with django_assert_num_queries(1):
        mentions = _resolve_mentions(mention_ids=ids, maximum=32)
    assert [mention.id for mention in mentions] == sorted(ids)
    assert {mention.name for mention in mentions} == {credential.name for credential in credentials}


@pytest.mark.parametrize("unavailable", ["revoked_at", "expires_at", "missing"])
def test_mention_batch_rejects_any_unavailable_credential(
    unavailable: str,
    credential_factory: CredentialFactory,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    available, _ = credential_factory()
    other, _ = credential_factory()
    missing_id = uuid4()
    if unavailable != "missing":
        setattr(other, unavailable, timezone.now() - timedelta(seconds=1))
        other.save(update_fields=[unavailable])
    with django_assert_num_queries(1), pytest.raises(InvalidRequest, match="not available"):
        _resolve_mentions(
            mention_ids=[available.id, missing_id if unavailable == "missing" else other.id],
            maximum=32,
        )


@pytest.mark.parametrize("operation", ["start", "rollover", "admin_start", "admin_rollover"])
def test_expired_cycle_can_be_replaced_without_maintenance(
    credential_factory: CredentialFactory, settings: Settings, operation: str
) -> None:
    from tunnels.admin_actions import rollover_cycle_as_admin, start_cycle_as_admin
    from tunnels.cycles import get_tunnel_status, rollover_cycle, start_cycle

    creator, _ = credential_factory()
    assert creator.created_by is not None
    created = _create(creator, key="lazy-expiry-create")
    expiry = timezone.now() - timedelta(seconds=30)
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=expiry, retention_seconds=120)
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 999

    status = get_tunnel_status(credential=creator, address=created.address)
    assert status.current_cycle is None
    assert status.tunnel.state == Tunnel.State.DORMANT
    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert created.tunnel.state == Tunnel.State.ACTIVE
    assert created.cycle.state == Cycle.State.ACTIVE

    content = {"type": "text", "text": "Continue after expiry."}
    if operation.startswith("admin_"):
        if operation == "admin_rollover":
            started = rollover_cycle_as_admin(
                actor=creator.created_by,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
                expected_cycle_id=created.cycle.id,
                root_content=content,
            )
        else:
            started = start_cycle_as_admin(
                actor=creator.created_by,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
                root_content=content,
            )
    elif operation == "rollover":
        started = rollover_cycle(
            credential=creator,
            address=created.address,
            idempotency_key="lazy-expiry-next",
            expected_address_generation=1,
            expected_cycle_id=created.cycle.id,
            root_content=content,
        )
    else:
        started = start_cycle(
            credential=creator,
            address=created.address,
            idempotency_key="lazy-expiry-next",
            expected_address_generation=1,
            root_content=content,
        )

    created.cycle.refresh_from_db()
    assert created.cycle.state == Cycle.State.CLOSED
    assert created.cycle.close_reason == "expired"
    assert created.cycle.closed_at == expiry
    assert created.cycle.retention_seconds == 120
    assert created.cycle.delete_after == expiry + timedelta(seconds=120)
    assert started.cycle.number == 2
    assert (
        get_tunnel_status(credential=creator, address=created.address).current_cycle
        == started.cycle
    )


def test_expired_tunnel_keeps_its_reported_dormancy_time_after_maintenance(
    credential_factory: CredentialFactory,
) -> None:
    from core.maintenance import cleanup_once
    from tunnels.cycles import get_tunnel_status

    creator, _ = credential_factory()
    created = _create(creator, key="dormancy-time-create")
    expiry = timezone.now() - timedelta(minutes=10)
    Cycle.objects.filter(pk=created.cycle.pk).update(expires_at=expiry, retention_seconds=3600)

    before = get_tunnel_status(credential=creator, address=created.address)
    assert before.tunnel.state == Tunnel.State.DORMANT
    assert before.tunnel.dormant_at == expiry

    cleanup_once()

    after = get_tunnel_status(credential=creator, address=created.address)
    created.cycle.refresh_from_db()
    assert after.tunnel.state == Tunnel.State.DORMANT
    assert after.tunnel.dormant_at == before.tunnel.dormant_at
    assert created.cycle.closed_at == expiry
    assert created.cycle.delete_after == expiry + timedelta(hours=1)
