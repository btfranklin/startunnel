"""Bearer authentication for agent-only API routes."""

from __future__ import annotations

from django.db import connection
from ninja.security import HttpBearer

from agents.models import AgentCredential
from agents.services import CredentialError, authenticate_key
from core import limits as instance_limits
from core.rate_limits import consume_api_operation
from tunnels.errors import InvalidCredential


class AgentBearer(HttpBearer):
    def __init__(self, *, release_before_wait: bool = False) -> None:
        super().__init__()
        self.release_before_wait = release_before_wait

    def authenticate(self, request: object, token: str) -> AgentCredential:
        try:
            try:
                credential = authenticate_key(token)
            except CredentialError as error:
                raise InvalidCredential() from error
            limits = instance_limits.effective_limits()
            consume_api_operation(credential, limits)
            return credential
        finally:
            # The async activity worker uses its own database connection.
            if (
                self.release_before_wait
                and connection.vendor == "postgresql"
                and not connection.in_atomic_block
            ):
                connection.close()


agent_bearer = AgentBearer()
activity_bearer = AgentBearer(release_before_wait=True)
