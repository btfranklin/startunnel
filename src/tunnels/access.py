"""Shared credential checks, tunnel access, and cycle selection."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from django.db.models import Exists, Q, QuerySet
from django.utils import timezone

from agents.models import AgentCredential
from core import limits as instance_limits
from core.errors import CycleClosed, CycleUnavailable, InvalidCredential, TunnelUnavailable
from core.limits import Limits

from .codec import InvalidAddress, canonical_address
from .models import Cycle, Tunnel, TunnelAddress


def credential_is_active(credential: AgentCredential) -> bool:
    return active_credential_query(credential=credential).exists()


def limits_for(credential: AgentCredential) -> Limits:
    if not credential_is_active(credential):
        raise InvalidCredential()
    return instance_limits.effective_limits()


def address_query(*, address: str) -> QuerySet[TunnelAddress]:
    return TunnelAddress.objects.filter(
        state=TunnelAddress.State.CURRENT,
        address=address,
    )


def active_credential_query(*, credential: AgentCredential) -> QuerySet[AgentCredential]:
    return AgentCredential.objects.filter(pk=credential.pk).available()


def _resolve_current_tunnel_if_authorized(
    *, credential: AgentCredential, address: str
) -> Tunnel | None:
    """Resolve the normal read path in one query or request the exact fallback."""

    try:
        canonical = canonical_address(address)
    except InvalidAddress:
        return None
    address_row = (
        address_query(address=canonical)
        .annotate(credential_is_active=Exists(active_credential_query(credential=credential)))
        .select_related("tunnel", "tunnel__creator")
        .first()
    )
    if (
        address_row is None
        or not getattr(address_row, "credential_is_active", False)
        or address_row.tunnel.state == Tunnel.State.RETIRED
    ):
        return None
    return address_row.tunnel


def resolve_tunnel(
    *, credential: AgentCredential, address: str, for_update: bool = False
) -> Tunnel:
    tunnel = _resolve_current_tunnel_if_authorized(
        credential=credential,
        address=address,
    )
    if tunnel is not None:
        if not for_update:
            return tunnel
        canonical = canonical_address(address)
        locked = (
            Tunnel.objects.select_related("creator")
            .select_for_update(of=("self",))
            .filter(pk=tunnel.pk)
            .first()
        )
        if (
            locked is None
            or locked.state == Tunnel.State.RETIRED
            or not address_query(address=canonical).filter(tunnel=locked).exists()
        ):
            raise TunnelUnavailable()
        return locked
    if not credential_is_active(credential):
        raise InvalidCredential()
    canonical = canonical_address(address)
    if not for_update:
        address_row = (
            address_query(address=canonical).select_related("tunnel", "tunnel__creator").first()
        )
        if address_row is None or address_row.tunnel.state == Tunnel.State.RETIRED:
            raise TunnelUnavailable()
        return address_row.tunnel

    address_row = address_query(address=canonical).only("id", "tunnel_id").first()
    if address_row is None:
        raise TunnelUnavailable()
    query = Tunnel.objects.select_related("creator").select_for_update(of=("self",))
    tunnel = query.filter(pk=address_row.tunnel_id).first()
    if tunnel is None or tunnel.state == Tunnel.State.RETIRED:
        raise TunnelUnavailable()
    if (
        not address_query(address=canonical)
        .filter(
            pk=address_row.pk,
            tunnel=tunnel,
        )
        .exists()
    ):
        raise TunnelUnavailable()
    return tunnel


def active_cycle(*, tunnel: Tunnel, for_update: bool = False) -> Cycle:
    query = Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE)
    if for_update:
        query = query.select_for_update()
    cycle = query.first()
    if cycle is None or tunnel.state != Tunnel.State.ACTIVE or cycle.expires_at <= timezone.now():
        raise CycleClosed()
    return cycle


def cycle_is_readable(cycle: Cycle, *, now: datetime | None = None) -> bool:
    """Apply history deadlines even before maintenance records an expiry."""

    checked_at = now or timezone.now()
    if cycle.state == Cycle.State.DELETED:
        return False
    if cycle.state == Cycle.State.CLOSED:
        return cycle.delete_after is None or checked_at < cycle.delete_after
    if cycle.state != Cycle.State.ACTIVE:
        return False
    if checked_at < cycle.expires_at or cycle.retention_seconds is None:
        return True
    return checked_at < cycle.expires_at + timedelta(seconds=cycle.retention_seconds)


def select_cycle(*, tunnel: Tunnel, cycle_id: UUID | None) -> Cycle:
    now = timezone.now()
    if cycle_id is None:
        active = Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
        if active and cycle_is_readable(active, now=now):
            return active
        recent = (
            Cycle.objects.filter(
                tunnel=tunnel,
                state=Cycle.State.CLOSED,
            )
            .filter(Q(delete_after__isnull=True) | Q(delete_after__gt=now))
            .order_by("-number")
            .first()
        )
        if recent:
            return recent
        raise CycleUnavailable()
    cycle = Cycle.objects.filter(tunnel=tunnel, pk=cycle_id).first()
    if cycle is None:
        raise CycleUnavailable()
    if cycle_is_readable(cycle, now=now):
        return cycle
    raise CycleUnavailable()
