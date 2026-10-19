"""Shared product administration operations and safe inspection."""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, connection, transaction
from django.db.models import Prefetch
from django.utils import timezone

from agents.models import AgentCredential
from core import limits as instance_limits
from core.audit import admin_audit_context, record_event
from core.errors import DomainError, IdempotencyConflict, InvalidRequest, LifecycleConflict
from core.idempotency import validate_idempotency_key
from core.limits import DEFAULT_LIMITS
from core.maintenance_status import read_maintenance_status
from core.models import AuditEvent
from core.rate_limits import consume_admin_operation
from tunnels.access import cycle_is_readable
from tunnels.beacons import beacon_queryset
from tunnels.models import Beacon, Cycle, Tunnel, TunnelAddress

from .admin_access import validate_actor
from .admin_commands import AdminCommand, TunnelStart, derive_secret
from .admin_data import (
    AdminOperationResult,
    AdminPageData,
    AdminStatusData,
    OperationReference,
    _tunnel_list_resource,
    admin_resource,
    agent_resource,
    audit_resource,
    beacon_resource,
    key_resource,
)
from .models import AdminCredential, AdminReceipt, User


def admin_status() -> AdminStatusData:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database = cursor.fetchone() == (1,)
    except DatabaseError:
        return {"healthy": False, "database": {"healthy": False}}
    maintenance = read_maintenance_status()
    limits = instance_limits.effective_limits()
    return {
        "healthy": database and maintenance.current,
        "database": {"healthy": database, "engine": connection.vendor},
        "maintenance": {
            "healthy": maintenance.current,
            "cleanup_lag_seconds": maintenance.cleanup_lag_seconds,
            "due_work_count": maintenance.due_work_count,
        },
        "capacity": {
            "active_agent_credentials": AgentCredential.objects.available().count(),
            "non_retired_tunnels": Tunnel.objects.exclude(state=Tunnel.State.RETIRED).count(),
            "active_credential_limit": limits.active_credentials,
            "tunnel_limit": limits.non_retired_tunnels,
        },
        "limits": asdict(limits),
        "runtime": {"admin_api": True},
    }


def list_admin_resources(
    kind: str,
    *,
    actor: User,
    limit: int = DEFAULT_LIMITS.default_list_page,
    cursor: str | None = None,
    state: str | None = None,
    admin_id: UUID | None = None,
    tunnel_id: UUID | None = None,
    action: str | None = None,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    credential_id: UUID | None = None,
) -> AdminPageData:
    from core.cursors import decode_cursor, encode_cursor

    validate_actor(actor)
    if not 1 <= limit <= instance_limits.effective_limits().maximum_list_page:
        raise InvalidRequest("The page limit is outside the allowed range.")
    models: dict[str, Any] = {
        "accounts": User,
        "keys": AdminCredential,
        "agents": AgentCredential,
        "tunnels": Tunnel,
        "audit": AuditEvent,
        "cycles": Cycle,
        "beacons": Beacon,
    }
    queryset = (
        models[kind].objects.all().order_by("-created_at", "-id")
        if kind == "audit"
        else models[kind].objects.all().order_by("id")
    )
    if kind == "beacons":
        queryset = beacon_queryset().order_by("id")
    elif kind == "keys":
        queryset = queryset.select_related("owner")
    elif kind == "tunnels":
        queryset = queryset.prefetch_related(
            Prefetch(
                "addresses",
                queryset=TunnelAddress.objects.filter(state=TunnelAddress.State.CURRENT),
                to_attr="current_addresses",
            ),
            Prefetch(
                "cycles",
                queryset=Cycle.objects.filter(state=Cycle.State.ACTIVE),
                to_attr="active_cycles",
            ),
        )
    filters = {
        "state": state,
        "admin_id": str(admin_id) if admin_id else None,
        "tunnel_id": str(tunnel_id) if tunnel_id else None,
        "action": action,
        "actor_id": str(actor_id) if actor_id else None,
        "target_type": target_type,
        "target_id": str(target_id) if target_id else None,
        "credential_id": str(credential_id) if credential_id else None,
    }
    if state:
        if kind == "accounts":
            if state not in {"active", "inactive"}:
                raise InvalidRequest("Select active or inactive accounts.")
            queryset = queryset.filter(is_active=state == "active")
        elif kind in {"keys", "agents"}:
            if state not in {"available", "revoked"}:
                raise InvalidRequest("Select available or revoked keys.")
            queryset = (
                queryset.available()
                if state == "available"
                else queryset.filter(revoked_at__isnull=False)
            )
        else:
            queryset = queryset.filter(state=state)
    if admin_id and kind == "keys":
        queryset = queryset.filter(owner_id=admin_id)
    if tunnel_id and kind == "cycles":
        queryset = queryset.filter(tunnel_id=tunnel_id)
    elif tunnel_id and kind == "beacons":
        queryset = queryset.filter(cycle__tunnel_id=tunnel_id)
    if action and kind == "audit":
        queryset = queryset.filter(action=action)
    if kind == "audit":
        if actor_id:
            queryset = queryset.filter(actor_user_id=actor_id)
        if target_type:
            queryset = queryset.filter(target_type=target_type)
        if target_id:
            queryset = queryset.filter(target_id=target_id)
        if credential_id:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(actor_admin_credential_id=credential_id) | Q(actor_credential_id=credential_id)
            )
    if cursor:
        decoded = decode_cursor(cursor, kind="admin_" + kind)
        if decoded.get("filters") != filters:
            raise InvalidRequest("The cursor does not match this request.")
        try:
            after_id = UUID(str(decoded["after"]))
            if kind == "audit":
                from django.db.models import Q

                after_at = datetime.fromisoformat(decoded["after_at"])
                queryset = queryset.filter(
                    Q(created_at__lt=after_at) | Q(created_at=after_at, id__lt=after_id)
                )
            else:
                queryset = queryset.filter(id__gt=after_id)
        except (ValueError, KeyError) as error:
            raise InvalidRequest("The cursor is not valid.") from error
    rows = list(queryset[: limit + 1])
    serializers: dict[str, Callable[[Any], dict[str, Any]]] = {
        "accounts": admin_resource,
        "keys": key_resource,
        "agents": agent_resource,
        "tunnels": _tunnel_list_resource,
        "audit": audit_resource,
        "beacons": beacon_resource,
        "cycles": lambda row: {
            "id": str(row.id),
            "number": row.number,
            "state": row.state,
            "label": row.label if cycle_is_readable(row) else "",
            "created_at": row.created_at.isoformat(),
        },
    }
    return {
        "items": [serializers[kind](row) for row in rows[:limit]],
        "next_cursor": encode_cursor(
            "admin_" + kind,
            after=str(rows[limit - 1].id),
            after_at=rows[limit - 1].created_at.isoformat() if kind == "audit" else None,
            filters=filters,
        )
        if len(rows) > limit
        else None,
    }


def receipt_response(receipt: AdminReceipt) -> dict[str, Any]:
    """Read a receipt only while its retained result is available."""

    response = dict(receipt.response)
    if not response:
        raise InvalidRequest("This operation is not available.")
    if receipt.operation == "beacons.withdraw":
        beacon = (
            Beacon.objects.select_related("cycle")
            .filter(pk=response.get("resource", {}).get("id"))
            .first()
        )
        if beacon is None or not cycle_is_readable(beacon.cycle):
            raise InvalidRequest("The operation resource is not available.")
    return response


def _replay(receipt: AdminReceipt) -> AdminOperationResult:
    response = receipt_response(receipt)
    if receipt.operation == "tunnels.rotate":
        resource = response.get("resource", {})
        address_row = TunnelAddress.objects.filter(
            tunnel_id=resource.get("id"),
            generation=resource.get("address_generation"),
            state=TunnelAddress.State.CURRENT,
            tunnel__state__in=["active", "dormant"],
        ).first()
        if address_row is None or address_row.address != resource.get("address"):
            raise LifecycleConflict(
                "This operation address is no longer available. Inspect the resource."
            )
    if receipt.secret_kind:
        if receipt.secret_resource_id is None:
            raise LifecycleConflict("The operation resource is not available.")
        secret = ""
        available = False
        if receipt.secret_kind == "admin_key":
            secret = derive_secret(receipt, receipt.secret_kind)
            row = AdminCredential.objects.filter(pk=receipt.secret_resource_id).first()
            available = row is not None and row.is_available
        elif receipt.secret_kind == "agent_key":
            secret = derive_secret(receipt, receipt.secret_kind)
            agent_row = AgentCredential.objects.filter(pk=receipt.secret_resource_id).first()
            available = agent_row is not None and agent_row.is_available
        if not available:
            raise LifecycleConflict(
                "This operation secret is no longer available. Inspect the resource."
            )
        response["secret"] = secret
    result = AdminOperationResult(
        resource=response["resource"],
        operation=OperationReference(
            id=response["operation"]["id"],
            audit_event_id=response["operation"]["audit_event_id"],
        ),
    )
    if "secret" in response:
        result["secret"] = response["secret"]
    return result


def execute_admin_operation(
    *,
    actor: User,
    credential: AdminCredential | None = None,
    command: AdminCommand,
    key: str,
    request_data: dict[str, Any],
) -> AdminOperationResult:
    try:
        consume_admin_operation(actor.id)
        return _execute_admin_operation(
            actor=actor, credential=credential, command=command, key=key, request_data=request_data
        )
    except DomainError as error:
        with suppress(DatabaseError):
            if User.objects.filter(pk=actor.pk).exists():
                metadata = {
                    "outcome": "failure",
                    "code": error.code,
                    "operation": command.operation,
                }
                record_event(
                    actor_user=actor,
                    actor_admin_credential=credential,
                    channel="admin_api" if credential else "browser",
                    action="admin.operation_failed",
                    target_type=command.target_type,
                    target_id=command.target_id,
                    metadata=metadata,
                )
        raise


@transaction.atomic
def _execute_admin_operation(
    *,
    actor: User,
    credential: AdminCredential | None = None,
    command: AdminCommand,
    key: str,
    request_data: dict[str, Any],
) -> AdminOperationResult:
    validate_idempotency_key(key)
    if isinstance(command, TunnelStart) and request_data.get("root_session") is None:
        request_data = {
            field: value for field, value in request_data.items() if field != "root_session"
        }
    actor = validate_actor(actor, credential)
    digest = hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), key.encode(), "sha256")
    try:
        canonical = json.dumps(
            {"operation": command.operation, "data": request_data},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode()
    except (ValueError, TypeError) as error:
        raise InvalidRequest("The request is not valid JSON.") from error
    request_digest = hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), canonical, "sha256")
    defaults = {
        "operation": command.operation,
        "request_digest": request_digest,
        "derivation_nonce": secrets.token_bytes(32),
        "expires_at": timezone.now() + timedelta(hours=24),
    }
    receipt, created = AdminReceipt.objects.select_for_update().get_or_create(
        owner=actor, key_digest=digest, defaults=defaults
    )
    if not created and receipt.expires_at > timezone.now():
        if not hmac.compare_digest(bytes(receipt.request_digest), request_digest):
            raise IdempotencyConflict()
        return _replay(receipt)
    if not created:
        receipt.delete()
        receipt = AdminReceipt.objects.create(owner=actor, key_digest=digest, **defaults)
    with admin_audit_context(credential, "admin_api" if credential else "browser"):
        try:
            resource, target_type, target_id = command.perform(actor, receipt)
        except ObjectDoesNotExist as error:
            raise InvalidRequest("The requested admin resource is not available.") from error
    event = record_event(
        actor_user=actor,
        actor_admin_credential=credential,
        channel="admin_api" if credential else "browser",
        action=command.operation,
        target_type=target_type,
        target_id=target_id,
        metadata={"operation_id": str(receipt.id), "outcome": "success"},
    )
    receipt.response = {
        "resource": resource,
        "operation": {"id": str(receipt.id), "audit_event_id": str(event.id)},
    }
    receipt.save(update_fields=["response", "secret_kind", "secret_resource_id"])
    return _replay(receipt)
