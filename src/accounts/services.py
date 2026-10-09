"""Manage local admin access to one StarTunnel instance."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction

from core.audit import record_event

from .models import User


class AccountError(ValueError):
    """A safe local-account domain error."""


def require_active_admin(user: User) -> None:
    if not user.is_active:
        raise AccountError("This account is inactive.")


@transaction.atomic
def create_admin(*, actor: User, username: str, password: str) -> User:
    require_active_admin(User.objects.select_for_update().get(pk=actor.pk))
    user = User.objects.create_user(username=username, password=password)
    record_event(
        actor_user=actor,
        action="admin.created",
        target_type="user",
        target_id=user.id,
    )
    return user


@transaction.atomic
def set_admin_active(*, actor: User, admin_id: UUID, active: bool) -> User:
    from tunnels.errors import TunnelDomainError

    from .admin_access import change_admin_state

    try:
        target = change_admin_state(actor=actor, admin_id=admin_id, active=active)
    except TunnelDomainError as error:
        raise AccountError(
            "This account is not available."
            if error.code == "invalid_credential"
            else error.safe_message
        ) from error
    record_event(
        actor_user=actor,
        action="admin.reactivated" if active else "admin.deactivated",
        target_type="user",
        target_id=target.id,
    )
    return target
