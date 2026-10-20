"""Shared tests use safe local credentials and no external services."""

from __future__ import annotations

import base64
import hmac
import re
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from django.conf import settings

from accounts.models import User
from agents.models import AgentCredential


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
        sqlite_only = [item for item in items if item.get_closest_marker("sqlite_only") is not None]
        if sqlite_only:
            items[:] = [item for item in items if item not in sqlite_only]
            config.hook.pytest_deselected(items=sqlite_only)
        return
    postgres = [item for item in items if item.get_closest_marker("postgres") is not None]
    if not postgres:
        return
    if re.search(r"\bpostgres\b", config.option.markexpr):
        raise pytest.UsageError(
            "The postgres marker requires TEST_DATABASE_URL with PostgreSQL. "
            "Run: pdm run python scripts/run_isolated_test_lane.py postgres"
        )
    items[:] = [item for item in items if item not in postgres]
    config.hook.pytest_deselected(items=postgres)


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


@pytest.fixture
def executable_dir() -> Iterator[Path]:
    """Keep executable fixtures outside the container's noexec /tmp mount."""
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="executable-test-", dir=artifacts) as directory:
        yield Path(directory)
