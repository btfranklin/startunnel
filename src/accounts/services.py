"""Manage local human access to one StarTunnel instance."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction

from core.audit import record_event

from .models import User


class AccountError(ValueError):
    """A safe local-account domain error."""


def require_active_human(user: User) -> None:
    if not user.is_active:
        raise AccountError("This account is inactive.")


@transaction.atomic
def create_human(*, actor: User, username: str, password: str) -> User:
    require_active_human(User.objects.select_for_update().get(pk=actor.pk))
    user = User.objects.create_user(username=username, password=password)
    record_event(
        actor_user=actor,
        action="human.created",
        target_type="user",
        target_id=user.id,
    )
    return user


@transaction.atomic
def set_human_active(*, actor: User, user_id: UUID, active: bool) -> User:
    locked_users = list(User.objects.select_for_update().order_by("id"))
    users_by_id = {user.id: user for user in locked_users}
    current_actor = users_by_id.get(actor.id)
    target: User | None = users_by_id.get(user_id)
    if current_actor is None or not current_actor.is_active or target is None:
        raise AccountError("This account is not available.")

    if not active and target.is_active:
        active_count = sum(user.is_active for user in locked_users)
        if active_count == 1:
            raise AccountError("The last active human account cannot be deactivated.")

    if target.is_active != active:
        target.is_active = active
        target.save(update_fields=["is_active"])
        record_event(
            actor_user=actor,
            action="human.reactivated" if active else "human.deactivated",
            target_type="user",
            target_id=target.id,
        )
    return target
