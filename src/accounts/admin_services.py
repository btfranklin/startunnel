"""Shared product administration operations and safe inspection."""

from __future__ import annotations

import base64
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
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from agents.models import AgentCredential
from agents.services import create_credential, revoke_credential
from core.audit import admin_audit_context, record_event
from core.limits import provider
from core.maintenance_status import read_maintenance_status
from core.rate_limits import consume_admin_operation
from tunnels.codec import address_digest, encode_token
from tunnels.errors import IdempotencyConflict, InvalidRequest, LifecycleConflict, TunnelDomainError
from tunnels.idempotency import validate_idempotency_key
from tunnels.models import AuditEvent, Cycle, Tunnel, TunnelAddress
from tunnels.services import (
    close_cycle_as_operator,
    retire_tunnel_as_operator,
    retirement_confirmation,
    rollover_cycle_as_operator,
    rotate_address_as_operator,
    start_cycle_as_operator,
)

from .admin_access import (
    change_admin_state,
    issue_admin_key,
    lock_admin_access,
    require_recovery_path,
    validate_actor,
)
from .models import AdminCredential, AdminOperation, User

ADMIN_OPERATION_TARGETS = {
    "accounts.create": ("user", None),
    "accounts.state": ("user", "admin_id"),
    "accounts.password": ("user", "admin_id"),
    "keys.create": ("user", "admin_id"),
    "keys.revoke": ("admin_credential", "key_id"),
    "agents.create": ("agent_credential", None),
    "agents.revoke": ("agent_credential", "credential_id"),
    "tunnels.start": ("tunnel", "tunnel_id"),
    "tunnels.close": ("tunnel", "tunnel_id"),
    "tunnels.rollover": ("tunnel", "tunnel_id"),
    "tunnels.rotate": ("tunnel", "tunnel_id"),
    "tunnels.retire": ("tunnel", "tunnel_id"),
}


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
        "available": key.owner.is_active
        and key.revoked_at is None
        and (key.expires_at is None or key.expires_at > timezone.now()),
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


def tunnel_resource(tunnel: Tunnel) -> dict[str, Any]:
    address = TunnelAddress.objects.filter(tunnel=tunnel, state=TunnelAddress.State.CURRENT).first()
    cycle = Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    return {
        "id": str(tunnel.id),
        "label": tunnel.label,
        "state": tunnel.state,
        "creator_id": str(tunnel.creator_id),
        "created_at": tunnel.created_at.isoformat(),
        "address_generation": address.generation if address else None,
        "active_cycle_id": str(cycle.id) if cycle else None,
        "retirement_confirmation": retirement_confirmation(tunnel),
    }


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


def admin_status() -> dict[str, Any]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            database = cursor.fetchone() == (1,)
    except DatabaseError:
        return {"healthy": False, "database": {"healthy": False}}
    maintenance = read_maintenance_status()
    limits = provider.for_instance()
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
    limit: int = 100,
    cursor: str | None = None,
    state: str | None = None,
    admin_id: UUID | None = None,
    tunnel_id: UUID | None = None,
    action: str | None = None,
    actor_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    credential_id: UUID | None = None,
) -> dict[str, Any]:
    from core.cursors import decode_cursor, encode_cursor

    validate_actor(actor)
    if not 1 <= limit <= provider.for_instance().maximum_list_page:
        raise InvalidRequest("The page limit is outside the allowed range.")
    models: dict[str, Any] = {
        "accounts": User,
        "keys": AdminCredential,
        "agents": AgentCredential,
        "tunnels": Tunnel,
        "audit": AuditEvent,
        "cycles": Cycle,
    }
    queryset = (
        models[kind].objects.all().order_by("-created_at", "-id")
        if kind == "audit"
        else models[kind].objects.all().order_by("id")
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
            queryset = queryset.filter(revoked_at__isnull=state == "available")
            if state == "available":
                from django.db.models import Q

                queryset = queryset.filter(
                    Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
                )
                queryset = (
                    queryset.filter(owner__is_active=True)
                    if kind == "keys"
                    else queryset.filter(suspended_at__isnull=True)
                )
        else:
            queryset = queryset.filter(state=state)
    if admin_id and kind == "keys":
        queryset = queryset.filter(owner_id=admin_id)
    if tunnel_id and kind == "cycles":
        queryset = queryset.filter(tunnel_id=tunnel_id)
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
        if decoded.get("filters") != filters or decoded.get("owner") != str(actor.id):
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
        "tunnels": tunnel_resource,
        "audit": audit_resource,
        "cycles": lambda row: {
            "id": str(row.id),
            "number": row.number,
            "state": row.state,
            "label": row.label,
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
            owner=str(actor.id),
        )
        if len(rows) > limit
        else None,
    }


def _derived_secret(receipt: AdminOperation, kind: str) -> str:
    raw = hmac.digest(
        settings.IDEMPOTENCY_SECRET.encode(),
        b"admin-secret:" + kind.encode() + receipt.id.bytes + bytes(receipt.derivation_nonce),
        "sha256",
    )
    if kind == "address":
        return encode_token(raw[:16])
    return ("sta_" if kind == "admin_key" else "st_") + base64.urlsafe_b64encode(
        raw
    ).decode().rstrip("=")


def _replay(receipt: AdminOperation) -> dict[str, Any]:
    response = dict(receipt.response)
    if receipt.secret_kind:
        if receipt.secret_resource_id is None:
            raise LifecycleConflict("The operation resource is not available.")
        secret = _derived_secret(receipt, receipt.secret_kind)
        available = False
        if receipt.secret_kind == "admin_key":
            row = AdminCredential.objects.filter(pk=receipt.secret_resource_id).first()
            available = row is not None and key_resource(row)["available"]
        elif receipt.secret_kind == "agent_key":
            agent_row = AgentCredential.objects.filter(pk=receipt.secret_resource_id).first()
            available = agent_row is not None and agent_row.is_available
        elif receipt.secret_kind == "address":
            from tunnels.codec import parse_address

            available = TunnelAddress.objects.filter(
                pk=receipt.secret_resource_id,
                state=TunnelAddress.State.CURRENT,
                tunnel__state__in=["active", "dormant"],
                address_digest=address_digest(parse_address(secret)),
            ).exists()
        if not available:
            raise LifecycleConflict(
                "This operation secret is no longer available. Inspect the resource."
            )
        response["secret"] = secret
    return response


def _id(data: dict[str, Any], field: str) -> UUID:
    try:
        return UUID(str(data[field]))
    except (KeyError, ValueError, TypeError) as error:
        raise InvalidRequest(f"Supply a valid {field}.") from error


def _expiry(data: dict[str, Any]) -> datetime | None:
    value = data.get("expires_at")
    if value is None:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if timezone.is_naive(parsed) or parsed <= timezone.now():
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError) as error:
        raise InvalidRequest("Supply a future expiry with a time zone.") from error


def execute_admin_operation(
    *,
    actor: User,
    credential: AdminCredential | None = None,
    operation: str,
    key: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    try:
        consume_admin_operation(actor.id)
        return _execute_admin_operation(
            actor=actor, credential=credential, operation=operation, key=key, data=data
        )
    except TunnelDomainError as error:
        with suppress(DatabaseError):
            if User.objects.filter(pk=actor.pk).exists():
                target_type, field = ADMIN_OPERATION_TARGETS.get(
                    operation, ("admin_operation", None)
                )
                target_id = None
                if field is not None:
                    with suppress(ValueError, TypeError):
                        target_id = UUID(str(data.get(field)))
                metadata = {"outcome": "failure", "code": error.code}
                if operation in ADMIN_OPERATION_TARGETS:
                    metadata["operation"] = operation
                record_event(
                    actor_user=actor,
                    actor_admin_credential=credential,
                    channel="admin_api" if credential else "browser",
                    action="admin.operation_failed",
                    target_type=target_type,
                    target_id=target_id,
                    metadata=metadata,
                )
        raise


@transaction.atomic
def _execute_admin_operation(
    *,
    actor: User,
    credential: AdminCredential | None = None,
    operation: str,
    key: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    validate_idempotency_key(key)
    lock_admin_access()
    actor = validate_actor(actor, credential)
    digest = hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), key.encode(), "sha256")
    try:
        canonical = json.dumps(
            {"operation": operation, "data": data},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode()
    except (ValueError, TypeError) as error:
        raise InvalidRequest("The request is not valid JSON.") from error
    request_digest = hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), canonical, "sha256")
    receipt = (
        AdminOperation.objects.select_for_update().filter(owner=actor, key_digest=digest).first()
    )
    if receipt and receipt.expires_at > timezone.now():
        if not hmac.compare_digest(bytes(receipt.request_digest), request_digest):
            raise IdempotencyConflict()
        return _replay(receipt)
    if receipt:
        receipt.delete()
    receipt = AdminOperation.objects.create(
        owner=actor,
        operation=operation,
        key_digest=digest,
        request_digest=request_digest,
        derivation_nonce=secrets.token_bytes(32),
        expires_at=timezone.now() + timedelta(hours=24),
    )
    with admin_audit_context(credential, "admin_api" if credential else "browser"):
        try:
            resource, target_type, target_id = _perform(actor, receipt, operation, data)
        except ObjectDoesNotExist as error:
            raise InvalidRequest("The requested admin resource is not available.") from error
    event = record_event(
        actor_user=actor,
        actor_admin_credential=credential,
        channel="admin_api" if credential else "browser",
        action=operation,
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


def _perform(
    actor: User, receipt: AdminOperation, operation: str, data: dict[str, Any]
) -> tuple[dict[str, Any], str, UUID]:
    if operation == "accounts.create":
        username = User.normalize_username(str(data.get("username", ""))).strip()
        account = User(username=username)
        password = data.get("password")
        try:
            account.full_clean(exclude={"password"})
            if password:
                validate_password(password, user=account)
        except ValidationError as error:
            raise InvalidRequest(" ".join(error.messages)) from error
        account.set_password(password or None)
        account.save()
        resource = admin_resource(account)
        if not password:
            receipt.secret_kind = "admin_key"
            created, _ = issue_admin_key(
                owner=account,
                name=str(data.get("key_name", "initial")),
                key=_derived_secret(receipt, "admin_key"),
            )
            receipt.secret_resource_id = created.id
            resource["initial_key"] = key_resource(created)
        return resource, "user", account.id
    if operation == "accounts.state":
        if not isinstance(data.get("active"), bool) or not isinstance(
            data.get("expected_active"), bool
        ):
            raise InvalidRequest("Supply active and expected_active as booleans.")
        account = change_admin_state(
            actor=actor,
            admin_id=_id(data, "admin_id"),
            active=data["active"],
            expected_active=data["expected_active"],
        )
        return admin_resource(account), "user", account.id
    if operation == "accounts.password":
        account = User.objects.get(pk=_id(data, "admin_id"))
        password = data.get("password")
        if password:
            try:
                validate_password(password, user=account)
            except ValidationError as error:
                raise InvalidRequest(" ".join(error.messages)) from error
        account.set_password(password or None)
        account.save(update_fields=["password"])
        require_recovery_path()
        return admin_resource(account), "user", account.id
    if operation == "keys.create":
        account = User.objects.get(pk=_id(data, "admin_id"))
        receipt.secret_kind = "admin_key"
        created, _ = issue_admin_key(
            owner=account,
            name=str(data.get("name", "")),
            expires_at=_expiry(data),
            key=_derived_secret(receipt, "admin_key"),
        )
        receipt.secret_resource_id = created.id
        return key_resource(created), "admin_credential", created.id
    if operation == "keys.revoke":
        created = AdminCredential.objects.get(pk=_id(data, "key_id"))
        created.revoked_at = created.revoked_at or timezone.now()
        created.save(update_fields=["revoked_at"])
        require_recovery_path()
        return key_resource(created), "admin_credential", created.id
    if operation == "agents.create":
        receipt.secret_kind = "agent_key"
        issued = create_credential(
            actor=actor,
            name=str(data.get("name", "")),
            expires_at=_expiry(data),
            key=_derived_secret(receipt, "agent_key"),
        )
        receipt.secret_resource_id = issued.credential.id
        return agent_resource(issued.credential), "agent_credential", issued.credential.id
    if operation == "agents.revoke":
        agent = AgentCredential.objects.get(pk=_id(data, "credential_id"))
        revoke_credential(actor=actor, credential=agent)
        agent.refresh_from_db()
        return agent_resource(agent), "agent_credential", agent.id
    tunnel_id = _id(data, "tunnel_id")
    common: dict[str, Any] = {"actor": actor, "tunnel_id": tunnel_id}
    if operation == "tunnels.close":
        close_cycle_as_operator(**common, expected_cycle_id=_id(data, "expected_cycle_id"))
    elif operation in {"tunnels.start", "tunnels.rollover"}:
        arguments = {
            **common,
            "expected_address_generation": data.get("expected_address_generation"),
            "root_content": data.get("root_content", {}),
            "cycle_label": data.get("cycle_label", ""),
            "expires_in_seconds": data.get("expires_in_seconds"),
        }
        if operation == "tunnels.rollover":
            rollover_cycle_as_operator(
                **arguments, expected_cycle_id=_id(data, "expected_cycle_id")
            )
        else:
            start_cycle_as_operator(**arguments)
    elif operation == "tunnels.rotate":
        rotated = rotate_address_as_operator(
            **common,
            expected_address_generation=int(data["expected_address_generation"]),
            new_address=_derived_secret(receipt, "address"),
        )
        row = TunnelAddress.objects.get(tunnel_id=tunnel_id, generation=rotated.generation)
        receipt.secret_kind = "address"
        receipt.secret_resource_id = row.id
    elif operation == "tunnels.retire":
        retire_tunnel_as_operator(
            **common,
            confirmation=str(data.get("confirmation", "")),
            expected_address_generation=int(data["expected_address_generation"]),
        )
    else:
        raise InvalidRequest("The admin operation is not supported.")
    return tunnel_resource(Tunnel.objects.get(pk=tunnel_id)), "tunnel", tunnel_id
