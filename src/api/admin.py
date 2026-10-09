"""Typed API for full instance administration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from ninja import HeaderEx, P, Router, Schema
from ninja.security import HttpBearer

from accounts.admin_access import authenticate_admin_key
from accounts.admin_services import (
    ADMIN_OPERATION_TARGETS,
    admin_resource,
    admin_status,
    execute_admin_operation,
    list_admin_resources,
    tunnel_resource,
)
from accounts.models import AdminCredential, AdminOperation, User
from core.limits import provider
from core.rate_limits import _consume, consume_admin_operation
from core.security import client_ip
from tunnels.errors import InvalidCredential, InvalidRequest
from tunnels.models import Tunnel

from .schemas import ErrorResponse, MessageContent, PublicSchema

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
        if request.method == "GET":
            consume_admin_operation(credential.owner_id)
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


class OperationReceipt(Schema):
    id: UUID
    audit_event_id: UUID


class SafeAdminResult(Schema):
    resource: AccountResource | KeyResource | AgentResource | TunnelResource
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


class AccountCreate(PublicSchema):
    username: str
    password: str | None = None
    key_name: str = "initial"


class AccountState(PublicSchema):
    admin_id: UUID
    active: bool
    expected_active: bool


class AccountPassword(PublicSchema):
    admin_id: UUID
    password: str | None = None


class KeyCreate(PublicSchema):
    admin_id: UUID
    name: str
    expires_at: datetime | None = None


class KeyRevoke(PublicSchema):
    key_id: UUID


class AgentCreate(PublicSchema):
    name: str
    expires_at: datetime | None = None


class AgentRevoke(PublicSchema):
    credential_id: UUID


class TunnelStart(PublicSchema):
    tunnel_id: UUID
    expected_address_generation: int
    root_content: MessageContent
    cycle_label: str = ""
    expires_in_seconds: int | None = None


class TunnelRollover(TunnelStart):
    expected_cycle_id: UUID


class TunnelClose(PublicSchema):
    tunnel_id: UUID
    expected_cycle_id: UUID


class TunnelRotate(PublicSchema):
    tunnel_id: UUID
    expected_address_generation: int


class TunnelRetire(TunnelRotate):
    confirmation: str


def _execute(request: AdminRequest, operation: str, data: Schema, key: str) -> dict[str, Any]:
    credential = request.auth
    try:
        return execute_admin_operation(
            actor=credential.owner,
            credential=credential,
            operation=operation,
            key=key,
            data=data.model_dump(mode="json"),
        )
    except ObjectDoesNotExist as error:
        raise InvalidRequest("The requested admin resource is not available.") from error


@router.get("/me", response={200: AdminMe, **ADMIN_ERRORS})
def me(request: AdminRequest) -> dict[str, Any]:
    return {
        "admin": admin_resource(request.auth.owner),
        "credential_id": str(request.auth.id),
        "authority": "instance_admin",
    }


@router.get("/capabilities", response={200: Capabilities, **ADMIN_ERRORS})
def capabilities(request: AdminRequest) -> dict[str, Any]:
    return {
        "authority": "instance_admin",
        "operations": list(ADMIN_OPERATION_TARGETS),
        "limits": asdict(provider.for_instance()),
        "idempotency_window_seconds": 86400,
    }


@router.get("/status", response={200: InstanceStatus, **ADMIN_ERRORS})
def status(request: AdminRequest) -> dict[str, Any]:
    return admin_status()


@router.get("/accounts", response={200: AccountPage, **ADMIN_ERRORS})
def accounts(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> dict[str, Any]:
    return list_admin_resources(
        "accounts", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )


@router.get("/accounts/{uuid:admin_id}", response={200: AccountResource, **ADMIN_ERRORS})
def account(request: AdminRequest, admin_id: UUID) -> dict[str, Any]:
    row = User.objects.filter(pk=admin_id).first()
    if row is None:
        raise InvalidRequest("This administrator is not available.")
    return admin_resource(row)


@router.get("/keys", response={200: KeyPage, **ADMIN_ERRORS})
def keys(
    request: AdminRequest,
    limit: int = 100,
    cursor: str | None = None,
    state: str | None = None,
    admin_id: UUID | None = None,
) -> dict[str, Any]:
    return list_admin_resources(
        "keys",
        actor=request.auth.owner,
        limit=limit,
        cursor=cursor,
        state=state,
        admin_id=admin_id,
    )


@router.get("/agents", response={200: AgentPage, **ADMIN_ERRORS})
def agents(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> dict[str, Any]:
    return list_admin_resources(
        "agents", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )


@router.get("/tunnels", response={200: TunnelPage, **ADMIN_ERRORS})
def tunnels(
    request: AdminRequest, limit: int = 100, cursor: str | None = None, state: str | None = None
) -> dict[str, Any]:
    return list_admin_resources(
        "tunnels", actor=request.auth.owner, limit=limit, cursor=cursor, state=state
    )


@router.get("/tunnels/{uuid:tunnel_id}", response={200: TunnelResource, **ADMIN_ERRORS})
def tunnel(request: AdminRequest, tunnel_id: UUID) -> dict[str, Any]:
    row = Tunnel.objects.filter(pk=tunnel_id).first()
    if row is None:
        raise InvalidRequest("This tunnel is not available.")
    return tunnel_resource(row)


@router.get("/tunnels/{uuid:tunnel_id}/cycles", response={200: CyclePage, **ADMIN_ERRORS})
def cycles(
    request: AdminRequest,
    tunnel_id: UUID,
    limit: int = 100,
    cursor: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    return list_admin_resources(
        "cycles",
        actor=request.auth.owner,
        tunnel_id=tunnel_id,
        limit=limit,
        cursor=cursor,
        state=state,
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
) -> dict[str, Any]:
    return list_admin_resources(
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


@router.get("/operations/{uuid:operation_id}", response={200: OperationRecord, **ADMIN_ERRORS})
def operation(request: AdminRequest, operation_id: UUID) -> dict[str, Any]:
    row = AdminOperation.objects.filter(pk=operation_id, owner=request.auth.owner).first()
    if row is None:
        raise InvalidRequest("This operation is not available.")
    return {
        "id": str(row.id),
        "name": row.operation,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "result": row.response,
    }


@router.post("/accounts/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_account(
    request: AdminRequest,
    data: AccountCreate,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "accounts.create", data, idempotency_key)


@router.post("/accounts/state", response={200: AdminResult, **ADMIN_ERRORS})
def account_state(
    request: AdminRequest,
    data: AccountState,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "accounts.state", data, idempotency_key)


@router.post("/accounts/password", response={200: AdminResult, **ADMIN_ERRORS})
def account_password(
    request: AdminRequest,
    data: AccountPassword,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "accounts.password", data, idempotency_key)


@router.post("/keys/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_key(
    request: AdminRequest,
    data: KeyCreate,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "keys.create", data, idempotency_key)


@router.post("/keys/revoke", response={200: AdminResult, **ADMIN_ERRORS})
def revoke_key(
    request: AdminRequest,
    data: KeyRevoke,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "keys.revoke", data, idempotency_key)


@router.post("/agents/create", response={200: AdminResult, **ADMIN_ERRORS})
def create_agent(
    request: AdminRequest,
    data: AgentCreate,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "agents.create", data, idempotency_key)


@router.post("/agents/revoke", response={200: AdminResult, **ADMIN_ERRORS})
def revoke_agent(
    request: AdminRequest,
    data: AgentRevoke,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "agents.revoke", data, idempotency_key)


@router.post("/tunnels/start", response={200: AdminResult, **ADMIN_ERRORS})
def start_tunnel(
    request: AdminRequest,
    data: TunnelStart,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "tunnels.start", data, idempotency_key)


@router.post("/tunnels/close", response={200: AdminResult, **ADMIN_ERRORS})
def close_tunnel(
    request: AdminRequest,
    data: TunnelClose,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "tunnels.close", data, idempotency_key)


@router.post("/tunnels/rollover", response={200: AdminResult, **ADMIN_ERRORS})
def rollover_tunnel(
    request: AdminRequest,
    data: TunnelRollover,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "tunnels.rollover", data, idempotency_key)


@router.post("/tunnels/rotate", response={200: AdminResult, **ADMIN_ERRORS})
def rotate_tunnel(
    request: AdminRequest,
    data: TunnelRotate,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "tunnels.rotate", data, idempotency_key)


@router.post("/tunnels/retire", response={200: AdminResult, **ADMIN_ERRORS})
def retire_tunnel(
    request: AdminRequest,
    data: TunnelRetire,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> dict[str, Any]:
    return _execute(request, "tunnels.retire", data, idempotency_key)
