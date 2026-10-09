"""Instance administrators control address and tunnel lifecycle."""

from typing import Any

import pytest

from tunnels.errors import InvalidRequest, LifecycleConflict, TunnelUnavailable
from tunnels.models import AuditEvent, Cycle, Tunnel, TunnelAddress, TunnelEvent
from tunnels.services import (
    create_tunnel,
    get_tunnel_status,
    retire_tunnel_as_operator,
    rotate_address_as_operator,
)

pytestmark = pytest.mark.django_db


def _create(credential: Any) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key="address-lifecycle-create",
        label="Address lifecycle board",
        root_content={"type": "text", "text": "Coordinate the release."},
    )


def test_any_active_admin_can_rotate_an_address(credential_factory: Any, user_factory: Any) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)

    rotated = rotate_address_as_operator(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )

    assert rotated.generation == 2
    assert rotated.address != created.address
    states = created.tunnel.addresses.order_by("generation").values_list("state", flat=True)
    assert list(states) == [
        TunnelAddress.State.RETIRED,
        TunnelAddress.State.CURRENT,
    ]
    with pytest.raises(TunnelUnavailable):
        get_tunnel_status(credential=reader, address=created.address)
    assert get_tunnel_status(credential=reader, address=rotated.address).tunnel == created.tunnel
    assert AuditEvent.objects.get(action="tunnel.address_rotated").metadata == {"generation": 2}


def test_rotation_rejects_stale_generation_and_inactive_admin(
    credential_factory: Any, user_factory: Any
) -> None:
    creator, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)
    with pytest.raises(LifecycleConflict):
        rotate_address_as_operator(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=2,
        )
    administrator.is_active = False
    administrator.save(update_fields=["is_active"])
    with pytest.raises(TunnelUnavailable):
        rotate_address_as_operator(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
        )


def test_retirement_requires_confirmation_and_closes_the_cycle(
    credential_factory: Any, user_factory: Any
) -> None:
    creator, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)
    with pytest.raises(InvalidRequest, match="confirmation"):
        retire_tunnel_as_operator(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
            confirmation="wrong value",
        )

    retire_tunnel_as_operator(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        confirmation="Address lifecycle board",
    )
    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert created.tunnel.state == Tunnel.State.RETIRED
    assert created.cycle.state == Cycle.State.CLOSED
    assert list(created.tunnel.events.values_list("event_type", flat=True))[-3:] == [
        TunnelEvent.Type.CYCLE_CLOSED,
        TunnelEvent.Type.TUNNEL_DORMANT,
        TunnelEvent.Type.TUNNEL_RETIRED,
    ]
