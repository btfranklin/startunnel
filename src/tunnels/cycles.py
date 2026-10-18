"""Tunnel creation, cycle lifecycle, and retained cycle status."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from core.audit import record_event
from core.limits import Limits
from core.logging import current_request_id
from core.metrics import TUNNELS_CREATED
from core.notifications import ACTIVITY_CHANNEL, notify, notify_maintenance

from .access import (
    active_cycle,
    credential_is_active,
    cycle_is_readable,
    limits_for,
    resolve_tunnel,
    select_cycle,
)
from .codec import (
    canonical_address,
    display_address,
    encode_token,
)
from .errors import (
    CycleUnavailable,
    InvalidCredential,
    InvalidRequest,
    LifecycleConflict,
    QuotaExceeded,
    TunnelUnavailable,
)
from .idempotency import begin as begin_idempotency
from .idempotency import find_replay as find_idempotency_replay
from .idempotency import finish as finish_idempotency
from .messages import (
    _add_session_request_data,
    _create_message,
    _normalize_correlation_id,
    _resolve_mentions,
    _store_mentions,
    _validate_mention_ids,
    _validate_session,
)
from .models import (
    Cycle,
    IdempotencyRecord,
    Message,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)
from .payloads import ValidatedPayload, validate_content, validate_storable_text

DEFAULT_CYCLE_SECONDS = 3_600
MAX_CYCLE_SECONDS = 604_800
MIN_CYCLE_SECONDS = 300
ADDRESS_ALLOCATION_ATTEMPTS = 8
INSTANCE_TUNNEL_QUOTA_LOCK = 7_195_415_308_902


@dataclass(frozen=True, slots=True)
class CreatedTunnel:
    tunnel: Tunnel
    address: str
    display_address: str
    cycle: Cycle
    root: Message
    tunnel_state: str
    cycle_state: str
    message_count: int
    content_bytes: int
    event_position: int
    replay: bool = False


@dataclass(frozen=True, slots=True)
class AgentTunnelStatus:
    tunnels: int


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    tunnel: Tunnel
    current_address: TunnelAddress
    current_cycle: Cycle | None


@dataclass(frozen=True, slots=True)
class CyclePage:
    tunnel: Tunnel
    cycles: tuple[Cycle, ...]
    snapshot_number: int
    next_before_number: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class StartedCycle:
    tunnel: Tunnel
    cycle: Cycle
    root: Message
    cycle_state: str
    message_count: int
    content_bytes: int
    event_position: int
    replay: bool = False


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    cycle_state: str
    message_count: int
    content_bytes: int
    event_position: int


def _read_cycle_snapshot(response: Mapping[str, Any]) -> CycleSnapshot:
    cycle_state = response.get("cycle_state")
    message_count = response.get("message_count")
    content_bytes = response.get("content_bytes")
    event_position = response.get("event_position")
    if (
        cycle_state not in Cycle.State.values
        or not isinstance(message_count, int)
        or isinstance(message_count, bool)
        or message_count < 1
        or not isinstance(content_bytes, int)
        or isinstance(content_bytes, bool)
        or content_bytes < 0
        or not isinstance(event_position, int)
        or isinstance(event_position, bool)
        or event_position < 1
    ):
        raise TunnelUnavailable()
    return CycleSnapshot(
        cycle_state=cycle_state,
        message_count=message_count,
        content_bytes=content_bytes,
        event_position=event_position,
    )


def _lock_instance_tunnel_quota() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [INSTANCE_TUNNEL_QUOTA_LOCK])


def _cycle_lifetime(expires_in_seconds: int | None) -> int:
    value = DEFAULT_CYCLE_SECONDS if expires_in_seconds is None else expires_in_seconds
    if value < MIN_CYCLE_SECONDS or value > MAX_CYCLE_SECONDS:
        raise InvalidRequest(
            f"expires_in_seconds must be from {MIN_CYCLE_SECONDS} through {MAX_CYCLE_SECONDS}."
        )
    return value


def agent_tunnel_status(*, credential: AgentCredential) -> AgentTunnelStatus:
    if not credential_is_active(credential):
        raise InvalidCredential()
    return AgentTunnelStatus(
        tunnels=Tunnel.objects.exclude(state=Tunnel.State.RETIRED).count(),
    )


def _validate_label(value: str, *, field: str) -> str:
    normalized = value.strip()
    validate_storable_text(normalized, field=field)
    if len(normalized) > 160:
        raise InvalidRequest(f"{field} can contain at most 160 characters.")
    return normalized


def _allocate_random_address(*, tunnel: Tunnel, generation: int) -> tuple[TunnelAddress, str]:
    """Allocate one random current address inside the locked tunnel transaction."""

    for _attempt in range(ADDRESS_ALLOCATION_ATTEMPTS):
        address = encode_token(secrets.token_bytes(16))
        try:
            with transaction.atomic():
                row = TunnelAddress.objects.create(
                    tunnel=tunnel,
                    address=address,
                    generation=generation,
                )
        except IntegrityError:
            continue
        return row, address
    raise InvalidRequest("StarTunnel could not allocate an address. Try the request again.")


@transaction.atomic
def create_tunnel(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    root_content: dict[str, Any],
    label: str = "",
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
    root_session: str | None = None,
) -> CreatedTunnel:
    if not credential_is_active(credential):
        raise InvalidCredential()
    seconds = _cycle_lifetime(expires_in_seconds)
    tunnel_label = _validate_label(label, field="A tunnel label")
    normalized_cycle_label = _validate_label(cycle_label, field="A cycle label")
    limits = limits_for(credential)
    root_payload = validate_content(root_content, maximum_bytes=limits.bytes_per_message)
    correlation = _normalize_correlation_id(root_correlation_id)
    session = _validate_session(root_session)
    mention_ids = root_mentions or []
    request_data = {
        "label": tunnel_label,
        "cycle_label": normalized_cycle_label,
        "expires_in_seconds": seconds,
        "root_content": root_content,
        "root_mentions": [str(item) for item in mention_ids],
        "root_correlation_id": correlation,
    }
    _add_session_request_data(request_data, session)
    operation = "create_tunnel"
    idempotency = begin_idempotency(
        credential=credential,
        operation=operation,
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        if not idempotency.record.resource_id:
            raise TunnelUnavailable()
        tunnel = Tunnel.objects.get(pk=idempotency.record.resource_id)
        cycle = tunnel.cycles.get(number=1)
        if not cycle_is_readable(cycle):
            raise CycleUnavailable()
        root = cycle.root_message
        if root is None:
            raise TunnelUnavailable()
        address_row = TunnelAddress.objects.filter(
            tunnel=tunnel,
            generation=1,
            state=TunnelAddress.State.CURRENT,
            tunnel__state__in=[Tunnel.State.ACTIVE, Tunnel.State.DORMANT],
        ).first()
        if address_row is None:
            raise TunnelUnavailable()
        address = address_row.address
        response = idempotency.record.response
        tunnel_state = response.get("tunnel_state")
        if tunnel_state not in Tunnel.State.values:
            raise TunnelUnavailable()
        snapshot = _read_cycle_snapshot(response)
        return CreatedTunnel(
            tunnel=tunnel,
            address=address,
            display_address=display_address(address),
            cycle=cycle,
            root=root,
            tunnel_state=tunnel_state,
            cycle_state=snapshot.cycle_state,
            message_count=snapshot.message_count,
            content_bytes=snapshot.content_bytes,
            event_position=snapshot.event_position,
            replay=True,
        )
    _lock_instance_tunnel_quota()
    owned = Tunnel.objects.exclude(state=Tunnel.State.RETIRED)
    maximum = limits.non_retired_tunnels
    if owned.count() >= maximum:
        raise QuotaExceeded()
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits.mentions_per_message,
    )

    now = timezone.now()
    tunnel = Tunnel.objects.create(
        creator=credential,
        label=tunnel_label,
        state=Tunnel.State.ACTIVE,
        next_cycle_number=2,
        next_event_position=3,
    )
    _address_row, address = _allocate_random_address(tunnel=tunnel, generation=1)
    opened = _open_cycle(
        tunnel=tunnel,
        credential=credential,
        number=1,
        start_position=1,
        label=normalized_cycle_label,
        expires_at=now + timedelta(seconds=seconds),
        payload=root_payload,
        correlation=correlation,
        mentions=mentions,
        session=session,
    )
    cycle, root = opened.cycle, opened.root
    finish_idempotency(
        idempotency.record,
        response={
            "tunnel_id": str(tunnel.id),
            "cycle_id": str(cycle.id),
            "root_message_id": str(root.id),
            "created_at": tunnel.created_at.isoformat(),
            "tunnel_state": Tunnel.State.ACTIVE,
            "cycle_state": Cycle.State.ACTIVE,
            "message_count": 1,
            "content_bytes": root.byte_count,
            "event_position": 2,
        },
        resource_id=str(tunnel.id),
    )
    record_event(
        actor_credential=credential,
        action="tunnel.created",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"cycle_id": str(cycle.id)},
    )
    TUNNELS_CREATED.inc()
    return CreatedTunnel(
        tunnel=tunnel,
        address=address,
        display_address=display_address(address),
        cycle=cycle,
        root=root,
        tunnel_state=Tunnel.State.ACTIVE,
        cycle_state=Cycle.State.ACTIVE,
        message_count=1,
        content_bytes=root.byte_count,
        event_position=2,
    )


def _can_control_with_credential(*, credential: AgentCredential, tunnel: Tunnel) -> bool:
    return tunnel.creator_id == credential.id


def _current_address(*, tunnel: Tunnel, for_update: bool = False) -> TunnelAddress:
    query = TunnelAddress.objects.filter(tunnel=tunnel, state=TunnelAddress.State.CURRENT)
    if for_update:
        query = query.select_for_update()
    row = query.first()
    if row is None:
        raise TunnelUnavailable()
    return row


def get_tunnel_status(*, credential: AgentCredential, address: str) -> TunnelStatus:
    tunnel = resolve_tunnel(credential=credential, address=address)
    current_address = _current_address(tunnel=tunnel)
    current_cycle = (
        Cycle.objects.select_related("root_message")
        .filter(tunnel=tunnel, state=Cycle.State.ACTIVE)
        .first()
    )
    return TunnelStatus(tunnel=tunnel, current_address=current_address, current_cycle=current_cycle)


def list_cycles(
    *,
    credential: AgentCredential,
    address: str,
    limit: int = 50,
    before_number: int | None = None,
    snapshot_number: int | None = None,
) -> CyclePage:
    limits = limits_for(credential)
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    current_high_water = tunnel.next_cycle_number - 1
    high_water = current_high_water if snapshot_number is None else snapshot_number
    if high_water < 1 or high_water > current_high_water:
        raise InvalidRequest("The cycle snapshot is not valid for this tunnel.")
    if before_number is not None and (before_number < 1 or before_number > high_water + 1):
        raise InvalidRequest("The cycle page position is not valid for this tunnel.")
    now = timezone.now()
    query = (
        Cycle.objects.select_related("root_message", "creator")
        .filter(tunnel=tunnel, number__lte=high_water)
        .filter(
            Q(state=Cycle.State.ACTIVE)
            | Q(state=Cycle.State.CLOSED, delete_after__isnull=True)
            | Q(state=Cycle.State.CLOSED, delete_after__gt=now)
        )
        .order_by("-number", "id")
    )
    if before_number is not None:
        query = query.filter(number__lt=before_number)
    rows = [cycle for cycle in query[: limit + 2] if cycle_is_readable(cycle, now=now)]
    has_more = len(rows) > limit
    page = rows[:limit]
    return CyclePage(
        tunnel=tunnel,
        cycles=tuple(page),
        snapshot_number=high_water,
        next_before_number=page[-1].number if has_more and page else None,
        complete=not has_more,
    )


def _prepare_cycle_root(
    *,
    limits: Limits,
    root_content: dict[str, Any],
    cycle_label: str,
    expires_in_seconds: int | None,
    root_mentions: list[UUID] | None,
    root_correlation_id: str | None,
) -> tuple[int, str, ValidatedPayload, str, list[UUID]]:
    seconds = _cycle_lifetime(expires_in_seconds)
    label = _validate_label(cycle_label, field="A cycle label")
    payload = validate_content(root_content, maximum_bytes=limits.bytes_per_message)
    correlation = _normalize_correlation_id(root_correlation_id)
    mention_ids = root_mentions or []
    _validate_mention_ids(mention_ids, maximum=limits.mentions_per_message)
    return seconds, label, payload, correlation, mention_ids


def _open_cycle(
    *,
    tunnel: Tunnel,
    credential: AgentCredential,
    number: int,
    start_position: int,
    expires_at: datetime,
    label: str,
    payload: ValidatedPayload,
    correlation: str,
    mentions: list[AgentCredential],
    session: str | None = None,
) -> StartedCycle:
    message_position = start_position + 1
    cycle = Cycle.objects.create(
        tunnel=tunnel,
        creator=credential,
        number=number,
        label=label,
        state=Cycle.State.ACTIVE,
        next_sequence=2,
        expires_at=expires_at,
        retention_seconds=settings.STARTUNNEL_HISTORY_RETENTION_SECONDS,
    )
    transaction.on_commit(notify_maintenance)
    root = _create_message(
        tunnel=tunnel,
        cycle=cycle,
        sender=credential,
        parent=None,
        sequence=1,
        payload=payload,
        correlation_id=correlation,
        session=session,
    )
    _store_mentions(root, mentions)
    cycle.root_message = root
    cycle.message_count = 1
    cycle.content_bytes = root.byte_count
    cycle.save(update_fields=["root_message", "message_count", "content_bytes"])
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=start_position,
                event_type=TunnelEvent.Type.CYCLE_STARTED,
            ),
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                message=root,
                position=message_position,
                event_type=TunnelEvent.Type.MESSAGE_POSTED,
            ),
        ]
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    return StartedCycle(
        tunnel=tunnel,
        cycle=cycle,
        root=root,
        cycle_state=Cycle.State.ACTIVE,
        message_count=1,
        content_bytes=root.byte_count,
        event_position=message_position,
    )


def _start_cycle_locked(
    *,
    tunnel: Tunnel,
    credential: AgentCredential,
    seconds: int,
    label: str,
    payload: ValidatedPayload,
    correlation: str,
    mentions: list[AgentCredential],
    session: str | None = None,
    actor_user: User | None = None,
) -> StartedCycle:
    if (
        tunnel.state != Tunnel.State.DORMANT
        or Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).exists()
    ):
        raise LifecycleConflict()
    now = timezone.now()
    number = tunnel.next_cycle_number
    start_position = tunnel.next_event_position
    tunnel.state = Tunnel.State.ACTIVE
    tunnel.dormant_at = None
    tunnel.next_cycle_number = number + 1
    tunnel.next_event_position = start_position + 2
    tunnel.save(update_fields=["state", "dormant_at", "next_cycle_number", "next_event_position"])
    opened = _open_cycle(
        tunnel=tunnel,
        credential=credential,
        number=number,
        start_position=start_position,
        expires_at=now + timedelta(seconds=seconds),
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=mentions,
        session=session,
    )
    record_event(
        actor_credential=credential if actor_user is None else None,
        actor_user=actor_user,
        action="cycle.started",
        target_type="cycle",
        target_id=opened.cycle.id,
        request_id=current_request_id(),
        metadata={"number": number},
    )
    return opened


def _replayed_cycle(
    record: IdempotencyRecord, *, credential: AgentCredential, address: str
) -> StartedCycle:
    tunnel = resolve_tunnel(credential=credential, address=address)
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    if record.resource_id is None:
        raise TunnelUnavailable()
    cycle = (
        Cycle.objects.select_related("root_message")
        .filter(pk=record.resource_id, tunnel=tunnel)
        .first()
    )
    if cycle is None or not cycle_is_readable(cycle):
        raise CycleUnavailable()
    snapshot = _read_cycle_snapshot(record.response)
    if cycle.root_message is None:
        raise TunnelUnavailable()
    return StartedCycle(
        tunnel=tunnel,
        cycle=cycle,
        root=cycle.root_message,
        cycle_state=snapshot.cycle_state,
        message_count=snapshot.message_count,
        content_bytes=snapshot.content_bytes,
        event_position=snapshot.event_position,
        replay=True,
    )


def _cycle_request_data(
    *,
    address: str,
    expected_address_generation: int,
    expected_cycle_id: UUID | None,
    cycle_label: str,
    expires_in_seconds: int,
    root_content: dict[str, Any],
    root_mentions: list[UUID],
    root_correlation_id: str,
) -> dict[str, Any]:
    return {
        "address": canonical_address(address),
        "expected_address_generation": expected_address_generation,
        "expected_cycle_id": str(expected_cycle_id) if expected_cycle_id else None,
        "cycle_label": cycle_label,
        "expires_in_seconds": expires_in_seconds,
        "root_content": root_content,
        "root_mentions": [str(value) for value in root_mentions],
        "root_correlation_id": root_correlation_id,
    }


@transaction.atomic
def start_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
    root_session: str | None = None,
) -> StartedCycle:
    limits = limits_for(credential)
    seconds, label, payload, correlation, mention_ids = _prepare_cycle_root(
        limits=limits,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=root_mentions,
        root_correlation_id=root_correlation_id,
    )
    session = _validate_session(root_session)
    request_data = _cycle_request_data(
        address=address,
        expected_address_generation=expected_address_generation,
        expected_cycle_id=None,
        cycle_label=label,
        expires_in_seconds=seconds,
        root_content=root_content,
        root_mentions=root_mentions or [],
        root_correlation_id=correlation,
    )
    _add_session_request_data(request_data, session)
    operation = "start_cycle"
    replay = find_idempotency_replay(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if replay:
        return _replayed_cycle(replay.record, credential=credential, address=address)
    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits.mentions_per_message,
    )
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    idempotency = begin_idempotency(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if idempotency.replay:
        return _replayed_cycle(idempotency.record, credential=credential, address=address)
    started = _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=mentions,
        session=session,
    )
    finish_idempotency(
        idempotency.record,
        response={
            "cycle_id": str(started.cycle.id),
            "cycle_state": started.cycle_state,
            "message_count": started.message_count,
            "content_bytes": started.content_bytes,
            "event_position": started.event_position,
            "started": True,
        },
        resource_id=str(started.cycle.id),
    )
    return started


def _close_for_rollover_locked(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    actor_credential: AgentCredential,
    actor_user: User | None = None,
) -> None:
    if cycle.state != Cycle.State.ACTIVE or tunnel.state != Tunnel.State.ACTIVE:
        raise LifecycleConflict()
    _close_locked_cycle(
        tunnel=tunnel,
        cycle=cycle,
        reason="rollover",
        actor_credential=actor_credential if actor_user is None else None,
        actor_user=actor_user,
    )


@transaction.atomic
def rollover_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_cycle_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
    root_session: str | None = None,
) -> StartedCycle:
    limits = limits_for(credential)
    seconds, label, payload, correlation, mention_ids = _prepare_cycle_root(
        limits=limits,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=root_mentions,
        root_correlation_id=root_correlation_id,
    )
    session = _validate_session(root_session)
    request_data = _cycle_request_data(
        address=address,
        expected_address_generation=expected_address_generation,
        expected_cycle_id=expected_cycle_id,
        cycle_label=label,
        expires_in_seconds=seconds,
        root_content=root_content,
        root_mentions=root_mentions or [],
        root_correlation_id=correlation,
    )
    _add_session_request_data(request_data, session)
    operation = "rollover_cycle"
    replay = find_idempotency_replay(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if replay:
        return _replayed_cycle(replay.record, credential=credential, address=address)
    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if idempotency.replay:
        return _replayed_cycle(idempotency.record, credential=credential, address=address)
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits.mentions_per_message,
    )
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    cycle = active_cycle(tunnel=tunnel, for_update=True)
    if cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_for_rollover_locked(
        tunnel=tunnel,
        cycle=cycle,
        actor_credential=credential,
    )
    started = _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=mentions,
        session=session,
    )
    finish_idempotency(
        idempotency.record,
        response={
            "closed_cycle_id": str(cycle.id),
            "cycle_id": str(started.cycle.id),
            "cycle_state": started.cycle_state,
            "message_count": started.message_count,
            "content_bytes": started.content_bytes,
            "event_position": started.event_position,
            "started": True,
        },
        resource_id=str(started.cycle.id),
    )
    return started


def _close_locked_cycle(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    reason: str,
    actor_credential: AgentCredential | None = None,
    actor_user: User | None = None,
) -> None:
    if cycle.state != Cycle.State.ACTIVE or tunnel.state != Tunnel.State.ACTIVE:
        return
    now = timezone.now()
    if cycle.expires_at <= now:
        reason = "expired"
    closed_at = cycle.expires_at if reason == "expired" else now
    retention_seconds = (
        cycle.retention_seconds
        if reason == "expired"
        else settings.STARTUNNEL_HISTORY_RETENTION_SECONDS
    )
    close_position = tunnel.next_event_position
    dormant_position = close_position + 1
    cycle.state = Cycle.State.CLOSED
    cycle.closed_at = closed_at
    cycle.retention_seconds = retention_seconds
    cycle.delete_after = (
        closed_at + timedelta(seconds=retention_seconds) if retention_seconds is not None else None
    )
    cycle.close_reason = reason
    cycle.final_sequence = cycle.next_sequence - 1
    cycle.save(
        update_fields=[
            "state",
            "closed_at",
            "retention_seconds",
            "delete_after",
            "close_reason",
            "final_sequence",
        ]
    )
    if reason != "expired":
        transaction.on_commit(notify_maintenance)
    tunnel.state = Tunnel.State.DORMANT
    tunnel.dormant_at = now
    tunnel.next_event_position = dormant_position + 1
    tunnel.save(update_fields=["state", "dormant_at", "next_event_position"])
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=close_position,
                event_type=TunnelEvent.Type.CYCLE_CLOSED,
                metadata={"reason": reason},
            ),
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=dormant_position,
                event_type=TunnelEvent.Type.TUNNEL_DORMANT,
            ),
        ]
    )
    from .beacons import end_beacon_for_cycle_locked

    end_beacon_for_cycle_locked(
        tunnel=tunnel,
        cycle=cycle,
        reason="expired" if reason == "expired" else "cycle_closed",
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    record_event(
        actor_credential=actor_credential,
        actor_user=actor_user,
        action="cycle.closed",
        target_type="cycle",
        target_id=cycle.id,
        request_id=current_request_id(),
        metadata={"reason": reason},
    )


@transaction.atomic
def close_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_cycle_id: UUID,
) -> None:
    canonical = canonical_address(address)
    request_data = {
        "address": canonical,
        "expected_cycle_id": str(expected_cycle_id),
    }
    replay = find_idempotency_replay(
        credential=credential,
        operation="close_cycle",
        key=idempotency_key,
        request_data=request_data,
    )
    if replay:
        tunnel = resolve_tunnel(credential=credential, address=address)
        if not _can_control_with_credential(credential=credential, tunnel=tunnel):
            raise TunnelUnavailable()
        select_cycle(tunnel=tunnel, cycle_id=expected_cycle_id)
        return
    tunnel = resolve_tunnel(
        credential=credential,
        address=address,
        for_update=True,
    )
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential,
        operation="close_cycle",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        select_cycle(tunnel=tunnel, cycle_id=expected_cycle_id)
        return
    cycle = active_cycle(tunnel=tunnel, for_update=True)
    if cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_locked_cycle(tunnel=tunnel, cycle=cycle, reason="creator", actor_credential=credential)
    finish_idempotency(
        idempotency.record,
        response={"cycle_id": str(cycle.id), "closed": True},
        resource_id=str(cycle.id),
    )
