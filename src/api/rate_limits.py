"""Rate limit rules for agent API operations and browser authentication."""

from __future__ import annotations

import hashlib

from agents.models import AgentCredential
from core.limits import Limits
from core.rate_limits import _consume


def consume_api_operation(credential: AgentCredential, limits: Limits) -> None:
    _consume(
        key=f"api:{credential.id}",
        limit=limits.api_operations_per_minute,
        window_seconds=60,
        rule="API operation",
        burst_limit=limits.api_burst,
    )


def consume_login_attempt(*, source_ip: str, username: str) -> None:
    source_digest = hashlib.sha256(source_ip.encode()).hexdigest()
    username_digest = hashlib.sha256(username.casefold().encode()).hexdigest()
    _consume(
        key=f"login:{source_digest}:{username_digest}",
        limit=10,
        window_seconds=300,
        rule="login attempt",
    )
