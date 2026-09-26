"""The instance has one explicit resource limit contract."""

from core.limits import InstanceLimitProvider, Limits


def test_instance_limit_values_match_the_product_contract() -> None:
    limits = Limits()
    assert limits.active_credentials == 50
    assert limits.non_retired_tunnels == 500
    assert limits.messages_per_cycle == 10_000
    assert limits.bytes_per_message == 65_536
    assert limits.content_bytes_per_cycle == 67_108_864
    assert limits.maximum_tree_depth == 128
    assert limits.mentions_per_message == 32
    assert limits.default_list_page == 100
    assert limits.maximum_list_page == 1_000
    assert limits.maximum_long_poll_seconds == 20
    assert limits.maximum_context_bytes == 1_048_576
    assert limits.maximum_search_results == 100
    assert limits.maximum_search_query_characters == 512
    assert limits.maximum_search_response_bytes == 1_048_576
    assert limits.search_statement_timeout_milliseconds == 500
    assert limits.api_operations_per_minute == 120
    assert limits.tunnel_creations_per_hour == 300
    assert limits.address_misses_per_minute == 30
    assert limits.api_burst == 30


def test_instance_limit_provider_returns_instance_limits() -> None:
    provider = InstanceLimitProvider()
    assert provider.for_instance() == Limits()
