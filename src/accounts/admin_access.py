"""Admin credential authentication and access protection."""

from __future__ import annotations

import base64
import hmac
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.limits import MAX_CREDENTIAL_NAME_CHARACTERS
from tunnels.errors import InvalidCredential, InvalidRequest, LifecycleConflict

from .models import AdminCredential, User


def key_digest(key: str) -> bytes:
    return hmac.digest(settings.API_KEY_PEPPER.encode(), key.encode(), "sha256")


def new_admin_key() -> str:
    return "sta_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def validate_actor(actor: User, credential: AdminCredential | None = None) -> User:
    current = User.objects.filter(pk=actor.pk, is_active=True).first()
    if current is None:
        raise InvalidCredential("The admin credential is not valid.")
    if (
        credential is not None
        and not AdminCredential.objects.available().filter(pk=credential.pk, owner=current).exists()
    ):
        raise InvalidCredential("The admin credential is not valid.")
    return current


def authenticate_admin_key(key: str) -> AdminCredential:
    if not key.startswith("sta_") or len(key) != 47:
        raise InvalidCredential("The admin credential is not valid.")
    try:
        raw = base64.b64decode(key[4:] + "=", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise InvalidCredential("The admin credential is not valid.") from error
    if len(raw) != 32:
        raise InvalidCredential("The admin credential is not valid.")
    now = timezone.now()
    credential = (
        AdminCredential.objects.available(at=now)
        .select_related("owner")
        .filter(key_digest=key_digest(key))
        .first()
    )
    if credential is None:
        raise InvalidCredential("The admin credential is not valid.")
    if credential.last_used_at is None or credential.last_used_at < now - timedelta(minutes=5):
        AdminCredential.objects.filter(pk=credential.pk).update(last_used_at=now)
        credential.last_used_at = now
    return credential


@transaction.atomic
def issue_admin_key(
    *, owner: User, name: str, key: str | None = None, expires_at: datetime | None = None
) -> tuple[AdminCredential, str]:
    name = name.strip()
    if not name or len(name) > MAX_CREDENTIAL_NAME_CHARACTERS:
        raise InvalidRequest(
            f"Give the admin credential a name of 1 to {MAX_CREDENTIAL_NAME_CHARACTERS} characters."
        )
    owner = User.objects.select_for_update(no_key=True).get(pk=owner.pk)
    if not owner.is_active:
        raise InvalidRequest("Activate this administrator before issuing a key.")
    if expires_at is not None and expires_at <= timezone.now():
        raise InvalidRequest("The expiry must be in the future.")
    secret = key or new_admin_key()
    credential = AdminCredential.objects.create(
        owner=owner,
        name=name,
        display_prefix=secret[:12],
        key_digest=key_digest(secret),
        expires_at=expires_at,
    )
    return credential, secret


@transaction.atomic
def change_admin_state(
    *, actor: User, admin_id: UUID, active: bool, expected_active: bool | None = None
) -> User:
    validate_actor(actor)
    target = User.objects.select_for_update(no_key=True).filter(pk=admin_id).first()
    if target is None:
        raise InvalidRequest("This admin account is not available.")
    if expected_active is not None and target.is_active != expected_active:
        raise LifecycleConflict("The admin state changed. Inspect it and try again.")
    target.is_active = active
    target.save(update_fields=["is_active"])
    if not active:
        AdminCredential.objects.filter(owner=target, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
    return target
