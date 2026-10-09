"""Instance agent keys authenticate independently from admin accounts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from agents.models import AgentCredential
from agents.services import (
    CredentialError,
    authenticate_key,
    create_credential,
    parse_key,
    revoke_credential,
)
from api.auth import agent_bearer
from tunnels.errors import InvalidCredential

pytestmark = pytest.mark.django_db


def test_create_authenticate_and_revoke_instance_credential(user_factory: Any) -> None:
    actor = user_factory()
    issued = create_credential(actor=actor, name="  Build agent  ")
    assert issued.key.startswith("st_") and len(issued.key) == 46
    assert issued.credential.name == "Build agent"
    assert issued.credential.created_by == actor
    assert issued.credential.expires_at is None
    assert bytes(issued.credential.key_digest) != issued.key.encode()
    assert authenticate_key(issued.key).id == issued.credential.id
    revoke_credential(actor=actor, credential=issued.credential)
    with pytest.raises(CredentialError, match="invalid"):
        authenticate_key(issued.key)


@pytest.mark.parametrize("key", ["", "st_short", "bad_" + "A" * 43, "st_" + "!" * 43])
def test_malformed_keys_are_rejected(key: str) -> None:
    with pytest.raises(CredentialError, match="invalid"):
        parse_key(key)


def test_decoder_length_and_unknown_key_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agents.services.base64.b64decode", lambda *args, **kwargs: b"short")
    with pytest.raises(CredentialError, match="invalid"):
        parse_key("st_" + "A" * 43)
    monkeypatch.undo()
    with pytest.raises(CredentialError, match="invalid"):
        authenticate_key("st_" + "A" * 43)


@pytest.mark.parametrize("state", ["revoked", "suspended", "expired"])
def test_unavailable_keys_cannot_authenticate(credential_factory: Any, state: str) -> None:
    credential, key = credential_factory()
    if state == "revoked":
        credential.revoked_at = timezone.now()
    elif state == "suspended":
        credential.suspended_at = timezone.now()
    else:
        credential.expires_at = timezone.now() - timedelta(seconds=1)
    credential.save()
    with pytest.raises(CredentialError, match="invalid"):
        authenticate_key(key)


def test_admin_deactivation_does_not_disable_agent_key(credential_factory: Any) -> None:
    credential, key = credential_factory()
    assert credential.created_by is not None
    credential.created_by.is_active = False
    credential.created_by.save(update_fields=["is_active"])
    assert authenticate_key(key).id == credential.id


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_credential_names_are_bounded(user_factory: Any, name: str) -> None:
    with pytest.raises(CredentialError, match="1 to 100"):
        create_credential(actor=user_factory(), name=name)


def test_inactive_admin_cannot_create_or_revoke(user_factory: Any, credential_factory: Any) -> None:
    actor = user_factory()
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    with pytest.raises(CredentialError, match="cannot create"):
        create_credential(actor=actor, name="Agent")
    credential, _ = credential_factory()
    with pytest.raises(CredentialError, match="not available"):
        revoke_credential(actor=actor, credential=credential)


def test_instance_credential_limit_blocks_new_keys(user_factory: Any, settings: Any) -> None:
    actor = user_factory()
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 1
    create_credential(actor=actor, name="First")
    with pytest.raises(CredentialError, match="limit"):
        create_credential(actor=actor, name="Second")


@pytest.mark.parametrize("state", ["expired", "suspended"])
def test_unavailable_credential_does_not_consume_quota(
    user_factory: Any,
    settings: Any,
    state: str,
) -> None:
    actor = user_factory()
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 1
    first = create_credential(actor=actor, name="First").credential
    if state == "expired":
        first.expires_at = timezone.now() - timedelta(seconds=1)
        first.save(update_fields=["expires_at"])
    else:
        first.suspended_at = timezone.now()
        first.save(update_fields=["suspended_at"])
    assert create_credential(actor=actor, name="Replacement").credential.is_available


def test_revoke_is_idempotent(user_factory: Any, credential_factory: Any) -> None:
    actor = user_factory()
    credential, _ = credential_factory()
    revoke_credential(actor=actor, credential=credential)
    revoke_credential(actor=actor, credential=credential)
    assert AgentCredential.objects.get(pk=credential.pk).revoked_at is not None


def test_api_authentication_closes_postgres_connection(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    closed: list[bool] = []
    monkeypatch.setattr("api.auth.authenticate_key", lambda token: credential)
    monkeypatch.setattr("api.auth.consume_api_operation", lambda value, limits: None)
    monkeypatch.setattr("api.auth.connection.vendor", "postgresql")
    monkeypatch.setattr("api.auth.connection.in_atomic_block", False)
    monkeypatch.setattr("api.auth.connection.close", lambda: closed.append(True))
    assert agent_bearer.authenticate(object(), "key") == credential
    assert closed == [True]


def test_api_authentication_maps_bad_key_and_preserves_outer_transaction(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "api.auth.authenticate_key", lambda token: (_ for _ in ()).throw(CredentialError())
    )
    with pytest.raises(InvalidCredential):
        agent_bearer.authenticate(object(), "bad")
    credential, _ = credential_factory()
    closed: list[bool] = []
    monkeypatch.setattr("api.auth.authenticate_key", lambda token: credential)
    monkeypatch.setattr("api.auth.consume_api_operation", lambda value, limits: None)
    monkeypatch.setattr("api.auth.connection.vendor", "postgresql")
    monkeypatch.setattr("api.auth.connection.in_atomic_block", True)
    monkeypatch.setattr("api.auth.connection.close", lambda: closed.append(True))
    assert agent_bearer.authenticate(object(), "key") == credential
    assert closed == []
