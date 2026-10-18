"""Domain operations for the instance-wide Beacon board."""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, QuerySet, Subquery
from django.db.models.functions import Now
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from core import limits as instance_limits
from core.activity_listener import activity_listener
from core.async_database import ASYNC_DATABASE_EXECUTOR
from core.audit import record_event
from core.cursors import cursor_int, decode_cursor, encode_cursor
from core.limits import (
    DEFAULT_BOARD_LIMIT,
    MAX_BEACON_CHARACTERS,
    MAX_BEACON_SECONDS,
    MAX_BOARD_LIMIT,
)
from core.logging import current_request_id
from core.notifications import ACTIVITY_CHANNEL, BOARD_CHANNEL, notify, notify_maintenance

from .access import credential_is_active, cycle_is_readable, resolve_tunnel
from .codec import canonical_address
from .errors import (
    CycleClosed,
    InvalidCredential,
    InvalidCursor,
    InvalidRequest,
    LifecycleConflict,
    TunnelUnavailable,
)
from .idempotency import begin as begin_idempotency
from .idempotency import find_replay, finish
from .models import (
    Beacon,
    BoardState,
    Cycle,
    IdempotencyRecord,
    Message,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)

DEFAULT_BEACON_SECONDS = 3_600
BOARD_PAGE_CURSOR_KIND = "beacon_board_page"
BOARD_WAIT_CURSOR_KIND = "beacon_board_wait"


@dataclass(frozen=True, slots=True)
class BoardPage:
    items: tuple[dict[str, Any], ...]
    snapshot_position: int
    board_cursor: str
    next_cursor: str | None
    complete: bool


def _advance_board_position() -> int:
    """Lock and increment the single revision row."""

    state, _ = BoardState.objects.select_for_update().get_or_create(pk=1)
    state.position += 1
    state.save(update_fields=["position"])
    notify(BOARD_CHANNEL)
    return state.position


def beacon_queryset() -> QuerySet[Beacon]:
    """Return Beacon rows with their publisher and live distinct-reply count."""

    responders = (
        Message.objects.filter(
            cycle_id=OuterRef("cycle_id"),
            sequence__gt=OuterRef("publication_sequence"),
            parent__isnull=False,
        )
        .exclude(sender_id=OuterRef("publisher_id"))
        .order_by()
        .values("cycle_id")
        .annotate(total=Count("sender_id", distinct=True))
        .values("total")
    )
    return Beacon.objects.select_related("cycle", "publisher").annotate(
        response_count=Subquery(responders[:1])
    )


def note_beacon_response(*, cycle: Cycle, message: Message) -> None:
    """Advance the board when a credential first replies after publication."""

    if message.parent_id is None:
        return
    earlier_responses = Message.objects.filter(
        cycle_id=OuterRef("cycle_id"),
        sender_id=message.sender_id,
        sequence__gt=OuterRef("publication_sequence"),
        parent__isnull=False,
    ).exclude(pk=message.pk)
    beacon = (
        Beacon.objects.select_for_update(of=("self",))
        .filter(
            cycle=cycle,
            publication_sequence__lt=message.sequence,
            ended_at__isnull=True,
            expires_at__gt=Now(),
            cycle__expires_at__gt=Now(),
            cycle__tunnel__state=Tunnel.State.ACTIVE,
        )
        .exclude(publisher_id=message.sender_id)
        .annotate(has_earlier_response=Exists(earlier_responses))
        .filter(has_earlier_response=False)
        .first()
    )
    if beacon is not None:
        _advance_board_position()


def beacon_data(beacon: Beacon) -> dict[str, Any]:
    """Serialize the public summary without an address."""

    response_count = beacon.response_count or 0  # type: ignore[attr-defined]
    return {
        "id": str(beacon.id),
        "tunnel_id": str(beacon.cycle.tunnel_id),
        "cycle_id": str(beacon.cycle_id),
        "publisher": {"id": str(beacon.publisher_id), "name": beacon.publisher.name},
        "text": beacon.text,
        "publication_sequence": beacon.publication_sequence,
        "board_position": beacon.board_position,
        "published_at": beacon.published_at.isoformat(),
        "expires_at": beacon.expires_at.isoformat(),
        "ended_at": beacon.ended_at.isoformat() if beacon.ended_at else None,
        "end_reason": beacon.end_reason,
        "response_count": response_count,
    }


def _cursor_expiry(cursor: dict[str, Any]) -> datetime | None:
    if "next_expiry" not in cursor:
        raise InvalidCursor()
    value = cursor["next_expiry"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidCursor()
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidCursor() from error
    if not timezone.is_aware(expiry):
        raise InvalidCursor()
    return expiry


def _next_visible_expiry(*, snapshot_position: int) -> datetime | None:
    current_address = TunnelAddress.objects.filter(
        tunnel_id=OuterRef("cycle__tunnel_id"), state=TunnelAddress.State.CURRENT
    ).values("address")[:1]
    value = (
        Beacon.objects.filter(
            board_position__lte=snapshot_position,
            ended_at__isnull=True,
            expires_at__gt=Now(),
            cycle__state=Cycle.State.ACTIVE,
            cycle__expires_at__gt=Now(),
            cycle__tunnel__state=Tunnel.State.ACTIVE,
        )
        .annotate(current_address=Subquery(current_address))
        .filter(current_address__isnull=False)
        .order_by("expires_at")
        .values_list("expires_at", flat=True)
        .first()
    )
    return value


def _validate_beacon_text(text: str) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_BEACON_CHARACTERS:
        raise InvalidRequest(f"Beacon text must contain 1 to {MAX_BEACON_CHARACTERS} characters.")
    if text != text.strip():
        raise InvalidRequest("Beacon text cannot start or end with whitespace.")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in text):
        raise InvalidRequest("Beacon text cannot contain control or format characters.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidRequest("Beacon text must contain valid Unicode.") from error


def _database_now() -> datetime:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            return cast(datetime, cursor.fetchone()[0])
    return timezone.now()


def _locked_tunnel_and_cycle(*, credential: AgentCredential, address: str) -> tuple[Tunnel, Cycle]:
    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    cycle = Cycle.objects.select_for_update().filter(tunnel=tunnel).order_by("-number").first()
    if cycle is None:
        raise CycleClosed()
    return tunnel, cycle


def _end_locked(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    beacon: Beacon,
    reason: str,
    actor_credential: AgentCredential | None = None,
    actor_user: User | None = None,
) -> None:
    if beacon.ended_at is not None:
        return
    _advance_board_position()
    now = _database_now()
    if beacon.expires_at <= now:
        reason = Beacon.EndReason.EXPIRED
    beacon.ended_at = beacon.expires_at if reason == Beacon.EndReason.EXPIRED else now
    beacon.end_reason = reason
    beacon.save(update_fields=["ended_at", "end_reason"])
    event_position = tunnel.next_event_position
    tunnel.next_event_position = event_position + 1
    tunnel.save(update_fields=["next_event_position"])
    TunnelEvent.objects.create(
        tunnel=tunnel,
        cycle=cycle,
        position=event_position,
        event_type=TunnelEvent.Type.BEACON_ENDED,
        metadata={"beacon_id": str(beacon.id), "reason": reason},
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    record_event(
        actor_credential=actor_credential,
        actor_user=actor_user,
        action="beacon.ended",
        target_type="beacon",
        target_id=beacon.id,
        request_id=current_request_id(),
        metadata={"cycle_id": str(cycle.id), "reason": reason},
    )


def end_beacon_for_cycle_locked(
    *, tunnel: Tunnel, cycle: Cycle, reason: str = Beacon.EndReason.CYCLE_CLOSED
) -> None:
    """End a cycle's Beacon after the cycle's own events have claimed positions."""

    beacon = Beacon.objects.select_for_update().filter(cycle=cycle).first()
    if beacon is not None and beacon.ended_at is None:
        _end_locked(tunnel=tunnel, cycle=cycle, beacon=beacon, reason=reason)


def _replayed_beacon(record: IdempotencyRecord) -> dict[str, Any]:
    if record.resource_id is None:
        raise TunnelUnavailable()
    beacon = Beacon.objects.select_related("cycle").filter(pk=record.resource_id).first()
    if beacon is None or not cycle_is_readable(beacon.cycle):
        raise TunnelUnavailable()
    return dict(record.response)


@transaction.atomic
def publish_beacon(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    text: str,
    lifetime_seconds: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Publish the one Beacon allowed in the current cycle."""

    _validate_beacon_text(text)
    lifetime = DEFAULT_BEACON_SECONDS if lifetime_seconds is None else lifetime_seconds
    if (
        isinstance(lifetime, bool)
        or not isinstance(lifetime, int)
        or not 1 <= lifetime <= MAX_BEACON_SECONDS
    ):
        raise InvalidRequest(f"lifetime_seconds must be from 1 through {MAX_BEACON_SECONDS}.")
    canonical = canonical_address(address)
    request_data = {"address": canonical, "text": text, "lifetime_seconds": lifetime_seconds}
    tunnel, cycle = _locked_tunnel_and_cycle(credential=credential, address=address)
    beacon = Beacon.objects.select_for_update().filter(cycle=cycle).first()
    replay = find_replay(
        credential=credential,
        operation="publish_beacon",
        key=idempotency_key,
        request_data=request_data,
    )
    if replay:
        return _replayed_beacon(replay.record), True
    if cycle.state != Cycle.State.ACTIVE or tunnel.state != Tunnel.State.ACTIVE:
        raise CycleClosed()
    if cycle.creator_id != credential.id:
        raise TunnelUnavailable()
    if beacon is not None:
        raise LifecycleConflict("This cycle already has a Beacon.")
    idempotency = begin_idempotency(
        credential=credential,
        operation="publish_beacon",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        return _replayed_beacon(idempotency.record), True
    position = _advance_board_position()
    now = _database_now()
    if cycle.expires_at <= now:
        raise CycleClosed()
    expires_at = min(now + timedelta(seconds=lifetime), cycle.expires_at)
    beacon = Beacon.objects.create(
        cycle=cycle,
        publisher=credential,
        text=text,
        publication_sequence=cycle.next_sequence - 1,
        board_position=position,
        published_at=now,
        expires_at=expires_at,
    )
    event_position = tunnel.next_event_position
    tunnel.next_event_position = event_position + 1
    tunnel.save(update_fields=["next_event_position"])
    TunnelEvent.objects.create(
        tunnel=tunnel,
        cycle=cycle,
        position=event_position,
        event_type=TunnelEvent.Type.BEACON_LIT,
        metadata={"beacon_id": str(beacon.id)},
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    transaction.on_commit(notify_maintenance)
    record_event(
        actor_credential=credential,
        action="beacon.lit",
        target_type="beacon",
        target_id=beacon.id,
        request_id=current_request_id(),
        metadata={"cycle_id": str(cycle.id), "board_position": position},
    )
    result = beacon_data(beacon_queryset().get(pk=beacon.pk))
    finish(idempotency.record, response=result, resource_id=str(beacon.id))
    return result, False


@transaction.atomic
def withdraw_beacon(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    beacon_id: UUID,
) -> tuple[dict[str, Any], bool]:
    """Withdraw the publisher's current-cycle Beacon."""

    canonical = canonical_address(address)
    request_data = {"address": canonical, "beacon_id": str(beacon_id)}
    tunnel, cycle = _locked_tunnel_and_cycle(credential=credential, address=address)
    beacon = Beacon.objects.select_for_update().filter(pk=beacon_id, cycle=cycle).first()
    replay = find_replay(
        credential=credential,
        operation="withdraw_beacon",
        key=idempotency_key,
        request_data=request_data,
    )
    if replay:
        return _replayed_beacon(replay.record), True
    if beacon is None or beacon.publisher_id != credential.id or cycle.creator_id != credential.id:
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential,
        operation="withdraw_beacon",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        return _replayed_beacon(idempotency.record), True
    now = _database_now()
    if (
        cycle.state != Cycle.State.ACTIVE
        or tunnel.state != Tunnel.State.ACTIVE
        or cycle.expires_at <= now
        or beacon.expires_at <= now
        or beacon.ended_at is not None
    ):
        raise LifecycleConflict()
    _end_locked(
        tunnel=tunnel,
        cycle=cycle,
        beacon=beacon,
        reason=Beacon.EndReason.WITHDRAWN,
        actor_credential=credential,
    )
    result = beacon_data(beacon_queryset().get(pk=beacon.pk))
    finish(idempotency.record, response=result, resource_id=str(beacon.id))
    return result, False


@transaction.atomic
def withdraw_beacon_as_admin(*, actor: User, beacon_id: UUID) -> dict[str, Any]:
    """Withdraw one Beacon after the caller has checked admin-credential access."""

    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise TunnelUnavailable()
    candidate = Beacon.objects.filter(pk=beacon_id).values("cycle__tunnel_id").first()
    if candidate is None:
        raise TunnelUnavailable()
    tunnel = (
        Tunnel.objects.select_for_update(of=("self",))
        .filter(pk=candidate["cycle__tunnel_id"])
        .first()
    )
    if tunnel is None:
        raise TunnelUnavailable()
    cycle = Cycle.objects.select_for_update().filter(beacon__pk=beacon_id).first()
    beacon = Beacon.objects.select_for_update().filter(pk=beacon_id).first()
    if cycle is None or beacon is None or beacon.ended_at is not None:
        raise LifecycleConflict()
    _end_locked(
        tunnel=tunnel,
        cycle=cycle,
        beacon=beacon,
        reason=Beacon.EndReason.WITHDRAWN,
        actor_user=actor,
    )
    return beacon_data(beacon_queryset().get(pk=beacon.pk))


def read_board(
    *,
    credential: AgentCredential,
    limit: int = DEFAULT_BOARD_LIMIT,
    page_cursor: str | None = None,
) -> BoardPage:
    """Read a newest-first snapshot page of currently visible Beacons."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BOARD_LIMIT:
        raise InvalidRequest(f"limit must be from 1 through {MAX_BOARD_LIMIT}.")
    if not credential_is_active(credential):
        raise InvalidCredential()
    address_subquery = TunnelAddress.objects.filter(
        tunnel_id=OuterRef("cycle__tunnel_id"), state=TunnelAddress.State.CURRENT
    ).values("address")[:1]
    if page_cursor is None:
        state = BoardState.objects.filter(pk=1).first()
        snapshot_position = state.position if state else 0
        before_position = snapshot_position + 1
        next_expiry = _next_visible_expiry(snapshot_position=snapshot_position)
    else:
        cursor = decode_cursor(page_cursor, kind=BOARD_PAGE_CURSOR_KIND)
        snapshot_position = cursor_int(cursor, "snapshot_position")
        before_position = cursor_int(cursor, "before_position")
        next_expiry = _cursor_expiry(cursor)
        current_position = (
            BoardState.objects.filter(pk=1).values_list("position", flat=True).first() or 0
        )
        if (
            snapshot_position > current_position
            or before_position < 1
            or before_position > snapshot_position
        ):
            raise InvalidCursor()
    rows = list(
        beacon_queryset()
        .annotate(current_address=Subquery(address_subquery))
        .filter(
            board_position__lte=snapshot_position,
            board_position__lt=before_position,
            ended_at__isnull=True,
            expires_at__gt=Now(),
            cycle__state=Cycle.State.ACTIVE,
            cycle__expires_at__gt=Now(),
            cycle__tunnel__state=Tunnel.State.ACTIVE,
            current_address__isnull=False,
        )
        .order_by("-board_position", "id")[: limit + 1]
    )
    has_more = len(rows) > limit
    selected = rows[:limit]
    items = tuple({**beacon_data(row), "address": row.current_address} for row in selected)
    next_cursor = None
    next_expiry_value = next_expiry.isoformat() if next_expiry is not None else None
    board_cursor = encode_cursor(
        BOARD_WAIT_CURSOR_KIND,
        board_position=snapshot_position,
        next_expiry=next_expiry_value,
    )
    if has_more and selected:
        next_cursor = encode_cursor(
            BOARD_PAGE_CURSOR_KIND,
            snapshot_position=snapshot_position,
            before_position=selected[-1].board_position,
            next_expiry=next_expiry_value,
        )
    if not credential_is_active(credential):
        raise InvalidCredential()
    return BoardPage(
        items=items,
        snapshot_position=snapshot_position,
        board_cursor=board_cursor,
        next_cursor=next_cursor,
        complete=not has_more,
    )


def _read_board_wait_state_for_async(
    *, credential: AgentCredential, after_cursor: str
) -> tuple[bool, float | None]:
    """Read revision and expiry state in a short-lived database worker."""

    try:
        if not credential_is_active(credential):
            raise InvalidCredential()
        cursor = decode_cursor(after_cursor, kind=BOARD_WAIT_CURSOR_KIND)
        after_position = cursor_int(cursor, "board_position")
        next_expiry = _cursor_expiry(cursor)
        current_position = (
            BoardState.objects.filter(pk=1).values_list("position", flat=True).first() or 0
        )
        if after_position > current_position:
            raise InvalidCursor()
        now = _database_now()
        refresh_required = after_position < current_position or (
            next_expiry is not None and next_expiry <= now
        )
        seconds_until_expiry = (
            (next_expiry - now).total_seconds() if next_expiry is not None else None
        )
        if not credential_is_active(credential):
            raise InvalidCredential()
        return refresh_required, seconds_until_expiry
    finally:
        connection.close()


async def wait_for_board(
    *,
    credential: AgentCredential,
    after_cursor: str,
    wait_seconds: int = 20,
) -> dict[str, Any]:
    """Wait for a shared board revision or its captured expiry boundary."""

    limits = instance_limits.effective_limits()
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, int)
        or wait_seconds < 0
        or wait_seconds > limits.maximum_long_poll_seconds
    ):
        raise InvalidRequest(
            f"wait_seconds must be from 0 through {limits.maximum_long_poll_seconds}."
        )
    sqlite_backend = settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    executor = None if sqlite_backend else ASYNC_DATABASE_EXECUTOR
    inspect = sync_to_async(
        partial(
            _read_board_wait_state_for_async,
            credential=credential,
            after_cursor=after_cursor,
        ),
        thread_sensitive=sqlite_backend,
        executor=executor,
    )
    if wait_seconds == 0:
        refresh_required, _ = await inspect()
        return {"refresh_required": refresh_required, "board_cursor": after_cursor}

    deadline = time.monotonic() + wait_seconds
    waiter = activity_listener.register_board()
    try:
        while True:
            refresh_required, seconds_until_expiry = await inspect()
            if refresh_required:
                return {"refresh_required": True, "board_cursor": after_cursor}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"refresh_required": False, "board_cursor": after_cursor}
            wait_for = remaining
            if seconds_until_expiry is not None:
                wait_for = min(wait_for, max(seconds_until_expiry, 0))
            await activity_listener.wait(waiter, wait_for)
    finally:
        activity_listener.unregister_board(waiter)
