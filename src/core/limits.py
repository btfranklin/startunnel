"""Resource limits and effective access for this instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Limits:
    active_credentials: int = 50
    non_retired_tunnels: int = 500
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
    cursor_lifetime_seconds: int = 3_600
    api_operations_per_minute: int = 120
    api_burst: int = 30
    tunnel_creations_per_hour: int = 300
    address_misses_per_minute: int = 30


class LimitProvider(Protocol):
    def for_instance(self) -> Limits: ...


class InstanceLimitProvider:
    def for_instance(self) -> Limits:
        return Limits()


provider: LimitProvider = InstanceLimitProvider()
