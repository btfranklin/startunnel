"""Create, authenticate, and revoke named agent credentials."""

from __future__ import annotations

import base64
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core.audit import record_event

from .models import AgentCredential


class CredentialError(ValueError):
    """A safe credential-domain error."""


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    credential: AgentCredential
    key: str


def _digest(secret: str) -> bytes:
    return hmac.digest(settings.API_KEY_PEPPER.encode(), secret.encode(), "sha256")


def _new_key() -> str:
    encoded = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return f"st_{encoded}"


def _lock_credential_quota() -> None:
    from django.db import connection

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [7_195_415_308_901])


@transaction.atomic
def create_credential(
    *, actor: User, name: str, expires_at: datetime | None = None, key: str | None = None
) -> IssuedCredential:
    actor = User.objects.select_for_update().get(pk=actor.pk)
    if not actor.is_active:
        raise CredentialError("This account cannot create an agent credential.")
    _lock_credential_quota()
    active_count = AgentCredential.objects.available().count()
    if active_count >= settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT:
        raise CredentialError("This instance has reached its active agent credential limit.")
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 100:
        raise CredentialError("Give the agent credential a name of 1 to 100 characters.")
    key = key or _new_key()
    credential = AgentCredential.objects.create(
        created_by=actor,
        name=normalized_name,
        display_prefix=key[:11],
        key_digest=_digest(key),
        expires_at=expires_at,
    )
    record_event(
        actor_user=actor,
        action="credential.created",
        target_type="agent_credential",
        target_id=credential.id,
        metadata={},
    )
    return IssuedCredential(credential=credential, key=key)


def parse_key(key: str) -> None:
    if not key.startswith("st_") or len(key) != 46:
        raise CredentialError("The agent credential is invalid.")
    encoded = key[3:]
    try:
        decoded = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise CredentialError("The agent credential is invalid.") from error
    if len(decoded) != 32:
        raise CredentialError("The agent credential is invalid.")


def authenticate_key(key: str) -> AgentCredential:
    parse_key(key)
    now = timezone.now()
    try:
        credential = AgentCredential.objects.available(at=now).get(key_digest=_digest(key))
    except AgentCredential.DoesNotExist as error:
        raise CredentialError("The agent credential is invalid.") from error
    if not credential.last_used_at or credential.last_used_at < now - timedelta(minutes=5):
        AgentCredential.objects.filter(pk=credential.pk).update(last_used_at=now)
        credential.last_used_at = now
    return credential


def revoke_credential(*, actor: User, credential: AgentCredential) -> None:
    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise CredentialError("This credential is not available.")
    changed = AgentCredential.objects.filter(pk=credential.pk, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    if changed:
        record_event(
            actor_user=actor,
            action="credential.revoked",
            target_type="agent_credential",
            target_id=credential.id,
            metadata={},
        )
