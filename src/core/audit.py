"""Write non-content audit events without creating domain import cycles."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.apps import apps

from .logging import current_request_id

if TYPE_CHECKING:
    from accounts.models import AdminCredential, User
    from agents.models import AgentCredential


_admin_context: ContextVar[tuple[Any, str] | None] = ContextVar("admin_audit_context", default=None)


@contextmanager
def admin_audit_context(credential: AdminCredential | None, channel: str) -> Iterator[None]:
    token = _admin_context.set((credential, channel))
    try:
        yield
    finally:
        _admin_context.reset(token)


def record_event(
    *,
    action: str,
    target_type: str,
    target_id: UUID | None,
    actor_user: User | None = None,
    actor_credential: AgentCredential | None = None,
    metadata: dict[str, Any] | None = None,
    actor_admin_credential: AdminCredential | None = None,
    channel: str = "",
    request_id: UUID | None = None,
) -> Any:
    """Store an event that contains identifiers and safe metadata only."""

    context = _admin_context.get()
    if context is not None:
        actor_admin_credential = actor_admin_credential or context[0]
        channel = channel or context[1]
    audit_event = apps.get_model("tunnels", "AuditEvent")
    return audit_event.objects.create(
        actor_user=actor_user,
        actor_credential=actor_credential,
        actor_admin_credential=actor_admin_credential,
        channel=channel or ("browser" if actor_user else "agent"),
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=request_id or current_request_id(),
        metadata=metadata or {},
    )
