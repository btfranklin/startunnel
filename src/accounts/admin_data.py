"""Canonical administrator resource data for receipts and inspection."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from agents.models import AgentCredential
from core.json_types import JsonObject, json_object
from core.models import AuditEvent
from tunnels.access import cycle_is_readable
from tunnels.admin_actions import retirement_confirmation
from tunnels.beacons import BeaconData, CountedBeacon, beacon_data
from tunnels.models import Cycle, Tunnel, TunnelAddress

from .models import AdminCredential, User


class AgentResourceData(TypedDict):
    id: str
    name: str
    display_prefix: str
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None
    available: bool


class KeyResourceData(AgentResourceData, closed=True):
    admin_id: str


class AccountResourceData(TypedDict, closed=True):
    id: str
    username: str
    active: bool
    browser_access: bool
    created_at: str
    initial_key: NotRequired[KeyResourceData]


class TunnelResourceData(TypedDict, closed=True):
    id: str
    label: str
    state: str
    creator_id: str
    created_at: str
    address: str | None
    address_generation: int | None
    active_cycle_id: str | None
    retirement_confirmation: str


class AuditResourceData(TypedDict, closed=True):
    id: str
    actor_id: str | None
    credential_id: str | None
    channel: str
    action: str
    target_type: str
    target_id: str | None
    request_id: str | None
    created_at: str
    metadata: JsonObject


class CycleResourceData(TypedDict, closed=True):
    id: str
    number: int
    state: str
    label: str
    created_at: str


class TunnelRetirementData(TypedDict, closed=True):
    credential_id: str
    retired_tunnel_ids: list[str]


type AdminResourceData = (
    AccountResourceData
    | KeyResourceData
    | AgentResourceData
    | TunnelResourceData
    | BeaconData
    | TunnelRetirementData
)
type ListedResourceData = (
    AccountResourceData
    | KeyResourceData
    | AgentResourceData
    | TunnelResourceData
    | BeaconData
    | AuditResourceData
    | CycleResourceData
)


class TunnelPrefetchData(TypedDict):
    current_addresses: list[TunnelAddress]
    active_cycles: list[Cycle]


if TYPE_CHECKING:
    from django_stubs_ext import WithAnnotations

    type PrefetchedTunnel = WithAnnotations[Tunnel, TunnelPrefetchData]
else:
    type PrefetchedTunnel = Tunnel


def admin_resource(account: User) -> AccountResourceData:
    return {
        "id": str(account.id),
        "username": account.username,
        "active": account.is_active,
        "browser_access": account.has_usable_password(),
        "created_at": account.date_joined.isoformat(),
    }


def key_resource(key: AdminCredential) -> KeyResourceData:
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


def agent_resource(key: AgentCredential) -> AgentResourceData:
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
) -> TunnelResourceData:
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


def tunnel_resource(tunnel: Tunnel) -> TunnelResourceData:
    address = TunnelAddress.objects.filter(tunnel=tunnel, state=TunnelAddress.State.CURRENT).first()
    cycle = Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    return _serialize_tunnel(tunnel, address, cycle)


def _tunnel_list_resource(tunnel: Tunnel) -> TunnelResourceData:
    prefetched = cast(PrefetchedTunnel, tunnel)
    addresses: list[TunnelAddress] = prefetched.current_addresses
    cycles: list[Cycle] = prefetched.active_cycles
    return _serialize_tunnel(
        tunnel, addresses[0] if addresses else None, cycles[0] if cycles else None
    )


def audit_resource(event: AuditEvent) -> AuditResourceData:
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
        "metadata": json_object(event.metadata),
    }


class OperationReference(TypedDict, closed=True):
    id: str
    audit_event_id: str


class AdminOperationResult(TypedDict, closed=True):
    resource: AdminResourceData
    operation: OperationReference
    secret: NotRequired[str]


class DatabaseStatusData(TypedDict, closed=True):
    healthy: bool
    engine: NotRequired[str]


class MaintenanceStatusData(TypedDict, closed=True):
    healthy: bool
    cleanup_lag_seconds: float | None
    due_work_count: int | None


class CapacityStatusData(TypedDict, closed=True):
    active_agent_credentials: int
    non_retired_tunnels: int
    active_credential_limit: int
    tunnel_limit: int


class AdminStatusData(TypedDict, closed=True):
    healthy: bool
    database: DatabaseStatusData
    maintenance: NotRequired[MaintenanceStatusData]
    capacity: NotRequired[CapacityStatusData]
    limits: NotRequired[dict[str, int]]
    runtime: NotRequired[dict[str, bool]]


class AdminPageData(TypedDict, closed=True):
    items: list[ListedResourceData]
    next_cursor: str | None


def beacon_resource(beacon: CountedBeacon) -> BeaconData:
    """Keep Beacon history metadata after its cycle content expires."""

    data = beacon_data(beacon)
    if not cycle_is_readable(beacon.cycle):
        data["text"] = ""
    return data
