"""Resource limits and effective access for this instance."""

from __future__ import annotations

from dataclasses import dataclass, replace

from django.conf import settings

MAX_CREDENTIAL_NAME_CHARACTERS = 100
MAX_LABEL_CHARACTERS = 160
MAX_SESSION_CHARACTERS = 32
MAX_CORRELATION_ID_CHARACTERS = 128
MAX_CONTEXT_REFERENCES = 100
MIN_IDEMPOTENCY_KEY_CHARACTERS = 8
MAX_IDEMPOTENCY_KEY_CHARACTERS = 128
MIN_CYCLE_SECONDS = 300
MAX_CYCLE_SECONDS = 604_800
MAX_BEACON_CHARACTERS = 280
MAX_BEACON_SECONDS = 86_400
DEFAULT_BOARD_LIMIT = 20
MAX_BOARD_LIMIT = 100
MAX_TUNNEL_RETIREMENTS = 500


@dataclass(frozen=True, slots=True)
class Limits:
    active_credentials: int = 50
    non_retired_tunnels: int = 500
    non_retired_tunnels_per_credential: int = 100
    messages_per_cycle: int = 10_000
    bytes_per_message: int = 65_536
    content_bytes_per_cycle: int = 67_108_864
    maximum_tree_depth: int = 128
    mentions_per_message: int = 32
    default_list_page: int = 100
    maximum_list_page: int = 1_000
    maximum_long_poll_seconds: int = 20
    maximum_context_items: int = 200
    maximum_context_bytes: int = 1_048_576
    maximum_search_results: int = 100
    maximum_search_query_characters: int = 512
    maximum_search_response_bytes: int = 1_048_576
    search_statement_timeout_milliseconds: int = 500
    api_operations_per_minute: int = 120
    api_burst: int = 30


DEFAULT_LIMITS = Limits()


def effective_limits() -> Limits:
    """Return the effective resource limits for this instance."""

    return replace(DEFAULT_LIMITS, active_credentials=settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT)
