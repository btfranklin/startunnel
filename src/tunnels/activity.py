"""Chronological tunnel activity and per-agent progress checkpoints."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from typing import cast
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Prefetch, QuerySet, prefetch_related_objects

from agents.models import AgentCredential
from core.activity_listener import activity_listener

from .access import (
    active_credential_query,
    address_query,
    cycle_is_readable,
    limits_for,
    resolve_tunnel,
)
from .codec import address_digest, parse_address
from .errors import CheckpointRegression, InvalidRequest
from .models import ActivityCheckpoint, MessageMention, Tunnel, TunnelEvent

# Keep one pool slot available for the request path in each process. Activity
# reads can use the remaining slots, up to the explicit executor cap.
_ASYNC_DATABASE_WORKERS = max(min(settings.DATABASE_POOL_MAX_SIZE - 1, 16), 1)
_ASYNC_DATABASE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_ASYNC_DATABASE_WORKERS,
    thread_name_prefix="startunnel-activity-db",
)


@dataclass(frozen=True, slots=True)
class ActivityPage:
    tunnel: Tunnel
    events: tuple[TunnelEvent, ...]
    start_position: int
    next_position: int
    high_water_position: int
    has_more: bool
    authorization_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class _ActivityStart:
    tunnel: Tunnel
    position: int
    high_water_position: int


def _activity_rows(query: QuerySet[TunnelEvent], *, limit: int) -> list[TunnelEvent]:
    """Load activity and fetch mentions only when at least one row needs them."""

    rows = cast(
        list[TunnelEvent],
        list(
            query.annotate(
                message_has_mentions=Exists(
                    MessageMention.objects.filter(message_id=OuterRef("message_id"))
                )
            ).order_by("position", "id")[: limit + 1]
        ),
    )
    for event in rows:
        if event.cycle is not None and not cycle_is_readable(event.cycle):
            event.message = None
            event.cycle = None
    messages = [event.message for event in rows if event.message is not None]
    if messages and any(getattr(event, "message_has_mentions", False) for event in rows):
        prefetch_related_objects(
            messages,
            Prefetch(
                "mentions",
                queryset=MessageMention.objects.select_related("credential"),
            ),
        )
    else:
        for message in messages:
            message._prefetched_objects_cache = {"mentions": []}  # type: ignore[attr-defined]
    return rows


def read_activity_once(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None = None,
    limit: int = 100,
    event_types: tuple[str, ...] | None = None,
) -> ActivityPage:
    """Read one stable page of tunnel events after a durable position."""

    limits = limits_for(credential)
    if after_position is not None and after_position < 0:
        raise InvalidRequest("The activity position cannot be negative.")
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    if after_position is None:
        after_position = (
            ActivityCheckpoint.objects.filter(tunnel=tunnel, credential=credential)
            .values_list("last_event_position", flat=True)
            .first()
            or 0
        )
    high_water = max(tunnel.next_event_position - 1, 0)
    if after_position > high_water:
        raise InvalidRequest("The activity position is not valid for this tunnel.")
    query = TunnelEvent.objects.select_related(
        "cycle", "message", "message__cycle", "message__sender"
    ).filter(tunnel=tunnel, position__gt=after_position, position__lte=high_water)
    if event_types is not None:
        invalid_types = set(event_types) - set(TunnelEvent.Type.values)
        if invalid_types:
            raise InvalidRequest("One activity event type is not valid.")
        query = query.filter(event_type__in=event_types)
    rows = _activity_rows(query, limit=limit)
    has_more = len(rows) > limit
    page = tuple(rows[:limit])
    confirmed = resolve_tunnel(credential=credential, address=address)
    if confirmed.id != tunnel.id:
        raise InvalidRequest("The tunnel changed during the activity read.")
    return ActivityPage(
        tunnel=tunnel,
        events=page,
        start_position=after_position,
        next_position=(page[-1].position if has_more and page else high_water),
        high_water_position=high_water,
        has_more=has_more,
    )


def read_activity(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None = None,
    wait_seconds: int = 0,
    limit: int = 100,
    event_types: tuple[str, ...] | None = None,
) -> ActivityPage:
    """Read activity with a bounded database fallback for synchronous callers."""

    limits = limits_for(credential)
    if wait_seconds < 0 or wait_seconds > limits.maximum_long_poll_seconds:
        raise InvalidRequest(
            f"wait_seconds must be from 0 through {limits.maximum_long_poll_seconds}."
        )
    immediate = read_activity_once(
        credential=credential,
        address=address,
        after_position=after_position,
        limit=limit,
        event_types=event_types,
    )
    if immediate.events or immediate.next_position > immediate.start_position or wait_seconds == 0:
        return immediate

    deadline = time.monotonic() + wait_seconds
    page = immediate
    while not page.events and page.next_position == page.start_position:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection.close()
        time.sleep(min(remaining, 1.0))
        page = read_activity_once(
            credential=credential,
            address=address,
            after_position=immediate.start_position,
            limit=limit,
            event_types=event_types,
        )
    connection.close()
    return page


def _read_activity_once_for_async(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None,
    limit: int,
    event_types: tuple[str, ...] | None,
) -> ActivityPage:
    """Resolve and read one activity page in a database worker."""

    try:
        start = _resolve_activity_start(
            credential=credential,
            address=address,
            after_position=after_position,
            limit=limit,
            event_types=event_types,
        )
        return _read_activity_for_known_tunnel(
            tunnel=start.tunnel,
            after_position=start.position,
            limit=limit,
            event_types=event_types,
            initial_high_water=start.high_water_position,
        )
    finally:
        connection.close()


def _resolve_activity_start(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None,
    limit: int,
    event_types: tuple[str, ...] | None,
) -> _ActivityStart:
    limits = limits_for(credential)
    if after_position is not None and after_position < 0:
        raise InvalidRequest("The activity position cannot be negative.")
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if event_types is not None and set(event_types) - set(TunnelEvent.Type.values):
        raise InvalidRequest("One activity event type is not valid.")
    tunnel = resolve_tunnel(
        credential=credential,
        address=address,
    )
    if after_position is None:
        after_position = (
            ActivityCheckpoint.objects.filter(tunnel=tunnel, credential=credential)
            .values_list("last_event_position", flat=True)
            .first()
            or 0
        )
    high_water = max(tunnel.next_event_position - 1, 0)
    if after_position > high_water:
        raise InvalidRequest("The activity position is not valid for this tunnel.")
    return _ActivityStart(
        tunnel=tunnel,
        position=after_position,
        high_water_position=high_water,
    )


def _begin_activity_wait_for_async(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None,
    limit: int,
    event_types: tuple[str, ...] | None,
) -> _ActivityStart:
    """Resolve and validate one wait before it registers for a database notice."""

    try:
        return _resolve_activity_start(
            credential=credential,
            address=address,
            after_position=after_position,
            limit=limit,
            event_types=event_types,
        )
    finally:
        connection.close()


def _read_activity_for_known_tunnel(
    *,
    tunnel: Tunnel,
    after_position: int,
    limit: int,
    event_types: tuple[str, ...] | None,
    initial_high_water: int | None,
    credential: AgentCredential | None = None,
    address: str | None = None,
) -> ActivityPage:
    """Read events without resolving the same address again during a wait."""

    high_water = initial_high_water
    if high_water is None and event_types is not None:
        next_position = (
            Tunnel.objects.filter(pk=tunnel.pk)
            .values_list("next_event_position", flat=True)
            .first()
        )
        if next_position is None:
            raise InvalidRequest("The tunnel changed during the activity read.")
        high_water = max(next_position - 1, 0)
        if after_position > high_water:
            raise InvalidRequest("The activity position is not valid for this tunnel.")

    query = TunnelEvent.objects.select_related(
        "cycle", "message", "message__cycle", "message__sender"
    ).filter(tunnel=tunnel, position__gt=after_position)
    if high_water is not None:
        query = query.filter(position__lte=high_water)
    if event_types is not None:
        query = query.filter(event_type__in=event_types)
    authorization_checked = False
    if credential is not None and address is not None:
        authorization_checked = True
        digest = address_digest(parse_address(address))
        query = (
            query.annotate(
                credential_is_active=Exists(active_credential_query(credential=credential)),
                address_is_current=Exists(
                    address_query(digest=digest).filter(tunnel_id=OuterRef("tunnel_id"))
                ),
            )
            .filter(
                credential_is_active=True,
                address_is_current=True,
            )
            .exclude(tunnel__state=Tunnel.State.RETIRED)
        )
    rows = _activity_rows(query, limit=limit)
    if high_water is None:
        high_water = rows[-1].position if rows else after_position
    has_more = len(rows) > limit
    page = tuple(rows[:limit])
    return ActivityPage(
        tunnel=tunnel,
        events=page,
        start_position=after_position,
        next_position=(page[-1].position if has_more and page else high_water),
        high_water_position=high_water,
        has_more=has_more,
        authorization_confirmed=authorization_checked and bool(rows),
    )


def _continue_activity_wait_for_async(
    *,
    tunnel: Tunnel,
    credential: AgentCredential,
    address: str,
    after_position: int,
    limit: int,
    event_types: tuple[str, ...] | None,
) -> ActivityPage:
    """Read one wait continuation and return its connection to the pool."""

    try:
        return _read_activity_for_known_tunnel(
            tunnel=tunnel,
            after_position=after_position,
            limit=limit,
            event_types=event_types,
            initial_high_water=None,
            credential=credential,
            address=address,
        )
    finally:
        connection.close()


def _confirm_activity_page_for_async(
    *,
    page: ActivityPage,
    credential: AgentCredential,
    address: str,
) -> ActivityPage:
    """Recheck current authorization and address ownership before a response."""

    try:
        confirmed = resolve_tunnel(
            credential=credential,
            address=address,
        )
        if confirmed.id != page.tunnel.id:
            raise InvalidRequest("The tunnel changed during the activity read.")
        return replace(page, tunnel=confirmed)
    finally:
        connection.close()


async def read_activity_async(
    *,
    credential: AgentCredential,
    address: str,
    after_position: int | None = None,
    wait_seconds: int = 0,
    limit: int = 100,
    event_types: tuple[str, ...] | None = None,
) -> ActivityPage:
    """Read activity without holding a synchronous worker during the wait."""

    sqlite_backend = settings.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    limits = await sync_to_async(
        limits_for,
        thread_sensitive=sqlite_backend,
    )(credential)
    if wait_seconds < 0 or wait_seconds > limits.maximum_long_poll_seconds:
        raise InvalidRequest(
            f"wait_seconds must be from 0 through {limits.maximum_long_poll_seconds}."
        )
    executor = None if sqlite_backend else _ASYNC_DATABASE_EXECUTOR
    if wait_seconds == 0:
        immediate = await sync_to_async(
            partial(
                _read_activity_once_for_async,
                credential=credential,
                address=address,
                after_position=after_position,
                limit=limit,
                event_types=event_types,
            ),
            thread_sensitive=sqlite_backend,
            executor=executor,
        )()
        return await sync_to_async(
            partial(
                _confirm_activity_page_for_async,
                credential=credential,
                address=address,
            ),
            thread_sensitive=sqlite_backend,
            executor=executor,
        )(page=immediate)

    start = await sync_to_async(
        partial(
            _begin_activity_wait_for_async,
            credential=credential,
            address=address,
            after_position=after_position,
            limit=limit,
            event_types=event_types,
        ),
        thread_sensitive=sqlite_backend,
        executor=executor,
    )()
    continuation_read = partial(
        _continue_activity_wait_for_async,
        tunnel=start.tunnel,
        credential=credential,
        address=address,
        after_position=start.position,
        limit=limit,
        event_types=event_types,
    )
    read_in_worker = sync_to_async(
        continuation_read,
        thread_sensitive=sqlite_backend,
        executor=executor,
    )
    confirm_in_worker = sync_to_async(
        partial(
            _confirm_activity_page_for_async,
            credential=credential,
            address=address,
        ),
        thread_sensitive=sqlite_backend,
        executor=executor,
    )

    deadline = time.monotonic() + wait_seconds
    waiter = activity_listener.register(start.tunnel.id)
    try:
        page = await read_in_worker()
        while not page.events and page.next_position == page.start_position:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await activity_listener.wait(waiter, remaining)
            page = await read_in_worker()
        if page.events or page.next_position > page.start_position:
            if page.authorization_confirmed:
                return page
            return await confirm_in_worker(page=page)
        page = await read_in_worker()
        return await confirm_in_worker(page=page)
    finally:
        activity_listener.unregister(start.tunnel.id, waiter)


@transaction.atomic
def commit_checkpoint(
    *,
    credential: AgentCredential,
    address: str,
    position: int,
    expected_tunnel_id: UUID | None = None,
) -> tuple[ActivityCheckpoint, bool]:
    """Move one credential's tunnel checkpoint forward or accept an exact retry."""

    if position < 0:
        raise InvalidRequest("The activity position cannot be negative.")
    tunnel = resolve_tunnel(
        credential=credential,
        address=address,
        for_update=True,
    )
    if expected_tunnel_id is not None and tunnel.id != expected_tunnel_id:
        raise InvalidRequest("The activity cursor is not valid for this tunnel.")
    high_water = max(tunnel.next_event_position - 1, 0)
    if position > high_water:
        raise InvalidRequest("The activity position is not valid for this tunnel.")
    if position and not TunnelEvent.objects.filter(tunnel=tunnel, position=position).exists():
        raise InvalidRequest("The activity position does not name a committed event.")
    checkpoint, created = ActivityCheckpoint.objects.select_for_update().get_or_create(
        tunnel=tunnel,
        credential=credential,
        defaults={"last_event_position": position},
    )
    if position < checkpoint.last_event_position:
        raise CheckpointRegression()
    advanced = (created and position > 0) or position > checkpoint.last_event_position
    if position > checkpoint.last_event_position:
        checkpoint.last_event_position = position
        checkpoint.save(update_fields=["last_event_position", "updated_at"])
    return checkpoint, advanced
