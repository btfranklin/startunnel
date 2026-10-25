"""Typed call contracts for shared test factories."""

from typing import Protocol

from accounts.models import User
from agents.models import AgentCredential


class UserFactory(Protocol):
    def __call__(
        self, *, username: str | None = None, password: str = "test-password-42"
    ) -> User: ...


class CredentialFactory(Protocol):
    def __call__(
        self, *, user: User | None = None, name: str = "Test agent"
    ) -> tuple[AgentCredential, str]: ...
