"""Append ordered events while the caller holds the tunnel's write lock."""

from __future__ import annotations

from core.notifications import ACTIVITY_CHANNEL, notify

from .models import Tunnel, TunnelEvent


def append_events(
    *, tunnel: Tunnel, events: list[TunnelEvent], update_fields: tuple[str, ...] = ()
) -> int:
    """Save one event batch and return its last position inside the write transaction."""

    for position, event in enumerate(events, start=tunnel.next_event_position):
        event.tunnel = tunnel
        event.position = position
    tunnel.next_event_position += len(events)
    tunnel.save(update_fields=["next_event_position", *update_fields])
    TunnelEvent.objects.bulk_create(events)
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    return tunnel.next_event_position - 1
