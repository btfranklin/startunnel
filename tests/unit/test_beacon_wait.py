"""Shared board waits use only the board revision and captured expiry."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.utils import timezone

from core.cursors import decode_cursor, encode_cursor
from tunnels.beacons import BOARD_WAIT_CURSOR_KIND, publish_beacon, read_board, wait_for_board
from tunnels.errors import InvalidCredential, InvalidCursor, InvalidRequest
from tunnels.models import ActivityCheckpoint, Beacon, BoardState
from tunnels.services import create_tunnel

pytestmark = pytest.mark.django_db(transaction=True)


def _publish(credential: Any, key: str = "wait-beacon") -> Any:
    created = create_tunnel(
        credential=credential,
        idempotency_key=f"{key}-create-0001",
        root_content={"type": "text", "text": "Review this proposal."},
    )
    result, _ = publish_beacon(
        credential=credential,
        idempotency_key=f"{key}-publish-0001",
        address=created.address,
        text="Please review.",
    )
    return created, result


def test_wait_cursor_is_shared_and_zero_wait_refreshes_without_checkpoint_changes(
    credential_factory: Any,
) -> None:
    publisher, _ = credential_factory(name="Publisher")
    reader, _ = credential_factory(name="Reader")
    other_reader, _ = credential_factory(name="Other reader")
    _created, _result = _publish(publisher)
    cursor = read_board(credential=reader).board_cursor

    response = async_to_sync(wait_for_board)(
        credential=other_reader, after_cursor=cursor, wait_seconds=0
    )

    assert response == {"refresh_required": False, "board_cursor": cursor}
    assert BoardState.objects.get(pk=1).position == 1
    assert not ActivityCheckpoint.objects.exists()


def test_wait_reports_a_new_revision_and_rejects_future_or_wrong_kind_cursors(
    credential_factory: Any,
) -> None:
    publisher, _ = credential_factory()
    reader, _ = credential_factory()
    cursor = read_board(credential=reader).board_cursor
    _publish(publisher)

    assert (
        async_to_sync(wait_for_board)(credential=reader, after_cursor=cursor, wait_seconds=0)[
            "refresh_required"
        ]
        is True
    )
    with pytest.raises(InvalidCursor):
        async_to_sync(wait_for_board)(
            credential=reader,
            after_cursor="not a cursor",
            wait_seconds=0,
        )
    future = encode_cursor(
        BOARD_WAIT_CURSOR_KIND,
        board_position=BoardState.objects.get(pk=1).position + 1,
        next_expiry=None,
    )
    with pytest.raises(InvalidCursor):
        async_to_sync(wait_for_board)(credential=reader, after_cursor=future, wait_seconds=0)


def test_wait_rechecks_active_credential_and_validates_timeout(
    credential_factory: Any,
) -> None:
    reader, _ = credential_factory()
    cursor = read_board(credential=reader).board_cursor
    reader.revoked_at = timezone.now()
    reader.save(update_fields=["revoked_at"])

    with pytest.raises(InvalidCredential):
        async_to_sync(wait_for_board)(credential=reader, after_cursor=cursor, wait_seconds=0)
    reader.revoked_at = None
    reader.save(update_fields=["revoked_at"])
    with pytest.raises(InvalidRequest):
        async_to_sync(wait_for_board)(credential=reader, after_cursor=cursor, wait_seconds=21)


def test_wait_expiry_wakes_without_maintenance_and_unregisters(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, _ = credential_factory()
    reader, _ = credential_factory()
    _created, result = _publish(publisher)
    Beacon.objects.filter(pk=result["id"]).update(
        expires_at=timezone.now() + timedelta(milliseconds=80)
    )
    cursor = read_board(credential=reader).board_cursor
    waiters: set[asyncio.Event] = set()

    class Listener:
        def register_board(self) -> asyncio.Event:
            waiter = asyncio.Event()
            waiters.add(waiter)
            return waiter

        def unregister_board(self, waiter: asyncio.Event) -> None:
            waiters.remove(waiter)

        async def wait(self, waiter: asyncio.Event, timeout: float) -> None:
            assert timeout <= 0.1
            await asyncio.sleep(timeout)

    monkeypatch.setattr("tunnels.beacons.activity_listener", Listener())
    response = async_to_sync(wait_for_board)(credential=reader, after_cursor=cursor, wait_seconds=2)

    assert response == {"refresh_required": True, "board_cursor": cursor}
    assert waiters == set()
    assert Beacon.objects.get(pk=result["id"]).ended_at is None


def test_wait_cursor_has_required_expiry_field(credential_factory: Any) -> None:
    reader, _ = credential_factory()
    malformed = encode_cursor(BOARD_WAIT_CURSOR_KIND, board_position=0)

    with pytest.raises(InvalidCursor):
        async_to_sync(wait_for_board)(credential=reader, after_cursor=malformed, wait_seconds=0)


def test_wait_cursor_carries_global_revision_and_expiry(credential_factory: Any) -> None:
    publisher, _ = credential_factory()
    reader, _ = credential_factory()
    _publish(publisher)
    cursor = read_board(credential=reader).board_cursor
    decoded = decode_cursor(cursor, kind=BOARD_WAIT_CURSOR_KIND)

    assert decoded["board_position"] == BoardState.objects.get(pk=1).position
    assert decoded["next_expiry"] is not None


def test_board_page_cursor_preserves_earliest_expiry_across_pages(
    credential_factory: Any,
) -> None:
    publisher, _ = credential_factory()
    reader, _ = credential_factory()
    _older, older_result = _publish(publisher, "wait-expiry-old")
    _newer, newer_result = _publish(publisher, "wait-expiry-new")
    earliest = timezone.now() + timedelta(seconds=120)
    later = earliest + timedelta(seconds=120)
    Beacon.objects.filter(pk=older_result["id"]).update(expires_at=earliest)
    Beacon.objects.filter(pk=newer_result["id"]).update(expires_at=later)

    first = read_board(credential=reader, limit=1)
    second = read_board(credential=reader, limit=1, page_cursor=first.next_cursor)
    page_cursor = decode_cursor(first.next_cursor or "", kind="beacon_board_page")
    wait_cursor = decode_cursor(first.board_cursor, kind=BOARD_WAIT_CURSOR_KIND)

    assert page_cursor["next_expiry"] == earliest.isoformat()
    assert wait_cursor["next_expiry"] == earliest.isoformat()
    assert second.board_cursor == first.board_cursor


def test_wait_registers_before_reread_and_cleans_up_on_cancellation(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, _ = credential_factory()
    cursor = read_board(credential=reader).board_cursor
    waiters: set[asyncio.Event] = set()

    class Listener:
        def register_board(self) -> asyncio.Event:
            waiter = asyncio.Event()
            waiters.add(waiter)
            return waiter

        def unregister_board(self, waiter: asyncio.Event) -> None:
            waiters.remove(waiter)

        async def wait(self, waiter: asyncio.Event, timeout: float) -> None:
            raise asyncio.CancelledError

    listener = Listener()
    monkeypatch.setattr("tunnels.beacons.activity_listener", listener)

    def read_state(*, credential: Any, after_cursor: str) -> tuple[bool, float | None]:
        assert waiters
        assert after_cursor == cursor
        return False, None

    monkeypatch.setattr("tunnels.beacons._read_board_wait_state_for_async", read_state)
    with pytest.raises(asyncio.CancelledError):
        async_to_sync(wait_for_board)(credential=reader, after_cursor=cursor, wait_seconds=2)
    assert waiters == set()
