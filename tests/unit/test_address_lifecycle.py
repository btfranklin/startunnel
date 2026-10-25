"""Instance administrators control address and tunnel lifecycle."""

import pytest
from tests.factories import CredentialFactory, UserFactory

from agents.models import AgentCredential
from core.errors import InvalidRequest, LifecycleConflict, TunnelUnavailable
from core.models import AuditEvent
from tunnels.admin_actions import retire_tunnel_as_admin, rotate_address_as_admin
from tunnels.codec import display_address
from tunnels.cycles import CreatedTunnel, create_tunnel, get_tunnel_status
from tunnels.messages import post_reply
from tunnels.models import Cycle, Tunnel, TunnelAddress, TunnelEvent

pytestmark = pytest.mark.django_db


def _create(credential: AgentCredential) -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key="address-lifecycle-create",
        label="Address lifecycle board",
        root_content={"type": "text", "text": "Coordinate the release."},
    )


def test_any_active_admin_can_rotate_an_address(
    credential_factory: CredentialFactory, user_factory: UserFactory
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)

    rotate_address_as_admin(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )
    rotated = created.tunnel.addresses.get(state="current")

    assert rotated.generation == 2
    assert rotated.address != created.address
    assert list(
        created.tunnel.addresses.order_by("generation").values_list("address", flat=True)
    ) == [
        created.address,
        rotated.address,
    ]
    states = created.tunnel.addresses.order_by("generation").values_list("state", flat=True)
    assert list(states) == [
        TunnelAddress.State.RETIRED,
        TunnelAddress.State.CURRENT,
    ]
    with pytest.raises(TunnelUnavailable):
        get_tunnel_status(credential=reader, address=created.address)
    with pytest.raises(TunnelUnavailable):
        get_tunnel_status(credential=reader, address=display_address(created.address))
    assert get_tunnel_status(credential=reader, address=rotated.address).tunnel == created.tunnel
    assert (
        get_tunnel_status(credential=reader, address=display_address(rotated.address)).tunnel
        == created.tunnel
    )
    assert AuditEvent.objects.get(action="tunnel.address_rotated").metadata == {"generation": 2}


def test_create_retry_never_returns_rotated_or_retired_address(
    credential_factory: CredentialFactory, user_factory: UserFactory
) -> None:
    creator, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)
    rotate_address_as_admin(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
    )
    with pytest.raises(TunnelUnavailable):
        _create(creator)
    assert created.tunnel.addresses.count() == 2

    retire_tunnel_as_admin(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        confirmation=created.tunnel.label,
        expected_address_generation=2,
    )
    with pytest.raises(TunnelUnavailable):
        _create(creator)


def test_rotation_rejects_stale_generation_and_inactive_admin(
    credential_factory: CredentialFactory, user_factory: UserFactory
) -> None:
    creator, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)
    with pytest.raises(LifecycleConflict):
        rotate_address_as_admin(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=2,
        )
    administrator.is_active = False
    administrator.save(update_fields=["is_active"])
    with pytest.raises(TunnelUnavailable):
        rotate_address_as_admin(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
        )


def test_display_form_uses_the_same_address_for_reply_retries(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    first = post_reply(
        credential=creator,
        idempotency_key="display-form-reply",
        address=display_address(created.address),
        parent_id=created.root.id,
        content={"type": "text", "text": "Reply"},
    )
    replay = post_reply(
        credential=creator,
        idempotency_key="display-form-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Reply"},
    )
    assert replay.replay and replay.message.id == first.message.id


def test_retirement_requires_confirmation_and_closes_the_cycle(
    credential_factory: CredentialFactory, user_factory: UserFactory
) -> None:
    creator, _ = credential_factory()
    administrator = user_factory()
    created = _create(creator)
    with pytest.raises(InvalidRequest, match="confirmation"):
        retire_tunnel_as_admin(
            actor=administrator,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
            confirmation="wrong value",
        )

    retire_tunnel_as_admin(
        actor=administrator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        confirmation="Address lifecycle board",
    )
    created.tunnel.refresh_from_db()
    created.cycle.refresh_from_db()
    assert created.tunnel.state == Tunnel.State.RETIRED
    assert created.cycle.state == Cycle.State.CLOSED
    with pytest.raises(TunnelUnavailable):
        get_tunnel_status(credential=creator, address=created.address)
    assert list(created.tunnel.events.values_list("event_type", flat=True))[-3:] == [
        TunnelEvent.Type.CYCLE_CLOSED,
        TunnelEvent.Type.TUNNEL_DORMANT,
        TunnelEvent.Type.TUNNEL_RETIRED,
    ]
