"""The instance has one explicit resource limit contract."""

from django.test import override_settings

from core.limits import DEFAULT_LIMITS, Limits


def test_instance_limit_values_match_the_product_contract() -> None:
    limits = Limits()
    assert limits.active_credentials == 50
    assert limits.non_retired_tunnels == 500
    assert limits.non_retired_tunnels_per_credential == 100
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
    assert limits.api_burst == 30


@override_settings(STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT=75)
def test_effective_limits_use_the_configured_credential_limit() -> None:
    from core.limits import effective_limits

    assert effective_limits().active_credentials == 75
    assert DEFAULT_LIMITS.active_credentials == 50
    with override_settings(STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT=25):
        assert effective_limits().active_credentials == 25
        assert DEFAULT_LIMITS.active_credentials == 50
