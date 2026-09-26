"""Write non-content audit events without creating domain import cycles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.apps import apps

from .logging import current_request_id

if TYPE_CHECKING:
    from accounts.models import User
    from agents.models import AgentCredential


def record_event(
    *,
    action: str,
    target_type: str,
    target_id: UUID | None,
    actor_user: User | None = None,
    actor_credential: AgentCredential | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Store an event that contains identifiers and safe metadata only."""

    audit_event = apps.get_model("tunnels", "AuditEvent")
    audit_event.objects.create(
        actor_user=actor_user,
        actor_credential=actor_credential,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=current_request_id(),
        metadata=metadata or {},
    )
