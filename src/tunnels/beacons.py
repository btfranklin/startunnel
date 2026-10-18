"""Domain operations for the instance-wide Beacon board."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Now
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from core.audit import record_event
from core.cursors import cursor_int, decode_cursor, encode_cursor
from core.logging import current_request_id
from core.notifications import ACTIVITY_CHANNEL, notify, notify_maintenance

from .access import credential_is_active, resolve_tunnel
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
from .models import Beacon, BoardState, Cycle, Message, Tunnel, TunnelAddress, TunnelEvent

DEFAULT_BEACON_SECONDS = 3_600
MAX_BEACON_SECONDS = 86_400
DEFAULT_BOARD_LIMIT = 20
MAX_BOARD_LIMIT = 100


@dataclass(frozen=True, slots=True)
class BoardPage:
    items: tuple[dict[str, Any], ...]
    snapshot_position: int
    next_cursor: str | None
    complete: bool


def _advance_board_position() -> int:
    """Lock and increment the single revision row."""

    state, _ = BoardState.objects.select_for_update().get_or_create(pk=1)
    state.position += 1
    state.save(update_fields=["position"])
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


def _validate_beacon_text(text: str) -> None:
    if not isinstance(text, str) or not 1 <= len(text) <= 280:
        raise InvalidRequest("Beacon text must contain 1 to 280 characters.")
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
        return replay.record.response, True
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
        return idempotency.record.response, True
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
        return replay.record.response, True
    if beacon is None or beacon.publisher_id != credential.id or cycle.creator_id != credential.id:
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential,
        operation="withdraw_beacon",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        return idempotency.record.response, True
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
def withdraw_beacon_as_operator(*, actor: User, beacon_id: UUID) -> dict[str, Any]:
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
    else:
        cursor = decode_cursor(page_cursor, kind="beacon_board")
        snapshot_position = cursor_int(cursor, "snapshot_position")
        before_position = cursor_int(cursor, "before_position")
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
    if has_more and selected:
        next_cursor = encode_cursor(
            "beacon_board",
            snapshot_position=snapshot_position,
            before_position=selected[-1].board_position,
        )
    if not credential_is_active(credential):
        raise InvalidCredential()
    return BoardPage(
        items=items,
        snapshot_position=snapshot_position,
        next_cursor=next_cursor,
        complete=not has_more,
    )
