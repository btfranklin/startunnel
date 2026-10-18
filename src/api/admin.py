"""Typed API for full instance administration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.security import HttpBearer
from pydantic import TypeAdapter

from accounts.admin_access import authenticate_admin_key
from accounts.admin_commands import (
    ADMIN_OPERATIONS,
    AccountCreate,
    AccountPassword,
    AccountState,
    AdminCommand,
    AgentCreate,
    AgentRevoke,
    BeaconWithdraw,
    KeyCreate,
    KeyRevoke,
    TunnelClose,
    TunnelRetire,
    TunnelRollover,
    TunnelRotate,
    TunnelStart,
)
from accounts.admin_data import OperationReference, admin_resource, tunnel_resource
from accounts.admin_services import (
    admin_status,
    execute_admin_operation,
    list_admin_resources,
)
from accounts.models import AdminCredential, AdminOperation, User
from core import limits as instance_limits
from core.rate_limits import _consume
from core.security import client_ip
from tunnels.beacons import beacon_data, beacon_queryset
from tunnels.errors import InvalidCredential, InvalidRequest
from tunnels.models import Tunnel

from .schemas import BeaconResponse, ErrorResponse, IdempotencyKey

ADMIN_ERRORS = {
    400: ErrorResponse,
    401: ErrorResponse,
    409: ErrorResponse,
    429: ErrorResponse,
    503: ErrorResponse,
    500: ErrorResponse,
}


class AdminRequest(HttpRequest):
    auth: AdminCredential


class AdminBearer(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> AdminCredential:
        try:
            credential = authenticate_admin_key(token)
        except InvalidCredential:
            _consume(
                key="admin-auth:" + client_ip(request),
                limit=10,
                window_seconds=300,
                rule="admin authentication",
            )
            raise
        return credential


router = Router(auth=AdminBearer(), tags=["Administration"])


class AccountResource(Schema):
    id: UUID
    username: str
    active: bool
    browser_access: bool
    created_at: datetime
    initial_key: KeyResource | None = None


class KeyResource(Schema):
    id: UUID
    admin_id: UUID
    name: str
    display_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    available: bool


class AgentResource(Schema):
    id: UUID
    name: str
    display_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    available: bool


class TunnelResource(Schema):
    id: UUID
    label: str
    state: str
    creator_id: UUID
    created_at: datetime
    address: str | None
    address_generation: int | None
    active_cycle_id: UUID | None
    retirement_confirmation: str


class AuditResource(Schema):
    id: UUID
    actor_id: UUID | None
    credential_id: UUID | None
    channel: str
    action: str
    target_type: str
    target_id: UUID | None
    request_id: UUID | None
    created_at: datetime
    metadata: dict[str, Any]


class CycleResource(Schema):
    id: UUID
    number: int
    state: str
    label: str
    created_at: datetime


class AdminMe(Schema):
    admin: AccountResource
    credential_id: UUID
    authority: str


class AccountPage(Schema):
    items: list[AccountResource]
    next_cursor: str | None


class KeyPage(Schema):
    items: list[KeyResource]
    next_cursor: str | None


class AgentPage(Schema):
    items: list[AgentResource]
    next_cursor: str | None


class TunnelPage(Schema):
    items: list[TunnelResource]
    next_cursor: str | None


class AuditPage(Schema):
    items: list[AuditResource]
    next_cursor: str | None


class CyclePage(Schema):
    items: list[CycleResource]
    next_cursor: str | None


class BeaconPage(Schema):
    items: list[BeaconResponse]
    next_cursor: str | None


class OperationReceipt(Schema):
    id: UUID
    audit_event_id: UUID


class SafeAdminResult(Schema):
    resource: AccountResource | KeyResource | AgentResource | TunnelResource | BeaconResponse
    operation: OperationReceipt


class AdminResult(SafeAdminResult):
    secret: str | None = None


class OperationRecord(Schema):
    id: UUID
    name: str
    created_at: datetime
    expires_at: datetime
    result: SafeAdminResult


class Capabilities(Schema):
    authority: str
    operations: list[str]
    limits: dict[str, int]
    idempotency_window_seconds: int


class DatabaseStatus(Schema):
    healthy: bool
    engine: str | None = None


class MaintenanceStatus(Schema):
    healthy: bool
    cleanup_lag_seconds: float | None
    due_work_count: int | None


class CapacityStatus(Schema):
    active_agent_credentials: int
    non_retired_tunnels: int
    active_credential_limit: int
    tunnel_limit: int


class InstanceStatus(Schema):
    healthy: bool
    database: DatabaseStatus
    maintenance: MaintenanceStatus | None = None
    capacity: CapacityStatus | None = None
    limits: dict[str, int] | None = None
    runtime: dict[str, bool] | None = None


ResourceResponse = AccountResource | KeyResource | AgentResource | TunnelResource | BeaconResponse
RESOURCE_RESPONSE: TypeAdapter[ResourceResponse] = TypeAdapter(ResourceResponse)


def _operation_reference(data: OperationReference) -> OperationReceipt:
    return OperationReceipt(id=UUID(data["id"]), audit_event_id=UUID(data["audit_event_id"]))


def _execute(request: AdminRequest, data: AdminCommand, key: str) -> AdminResult:
    credential = request.auth
    result = execute_admin_operation(
        actor=credential.owner,
        credential=credential,
        command=data,
        key=key,
        request_data=data.model_dump(mode="json"),
    )
    return AdminResult(
        resource=RESOURCE_RESPONSE.validate_python(result["resource"]),
        operation=_operation_reference(result["operation"]),
        secret=result.get("secret"),
    )


@router.get("/me", response={200: AdminMe, **ADMIN_ERRORS})
def me(request: AdminRequest) -> AdminMe:
    return AdminMe(
        admin=AccountResource.model_validate(admin_resource(request.auth.owner)),
        credential_id=request.auth.id,
        authority="instance_admin",
    )


@router.get("/capabilities", response={200: Capabilities, **ADMIN_ERRORS})
def capabilities(request: AdminRequest) -> Capabilities:
    return Capabilities(
        authority="instance_admin",
        operations=list(ADMIN_OPERATIONS),
        limits=asdict(instance_limits.effective_limits()),
        idempotency_window_seconds=86400,
    )


@router.get("/status", response={200: InstanceStatus, **ADMIN_ERRORS})
def status(request: AdminRequest) -> InstanceStatus:
    snapshot = admin_status()
    maintenance = snapshot.get("maintenance")
    capacity = snapshot.get("capacity")
    return InstanceStatus(
        healthy=snapshot["healthy"],
        database=DatabaseStatus(
            healthy=snapshot["database"]["healthy"],
            engine=snapshot["database"].get("engine"),
        ),
        maintenance=MaintenanceStatus(
            healthy=maintenance["healthy"],
            cleanup_lag_seconds=maintenance["cleanup_lag_seconds"],
            due_work_count=maintenance["due_work_count"],
        )
        if maintenance is not None
        else None,
        capacity=CapacityStatus(
            active_agent_credentials=capacity["active_agent_credentials"],
            non_retired_tunnels=capacity["non_retired_tunnels"],
            active_credential_limit=capacity["active_credential_limit"],
            tunnel_limit=capacity["tunnel_limit"],
        )
        if capacity is not None
        else None,
        limits=snapshot.get("limits"),
        runtime=snapshot.get("runtime"),
    )


@router.get("/accounts", response={200: AccountPage, **ADMIN_ERRORS})
def accounts(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> AccountPage:
    page = list_admin_resources(
        "accounts", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )
    return AccountPage(
        items=[AccountResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/accounts/{uuid:admin_id}", response={200: AccountResource, **ADMIN_ERRORS})
def account(request: AdminRequest, admin_id: UUID) -> AccountResource:
    row = User.objects.filter(pk=admin_id).first()
    if row is None:
        raise InvalidRequest("This administrator is not available.")
    return AccountResource.model_validate(admin_resource(row))


@router.get("/keys", response={200: KeyPage, **ADMIN_ERRORS})
def keys(
    request: AdminRequest,
    limit: int = 100,
    cursor: str | None = None,
    state: str | None = None,
    admin_id: UUID | None = None,
) -> KeyPage:
    page = list_admin_resources(
        "keys",
        actor=request.auth.owner,
        limit=limit,
        cursor=cursor,
        state=state,
        admin_id=admin_id,
    )
    return KeyPage(
        items=[KeyResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/agents", response={200: AgentPage, **ADMIN_ERRORS})
def agents(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> AgentPage:
    page = list_admin_resources(
        "agents", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )
    return AgentPage(
        items=[AgentResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/tunnels", response={200: TunnelPage, **ADMIN_ERRORS})
def tunnels(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> TunnelPage:
    page = list_admin_resources(
        "tunnels", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )
    return TunnelPage(
        items=[TunnelResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/tunnels/{uuid:tunnel_id}", response={200: TunnelResource, **ADMIN_ERRORS})
def tunnel(request: AdminRequest, tunnel_id: UUID) -> TunnelResource:
    row = Tunnel.objects.filter(pk=tunnel_id).first()
    if row is None:
        raise InvalidRequest("This tunnel is not available.")
    return TunnelResource.model_validate(tunnel_resource(row))


@router.get("/tunnels/{uuid:tunnel_id}/cycles", response={200: CyclePage, **ADMIN_ERRORS})
def cycles(
    request: AdminRequest,
    tunnel_id: UUID,
    limit: int = 100,
    cursor: str | None = None,
    state: str | None = None,
) -> CyclePage:
    page = list_admin_resources(
        "cycles",
        actor=request.auth.owner,
        tunnel_id=tunnel_id,
        limit=limit,
        cursor=cursor,
        state=state,
    )
    return CyclePage(
        items=[CycleResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/audit", response={200: AuditPage, **ADMIN_ERRORS})
def audit(
    request: AdminRequest,
    limit: int = 100,
    cursor: str | None = None,
    action: str | None = None,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    credential_id: UUID | None = None,
) -> AuditPage:
    page = list_admin_resources(
        "audit",
        actor=request.auth.owner,
        limit=limit,
        cursor=cursor,
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        credential_id=credential_id,
    )
    return AuditPage(
        items=[AuditResource.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/operations/{uuid:operation_id}", response={200: OperationRecord, **ADMIN_ERRORS})
def operation(request: AdminRequest, operation_id: UUID) -> OperationRecord:
    row = AdminOperation.objects.filter(pk=operation_id, expires_at__gt=timezone.now()).first()
    if row is None:
        raise InvalidRequest("This operation is not available.")
    return OperationRecord(
        id=row.id,
        name=row.operation,
        created_at=row.created_at,
        expires_at=row.expires_at,
        result=SafeAdminResult(
            resource=RESOURCE_RESPONSE.validate_python(row.response["resource"]),
            operation=_operation_reference(row.response["operation"]),
        ),
    )


@router.post("/accounts/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_account(
    request: AdminRequest,
    data: AccountCreate,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/accounts/state", response={200: AdminResult, **ADMIN_ERRORS})
def account_state(
    request: AdminRequest,
    data: AccountState,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/accounts/password", response={200: AdminResult, **ADMIN_ERRORS})
def account_password(
    request: AdminRequest,
    data: AccountPassword,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/keys/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_key(
    request: AdminRequest,
    data: KeyCreate,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/keys/revoke", response={200: AdminResult, **ADMIN_ERRORS})
def revoke_key(
    request: AdminRequest,
    data: KeyRevoke,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/agents/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_agent(
    request: AdminRequest,
    data: AgentCreate,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/agents/revoke", response={200: AdminResult, **ADMIN_ERRORS})
def revoke_agent(
    request: AdminRequest,
    data: AgentRevoke,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/tunnels/start", response={200: AdminResult, **ADMIN_ERRORS})
def start_tunnel(
    request: AdminRequest,
    data: TunnelStart,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/tunnels/close", response={200: AdminResult, **ADMIN_ERRORS})
def close_tunnel(
    request: AdminRequest,
    data: TunnelClose,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/tunnels/rollover", response={200: AdminResult, **ADMIN_ERRORS})
def rollover_tunnel(
    request: AdminRequest,
    data: TunnelRollover,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/tunnels/rotate", response={200: AdminResult, **ADMIN_ERRORS})
def rotate_tunnel(
    request: AdminRequest,
    data: TunnelRotate,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.post("/tunnels/retire", response={200: AdminResult, **ADMIN_ERRORS})
def retire_tunnel(
    request: AdminRequest,
    data: TunnelRetire,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)


@router.get("/beacons", response={200: BeaconPage, **ADMIN_ERRORS})
def beacons(
    request: AdminRequest,
    limit: int = 100,
    cursor: str | None = None,
    tunnel_id: UUID | None = None,
) -> BeaconPage:
    page = list_admin_resources(
        "beacons",
        actor=request.auth.owner,
        limit=limit,
        cursor=cursor,
        tunnel_id=tunnel_id,
    )
    return BeaconPage(
        items=[BeaconResponse.model_validate(item) for item in page["items"]],
        next_cursor=page["next_cursor"],
    )


@router.get("/beacons/{uuid:beacon_id}", response={200: BeaconResponse, **ADMIN_ERRORS})
def beacon(request: AdminRequest, beacon_id: UUID) -> BeaconResponse:
    row = beacon_queryset().filter(pk=beacon_id).first()
    if row is None:
        raise InvalidRequest("This beacon is not available.")
    return BeaconResponse.model_validate(beacon_data(row))


@router.post("/beacons/withdraw", response={200: AdminResult, 404: ErrorResponse, **ADMIN_ERRORS})
def withdraw_beacon(
    request: AdminRequest,
    data: BeaconWithdraw,
    idempotency_key: IdempotencyKey,
) -> AdminResult:
    return _execute(request, data, idempotency_key)
