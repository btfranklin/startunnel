"""Domain writes append consecutive event positions and notify by batch."""

from __future__ import annotations

from typing import Any

import pytest

from core.notifications import ACTIVITY_CHANNEL
from tunnels.admin_actions import (
    retire_tunnel_as_admin,
    retirement_confirmation,
    rotate_address_as_admin,
)
from tunnels.beacons import publish_beacon
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Tunnel, TunnelEvent

pytestmark = pytest.mark.django_db


def test_event_batches_keep_lifecycle_order_and_the_next_position(
    credential_factory: Any, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tunnels.events.notify", lambda channel, payload: notifications.append((channel, payload))
    )
    credential, _ = credential_factory()
    administrator = user_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key="event-batch-create",
        root_content={"type": "text", "text": "Root."},
    )
    reply = post_reply(
        credential=credential,
        address=created.address,
        parent_id=created.root.id,
        idempotency_key="event-batch-reply",
        content={"type": "text", "text": "Reply."},
    )
    assert reply.event_position == 3
    publish_beacon(
        credential=credential,
        address=created.address,
        idempotency_key="event-batch-publish",
        text="Review this.",
    )
    close_cycle(
        credential=credential,
        address=created.address,
        expected_cycle_id=created.cycle.id,
        idempotency_key="event-batch-close",
    )
    rotate_address_as_admin(
        actor=administrator, tunnel_id=created.tunnel.id, expected_address_generation=1
    )
    retire_tunnel_as_admin(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=2,
        confirmation=retirement_confirmation(created.tunnel),
    )

    events = list(created.tunnel.events.order_by("position"))
    assert [event.position for event in events] == list(range(1, 10))
    assert [event.event_type for event in events] == [
        TunnelEvent.Type.CYCLE_STARTED,
        TunnelEvent.Type.MESSAGE_POSTED,
        TunnelEvent.Type.MESSAGE_POSTED,
        TunnelEvent.Type.BEACON_LIT,
        TunnelEvent.Type.CYCLE_CLOSED,
        TunnelEvent.Type.TUNNEL_DORMANT,
        TunnelEvent.Type.BEACON_ENDED,
        TunnelEvent.Type.ADDRESS_ROTATED,
        TunnelEvent.Type.TUNNEL_RETIRED,
    ]
    created.tunnel.refresh_from_db()
    assert created.tunnel.next_event_position == 10
    assert created.tunnel.state == Tunnel.State.RETIRED
    assert notifications == [(ACTIVITY_CHANNEL, str(created.tunnel.id))] * 7
