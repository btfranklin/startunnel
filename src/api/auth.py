"""Bearer authentication for agent-only API routes."""

from __future__ import annotations

from django.db import connection
from ninja.security import HttpBearer

from agents.models import AgentCredential
from agents.services import CredentialError, authenticate_key
from core.limits import provider
from tunnels.errors import InvalidCredential

from .rate_limits import consume_api_operation


def _return_postgresql_connection_before_wait() -> None:
    """Return a production connection without breaking an outer test transaction."""

    if connection.vendor == "postgresql" and not connection.in_atomic_block:
        connection.close()


class AgentBearer(HttpBearer):
    def authenticate(self, request: object, token: str) -> AgentCredential:
        try:
            try:
                credential = authenticate_key(token)
            except CredentialError as error:
                raise InvalidCredential() from error
            limits = provider.for_instance()
            consume_api_operation(credential, limits)
            return credential
        finally:
            # Return the authentication connection before a bounded wait begins.
            _return_postgresql_connection_before_wait()


agent_bearer = AgentBearer()
