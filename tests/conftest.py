"""Shared tests use safe local credentials and no external services."""

from __future__ import annotations

import base64
import hmac
import secrets
from collections.abc import Callable

import pytest
from django.conf import settings

from accounts.models import User
from agents.models import AgentCredential


@pytest.fixture
def user_factory(db: object) -> Callable[..., User]:
    def create(*, username: str | None = None, password: str = "test-password-42") -> User:
        user = User.objects.create_user(
            username=username or f"user-{secrets.token_hex(4)}",
            password=password,
        )
        return user

    return create


@pytest.fixture
def credential_factory(
    db: object, user_factory: Callable[..., User]
) -> Callable[..., tuple[AgentCredential, str]]:
    def create(
        *, user: User | None = None, name: str = "Test agent"
    ) -> tuple[AgentCredential, str]:
        key = "st_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        digest = hmac.digest(settings.API_KEY_PEPPER.encode(), key.encode(), "sha256")
        credential = AgentCredential.objects.create(
            created_by=user or user_factory(),
            name=name,
            display_prefix=key[:11],
            key_digest=digest,
        )
        return credential, key

    return create
