"""Admin control of cycle, address, and tunnel lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from core import limits as instance_limits
from core.audit import record_event
from core.errors import (
    CycleClosed,
    InvalidRequest,
    LifecycleConflict,
    ResourceUnavailable,
    TunnelUnavailable,
)
from core.limits import MAX_TUNNEL_RETIREMENTS
from core.logging import current_request_id

from .cycles import (
    StartedCycle,
    _allocate_random_address,
    _close_for_rollover_locked,
    _close_locked_cycle,
    _current_address,
    _prepare_cycle_root,
    _start_cycle_locked,
)
from .events import append_events
from .messages import _validate_session
from .models import (
    Beacon,
    Cycle,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)


@transaction.atomic
def close_cycle_as_admin(*, actor: User, tunnel_id: UUID, expected_cycle_id: UUID) -> None:
    tunnel = _locked_admin_tunnel(actor=actor, tunnel_id=tunnel_id)
    cycle = (
        Cycle.objects.select_for_update().filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    )
    if cycle is None or cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_locked_cycle(tunnel=tunnel, cycle=cycle, reason="instance_operator", actor_user=actor)


def _locked_admin_tunnel(*, actor: User, tunnel_id: UUID) -> Tunnel:
    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise TunnelUnavailable()
    tunnel = (
        Tunnel.objects.select_for_update(of=("self",))
        .select_related("creator")
        .filter(pk=tunnel_id)
        .first()
    )
    if tunnel is None:
        raise TunnelUnavailable()
    return tunnel


def _start_cycle_as_admin_locked(
    *,
    tunnel: Tunnel,
    actor: User,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str,
    expires_in_seconds: int | None,
    expected_cycle_id: UUID | None,
    root_session: str | None,
) -> StartedCycle:
    credential = tunnel.creator
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    seconds, label, payload, correlation, _ = _prepare_cycle_root(
        limits=instance_limits.effective_limits(),
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=[],
        root_correlation_id=None,
    )
    session = _validate_session(root_session)
    if expected_cycle_id is not None:
        cycle = (
            Cycle.objects.select_for_update()
            .filter(tunnel=tunnel, state=Cycle.State.ACTIVE)
            .first()
        )
        if cycle is None:
            raise CycleClosed()
        if cycle.id != expected_cycle_id:
            raise LifecycleConflict()
        _close_for_rollover_locked(
            tunnel=tunnel,
            cycle=cycle,
            actor_credential=credential,
            actor_user=actor,
        )
    return _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=[],
        session=session,
        actor_user=actor,
    )


@transaction.atomic
def start_cycle_as_admin(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_session: str | None = None,
) -> StartedCycle:
    tunnel = _locked_admin_tunnel(actor=actor, tunnel_id=tunnel_id)
    return _start_cycle_as_admin_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        expected_cycle_id=None,
        root_session=root_session,
    )


@transaction.atomic
def rollover_cycle_as_admin(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_cycle_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_session: str | None = None,
) -> StartedCycle:
    tunnel = _locked_admin_tunnel(actor=actor, tunnel_id=tunnel_id)
    return _start_cycle_as_admin_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        expected_cycle_id=expected_cycle_id,
        root_session=root_session,
    )


def retirement_confirmation(tunnel: Tunnel) -> str:
    return tunnel.label or str(tunnel.id)


def _rotate_locked_address(
    *, tunnel: Tunnel, actor: User, expected_address_generation: int
) -> None:
    if tunnel.state == Tunnel.State.RETIRED:
        raise LifecycleConflict()
    cycle = (
        Cycle.objects.select_for_update().filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    )
    current = _current_address(tunnel=tunnel, for_update=True)
    if current.generation != expected_address_generation:
        raise LifecycleConflict()
    now = timezone.now()
    current.state = TunnelAddress.State.RETIRED
    current.retired_at = now
    current.retirement_reason = "operator_rotation"
    current.save(update_fields=["state", "retired_at", "retirement_reason"])
    generation = current.generation + 1
    _allocate_random_address(tunnel=tunnel, generation=generation)
    append_events(
        tunnel=tunnel,
        events=[
            TunnelEvent(
                event_type=TunnelEvent.Type.ADDRESS_ROTATED, metadata={"generation": generation}
            )
        ],
    )
    if cycle is not None:
        from .beacons import end_beacon_for_cycle_locked

        end_beacon_for_cycle_locked(tunnel=tunnel, cycle=cycle, reason=Beacon.EndReason.WITHDRAWN)
    record_event(
        actor_user=actor,
        action="tunnel.address_rotated",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"generation": generation},
    )


@transaction.atomic
def rotate_address_as_admin(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_address_generation: int,
) -> None:
    tunnel = _locked_admin_tunnel(actor=actor, tunnel_id=tunnel_id)
    _rotate_locked_address(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
    )


def _retire_locked_tunnel(
    *,
    tunnel: Tunnel,
    reason: str,
    actor_user: User | None = None,
) -> None:
    """Retire one locked tunnel and keep its immutable cycle content."""

    cycle = (
        Cycle.objects.select_for_update().filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    )
    if cycle:
        _close_locked_cycle(
            tunnel=tunnel,
            cycle=cycle,
            reason=reason,
            actor_user=actor_user,
        )
        tunnel.refresh_from_db()
    now = timezone.now()
    TunnelAddress.objects.select_for_update().filter(
        tunnel=tunnel,
        state=TunnelAddress.State.CURRENT,
    ).update(
        state=TunnelAddress.State.RETIRED,
        retired_at=now,
        retirement_reason=reason,
    )
    tunnel.state = Tunnel.State.RETIRED
    tunnel.retired_at = now
    append_events(
        tunnel=tunnel,
        events=[
            TunnelEvent(event_type=TunnelEvent.Type.TUNNEL_RETIRED, metadata={"reason": reason})
        ],
        update_fields=("state", "retired_at"),
    )
    record_event(
        actor_user=actor_user,
        action="tunnel.retired",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"reason": reason},
    )


def _retire_as_admin_locked(
    *,
    tunnel: Tunnel,
    actor: User,
    expected_address_generation: int,
    confirmation: str,
) -> None:
    if confirmation != retirement_confirmation(tunnel):
        raise InvalidRequest("The retirement confirmation does not match.")
    current = _current_address(tunnel=tunnel, for_update=True)
    if current.generation != expected_address_generation:
        raise LifecycleConflict()
    _retire_locked_tunnel(tunnel=tunnel, reason="operator_retirement", actor_user=actor)


@transaction.atomic
def retire_tunnel_as_admin(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_address_generation: int,
    confirmation: str,
) -> None:
    tunnel = _locked_admin_tunnel(actor=actor, tunnel_id=tunnel_id)
    _retire_as_admin_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        confirmation=confirmation,
    )


@transaction.atomic
def retire_tunnels_for_credentials(*, credential_ids: list[UUID], reason: str) -> int:
    """Retire tunnels from replaced fixture credentials and keep their cycle content."""

    tunnels = list(
        Tunnel.objects.select_for_update()
        .filter(creator_id__in=credential_ids)
        .exclude(state=Tunnel.State.RETIRED)
        .order_by("id")
    )
    for tunnel in tunnels:
        _retire_locked_tunnel(tunnel=tunnel, reason=reason)
    return len(tunnels)


@dataclass(frozen=True, slots=True)
class TunnelRetirement:
    tunnel_id: UUID
    expected_address_generation: int
    confirmation: str


@transaction.atomic
def retire_tunnels_for_revoked_credential_as_admin(
    *, actor: User, credential_id: UUID, selections: list[TunnelRetirement]
) -> list[UUID]:
    """Retire explicitly selected tunnels with the normal confirmation checks."""

    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise TunnelUnavailable()
    if not 1 <= len(selections) <= MAX_TUNNEL_RETIREMENTS:
        raise InvalidRequest(f"Select from 1 through {MAX_TUNNEL_RETIREMENTS} tunnels.")
    selected = {item.tunnel_id: item for item in selections}
    if len(selected) != len(selections):
        raise InvalidRequest("Select each tunnel only once.")
    credential = AgentCredential.objects.filter(pk=credential_id).first()
    if credential is None:
        raise ResourceUnavailable("This agent credential is not available.")
    if credential.revoked_at is None:
        raise InvalidRequest("Select a revoked agent credential.")
    tunnels = list(
        Tunnel.objects.select_for_update(of=("self",)).filter(pk__in=selected).order_by("id")
    )
    if len(tunnels) != len(selected):
        raise TunnelUnavailable()
    if any(tunnel.creator_id != credential_id for tunnel in tunnels):
        raise InvalidRequest("Each selected tunnel must have the selected credential as creator.")
    for tunnel in tunnels:
        selection = selected[tunnel.pk]
        _retire_as_admin_locked(
            tunnel=tunnel,
            actor=actor,
            expected_address_generation=selection.expected_address_generation,
            confirmation=selection.confirmation,
        )
    return [tunnel.id for tunnel in tunnels]
