"""Canonical administrator resource data for receipts and inspection."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict, cast

from agents.models import AgentCredential
from core.models import AuditEvent
from tunnels.admin_actions import retirement_confirmation
from tunnels.models import Cycle, Tunnel, TunnelAddress

from .models import AdminCredential, User


def admin_resource(account: User) -> dict[str, Any]:
    return {
        "id": str(account.id),
        "username": account.username,
        "active": account.is_active,
        "browser_access": account.has_usable_password(),
        "created_at": account.date_joined.isoformat(),
    }


def key_resource(key: AdminCredential) -> dict[str, Any]:
    return {
        "id": str(key.id),
        "admin_id": str(key.owner_id),
        "name": key.name,
        "display_prefix": key.display_prefix,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "available": key.is_available,
    }


def agent_resource(key: AgentCredential) -> dict[str, Any]:
    return {
        "id": str(key.id),
        "name": key.name,
        "display_prefix": key.display_prefix,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "available": key.is_available,
    }


def _serialize_tunnel(
    tunnel: Tunnel, address: TunnelAddress | None, cycle: Cycle | None
) -> dict[str, Any]:
    return {
        "id": str(tunnel.id),
        "label": tunnel.label,
        "state": tunnel.state,
        "creator_id": str(tunnel.creator_id),
        "created_at": tunnel.created_at.isoformat(),
        "address": address.address if address else None,
        "address_generation": address.generation if address else None,
        "active_cycle_id": str(cycle.id) if cycle else None,
        "retirement_confirmation": retirement_confirmation(tunnel),
    }


def tunnel_resource(tunnel: Tunnel) -> dict[str, Any]:
    address = TunnelAddress.objects.filter(tunnel=tunnel, state=TunnelAddress.State.CURRENT).first()
    cycle = Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    return _serialize_tunnel(tunnel, address, cycle)


def _tunnel_list_resource(tunnel: Tunnel) -> dict[str, Any]:
    prefetched = cast(Any, tunnel)
    addresses: list[TunnelAddress] = prefetched.current_addresses
    cycles: list[Cycle] = prefetched.active_cycles
    return _serialize_tunnel(
        tunnel, addresses[0] if addresses else None, cycles[0] if cycles else None
    )


def audit_resource(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "actor_id": str(event.actor_user_id) if event.actor_user_id else None,
        "credential_id": str(event.actor_admin_credential_id or event.actor_credential_id)
        if event.actor_admin_credential_id or event.actor_credential_id
        else None,
        "channel": event.channel,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": str(event.target_id) if event.target_id else None,
        "request_id": str(event.request_id) if event.request_id else None,
        "created_at": event.created_at.isoformat(),
        "metadata": event.metadata,
    }


class OperationReference(TypedDict):
    id: str
    audit_event_id: str


class AdminOperationResult(TypedDict):
    resource: dict[str, Any]
    operation: OperationReference
    secret: NotRequired[str]


class DatabaseStatusData(TypedDict):
    healthy: bool
    engine: NotRequired[str]


class MaintenanceStatusData(TypedDict):
    healthy: bool
    cleanup_lag_seconds: float | None
    due_work_count: int | None


class CapacityStatusData(TypedDict):
    active_agent_credentials: int
    non_retired_tunnels: int
    active_credential_limit: int
    tunnel_limit: int


class AdminStatusData(TypedDict):
    healthy: bool
    database: DatabaseStatusData
    maintenance: NotRequired[MaintenanceStatusData]
    capacity: NotRequired[CapacityStatusData]
    limits: NotRequired[dict[str, int]]
    runtime: NotRequired[dict[str, bool]]


class AdminPageData(TypedDict):
    items: list[dict[str, Any]]
    next_cursor: str | None
