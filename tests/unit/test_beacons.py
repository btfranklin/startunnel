"""Beacon publication, discovery, and final lifecycle behavior."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.admin_commands import BeaconWithdraw
from accounts.admin_data import AdminOperationResult
from accounts.admin_services import execute_admin_operation
from accounts.models import AdminReceipt
from core.cursors import decode_cursor, encode_cursor
from core.errors import (
    IdempotencyConflict,
    InvalidCursor,
    InvalidRequest,
    LifecycleConflict,
    TunnelUnavailable,
)
from core.maintenance import cleanup_once
from core.models import AgentReceipt
from tunnels.admin_actions import (
    retire_tunnel_as_admin,
    retirement_confirmation,
    rotate_address_as_admin,
    start_cycle_as_admin,
)
from tunnels.beacons import publish_beacon, read_board, withdraw_beacon
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Beacon, BoardState, Cycle, TunnelEvent
from tunnels.retention import delete_due_cycle_content

pytestmark = pytest.mark.django_db


def _create(credential: Any, suffix: str = "beacon") -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=f"{suffix}-create-0001",
        root_content={"type": "text", "text": "Review the proposal."},
    )


def _reply(
    created: Any,
    credential: Any,
    key: str,
    *,
    mentions: list[Any] | None = None,
    session: str | None = None,
) -> None:
    post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "I can help."},
        mentions=mentions,
        session=session,
    )


def test_publish_retry_is_exact_and_cycle_has_one_final_publication(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Beacon publisher")
    created = _create(creator)
    result, replay = publish_beacon(
        credential=creator,
        idempotency_key="publish-1",
        address=created.address,
        text="Review this.",
    )
    retry, retried = publish_beacon(
        credential=creator,
        idempotency_key="publish-1",
        address=created.address,
        text="Review this.",
    )

    assert replay is False and retried is True
    assert retry == result
    assert BoardState.objects.get(pk=1).position == 1
    with pytest.raises(IdempotencyConflict):
        publish_beacon(
            credential=creator,
            idempotency_key="publish-1",
            address=created.address,
            text="Different text.",
        )
    with pytest.raises(LifecycleConflict):
        publish_beacon(
            credential=creator,
            idempotency_key="publish-2",
            address=created.address,
            text="Second publication.",
        )

    end, _ = withdraw_beacon(
        credential=creator,
        idempotency_key="withdraw1",
        address=created.address,
        beacon_id=UUID(result["id"]),
    )
    assert end["end_reason"] == Beacon.EndReason.WITHDRAWN
    retry_end, replay_end = withdraw_beacon(
        credential=creator,
        idempotency_key="withdraw1",
        address=created.address,
        beacon_id=UUID(result["id"]),
    )
    assert replay_end is True and retry_end == end
    assert BoardState.objects.get(pk=1).position == 2


@pytest.mark.parametrize("text", ["", " leading", "trailing ", "line\nbreak", "x\u200by"])
def test_beacon_text_rejects_empty_outer_space_and_control_or_format_chars(
    credential_factory: Any, text: str
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-text")
    with pytest.raises(InvalidRequest):
        publish_beacon(
            credential=creator,
            idempotency_key="publish-text-0001",
            address=created.address,
            text=text,
        )


def test_board_counts_distinct_nonpublisher_credentials_that_reply_after_publish(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Publisher")
    responder, _ = credential_factory(name="Responder")
    other, _ = credential_factory(name="Other responder")
    mentioned, _ = credential_factory(name="Mentioned only")
    created = _create(creator, "beacon-count")
    _reply(created, responder, "reply-before-0001", session="early-chat")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="count0001",
        address=created.address,
        text="Need review.",
    )
    _reply(created, creator, "reply-publisher-0001")
    _reply(created, responder, "reply-after-0001", session="session-a")
    _reply(created, responder, "reply-after-0002", session="session-b")
    _reply(created, other, "reply-after-0003", mentions=[mentioned.id])

    page = read_board(credential=other, limit=20)

    assert page.items[0]["id"] == result["id"]
    assert page.items[0]["response_count"] == 2
    assert page.items[0]["address"] == created.address
    assert "address" not in result
    assert BoardState.objects.get(pk=1).position == 3


def test_board_cursor_keeps_snapshot_across_active_credentials_and_page_sizes(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Board publisher")
    reader, _ = credential_factory(name="Board reader")
    first = _create(creator, "beacon-page-1")
    first_result, _ = publish_beacon(
        credential=creator, idempotency_key="publish-page-1", address=first.address, text="First."
    )
    second = _create(creator, "beacon-page-2")
    second_result, _ = publish_beacon(
        credential=creator, idempotency_key="publish-page-2", address=second.address, text="Second."
    )
    page1 = read_board(credential=reader, limit=1)
    third = _create(creator, "beacon-page-3")
    publish_beacon(
        credential=creator, idempotency_key="publish-page-3", address=third.address, text="Later."
    )

    page2 = read_board(credential=reader, limit=2, page_cursor=page1.next_cursor)

    assert page1.items[0]["id"] == second_result["id"]
    assert page2.items[0]["id"] == first_result["id"]
    assert page2.snapshot_position == page1.snapshot_position
    assert page2.board_cursor == page1.board_cursor
    assert page2.next_cursor is None
    with pytest.raises(InvalidCursor):
        read_board(credential=reader, limit=1, page_cursor=page1.board_cursor)
    with pytest.raises(InvalidCursor):
        read_board(credential=reader, limit=1, page_cursor=encode_cursor("activity", position=0))
    continuation = decode_cursor(page1.next_cursor or "", kind="beacon_board_page")
    with pytest.raises(InvalidCursor):
        read_board(
            credential=reader,
            limit=1,
            page_cursor=encode_cursor(
                "beacon_board_page",
                snapshot_position=BoardState.objects.get(pk=1).position + 1,
                before_position=page1.snapshot_position,
                next_expiry=continuation["next_expiry"],
            ),
        )
    with pytest.raises(InvalidCursor):
        read_board(
            credential=reader,
            limit=1,
            page_cursor=encode_cursor(
                "beacon_board_page",
                snapshot_position=page1.snapshot_position,
                before_position=0,
                next_expiry=continuation["next_expiry"],
            ),
        )
    with pytest.raises(InvalidCursor):
        read_board(
            credential=reader,
            limit=1,
            page_cursor=encode_cursor(
                "beacon_board_page",
                snapshot_position=page1.snapshot_position,
                before_position=1,
            ),
        )


def test_cycle_close_ends_beacon_after_cycle_events(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-close")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="close0001",
        address=created.address,
        text="Join review.",
    )

    close_cycle(
        credential=creator,
        idempotency_key="close-beacon-0001",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )

    beacon = Beacon.objects.get(pk=result["id"])
    events = list(TunnelEvent.objects.filter(tunnel=created.tunnel).order_by("position"))
    assert beacon.end_reason == Beacon.EndReason.CYCLE_CLOSED
    assert [event.position for event in events] == list(range(1, 7))
    assert events[-1].event_type == TunnelEvent.Type.BEACON_ENDED
    assert read_board(credential=creator).items == ()


def test_new_cycle_can_publish_a_new_beacon(credential_factory: Any, user_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-new-cycle")
    first, _ = publish_beacon(
        credential=creator,
        idempotency_key="publish-old-cycle-0001",
        address=created.address,
        text="First cycle.",
    )
    close_cycle(
        credential=creator,
        idempotency_key="close-old-cycle-0001",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    started = start_cycle_as_admin(
        actor=user_factory(),
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        root_content={"type": "text", "text": "Second cycle root."},
    )

    second, _ = publish_beacon(
        credential=creator,
        idempotency_key="publish-new-cycle-0001",
        address=created.address,
        text="Second cycle.",
    )

    assert first["cycle_id"] != second["cycle_id"]
    assert second["cycle_id"] == str(started.cycle.id)


def test_only_current_cycle_creator_can_publish_or_withdraw(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Cycle creator")
    other, _ = credential_factory(name="Other agent")
    created = _create(creator, "beacon-authority")

    with pytest.raises(TunnelUnavailable):
        publish_beacon(
            credential=other,
            idempotency_key="other0001",
            address=created.address,
            text="Join this work.",
        )
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="publish-authority-0001",
        address=created.address,
        text="Join this work.",
    )
    with pytest.raises(TunnelUnavailable):
        withdraw_beacon(
            credential=other,
            idempotency_key="withdraw-other-0001",
            address=created.address,
            beacon_id=UUID(result["id"]),
        )


def test_expiry_hides_beacon_before_maintenance_and_cleanup_records_end(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-expiry")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="expiry001",
        address=created.address,
        text="Need review.",
        lifetime_seconds=60,
    )
    expiry = timezone.now() + timedelta(seconds=60)
    Beacon.objects.filter(pk=result["id"]).update(expires_at=expiry)

    assert read_board(credential=creator).items[0]["id"] == result["id"]
    counts = cleanup_once(now=expiry + timedelta(seconds=1))
    beacon = Beacon.objects.get(pk=result["id"])
    assert counts["beacons_expired"] == 1
    assert beacon.end_reason == Beacon.EndReason.EXPIRED
    assert beacon.ended_at == beacon.expires_at


def test_address_rotation_withdraws_beacon_and_retirement_keeps_it_hidden(
    credential_factory: Any, user_factory: Any
) -> None:
    creator, _ = credential_factory(name="Address publisher")
    created = _create(creator, "beacon-address")
    publish_beacon(
        credential=creator,
        idempotency_key="publish-address-0001",
        address=created.address,
        text="Need review.",
    )
    assert BoardState.objects.get(pk=1).position == 1
    operator = user_factory(username="beacon-address-operator")

    rotate_address_as_admin(
        actor=operator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )
    rotated = created.tunnel.addresses.get(state="current")
    page = read_board(credential=creator)
    assert page.items == ()
    beacon = Beacon.objects.get(cycle=created.cycle)
    assert beacon.ended_at is not None
    assert beacon.end_reason == Beacon.EndReason.WITHDRAWN
    assert rotated.address != created.address
    assert BoardState.objects.get(pk=1).position == 2

    retire_tunnel_as_admin(
        actor=operator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=2,
        confirmation=retirement_confirmation(created.tunnel),
    )
    assert read_board(credential=creator).items == ()


def test_retention_deletes_beacon_text_and_keeps_retry_receipts(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-retention")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="retain001",
        address=created.address,
        text="Need review.",
    )
    withdraw_beacon(
        credential=creator,
        idempotency_key="endret001",
        address=created.address,
        beacon_id=UUID(result["id"]),
    )
    close_cycle(
        credential=creator,
        idempotency_key="close-retention-0001",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    Cycle.objects.filter(pk=created.cycle.id).update(
        delete_after=timezone.now() - timedelta(seconds=1)
    )

    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=timezone.now())
    assert not Beacon.objects.filter(pk=result["id"]).exists()
    receipts = list(AgentReceipt.objects.filter(resource_id=result["id"]))
    assert len(receipts) == 2
    assert all(receipt.response == {} for receipt in receipts)
    with pytest.raises(TunnelUnavailable):
        publish_beacon(
            credential=creator,
            idempotency_key="retain001",
            address=created.address,
            text="Need review.",
        )
    with pytest.raises(TunnelUnavailable):
        withdraw_beacon(
            credential=creator,
            idempotency_key="endret001",
            address=created.address,
            beacon_id=UUID(result["id"]),
        )
    with pytest.raises(IdempotencyConflict):
        publish_beacon(
            credential=creator,
            idempotency_key="retain001",
            address=created.address,
            text="Changed request.",
        )


def test_retention_clears_admin_beacon_summary_and_rejects_replay(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "admin-beacon-retention")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="publish-admin-retention",
        address=created.address,
        text="Temporary Beacon text.",
    )

    def withdraw() -> AdminOperationResult:
        request_data = {"beacon_id": result["id"]}
        return execute_admin_operation(
            actor=creator.created_by,
            command=BeaconWithdraw.model_validate(request_data),
            key="withdraw-admin-retention",
            request_data=request_data,
        )

    response = withdraw()
    receipt = AdminReceipt.objects.get(pk=response["operation"]["id"])
    fingerprint = bytes(receipt.request_digest)
    close_cycle(
        credential=creator,
        idempotency_key="close-admin-retention",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    Cycle.objects.filter(pk=created.cycle.id).update(
        delete_after=timezone.now() - timedelta(seconds=1)
    )
    assert delete_due_cycle_content(cycle_id=created.cycle.id, now=timezone.now())
    receipt.refresh_from_db()
    assert receipt.response == {}
    assert bytes(receipt.request_digest) == fingerprint
    with pytest.raises(InvalidRequest):
        withdraw()
    assert AdminReceipt.objects.count() == 1


@pytest.mark.parametrize("lifetime", [0, 86_401, True, 1.5])
def test_publish_rejects_lifetime_outside_integer_bounds(
    credential_factory: Any, lifetime: Any
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-lifetime-bound")
    with pytest.raises(InvalidRequest):
        publish_beacon(
            credential=creator,
            idempotency_key="publish-lifetime-bounds",
            address=created.address,
            text="Review.",
            lifetime_seconds=lifetime,
        )


def test_default_lifetime_and_cycle_deadline_cap(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    long_cycle = create_tunnel(
        credential=creator,
        idempotency_key="beacon-lifetime-long-create",
        root_content={"type": "text", "text": "Long cycle."},
        expires_in_seconds=86_400,
    )
    default, _ = publish_beacon(
        credential=creator,
        idempotency_key="beacon-lifetime-default",
        address=long_cycle.address,
        text="Default lifetime.",
    )
    default_seconds = (
        datetime.fromisoformat(default["expires_at"])
        - datetime.fromisoformat(default["published_at"])
    ).total_seconds()
    assert 3_599 < default_seconds <= 3_600

    short_cycle = create_tunnel(
        credential=creator,
        idempotency_key="beacon-lifetime-short-create",
        root_content={"type": "text", "text": "Short cycle."},
        expires_in_seconds=300,
    )
    capped, _ = publish_beacon(
        credential=creator,
        idempotency_key="beacon-lifetime-capped",
        address=short_cycle.address,
        text="Capped lifetime.",
        lifetime_seconds=86_400,
    )
    assert datetime.fromisoformat(capped["expires_at"]) == short_cycle.cycle.expires_at


def test_query_count_is_constant_for_populated_board_pages(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Query bound publisher")
    reader, _ = credential_factory(name="Query bound reader")
    for index in range(3):
        created = _create(creator, f"beacon-query-{index}")
        publish_beacon(
            credential=creator,
            idempotency_key=f"publish-query-{index:04d}",
            address=created.address,
            text=f"Review {index}.",
        )

    with CaptureQueriesContext(connection) as small_page_queries:
        small_page = read_board(credential=reader, limit=1)
    with CaptureQueriesContext(connection) as full_page_queries:
        full_page = read_board(credential=reader, limit=3)

    assert len(small_page.items) == 1
    assert len(full_page.items) == 3
    assert len(small_page_queries) == len(full_page_queries)


def test_beacon_ended_between_pages_disappears_from_continuation(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Removal publisher")
    reader, _ = credential_factory(name="Removal reader")
    older = _create(creator, "beacon-removal-older")
    older_result, _ = publish_beacon(
        credential=creator,
        idempotency_key="publish-removal-older",
        address=older.address,
        text="Older item.",
    )
    newer = _create(creator, "beacon-removal-newer")
    publish_beacon(
        credential=creator,
        idempotency_key="publish-removal-newer",
        address=newer.address,
        text="Newer item.",
    )
    first = read_board(credential=reader, limit=1)

    withdraw_beacon(
        credential=creator,
        idempotency_key="withdraw-removal-older",
        address=older.address,
        beacon_id=UUID(older_result["id"]),
    )
    continuation = read_board(credential=reader, limit=1, page_cursor=first.next_cursor)

    assert continuation.items == ()
    assert continuation.complete is True


def test_beacon_deadline_hides_it_without_maintenance(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "beacon-deadline")
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="beacon-deadline-publish",
        address=created.address,
        text="Review request.",
    )
    Beacon.objects.filter(pk=result["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert Beacon.objects.get(pk=result["id"]).ended_at is None
    assert read_board(credential=creator).items == ()
