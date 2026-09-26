"""Verify the current schema from a fresh PostgreSQL installation."""

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_initial_schema_has_current_models_and_guards() -> None:
    assert connection.vendor == "postgresql"
    leaves = dict(MigrationLoader(connection).graph.leaf_nodes())
    assert {name: leaves[name] for name in ("accounts", "agents", "core", "tunnels")} == {
        "accounts": "0001_initial",
        "agents": "0001_initial",
        "core": "0001_initial",
        "tunnels": "0001_initial",
    }

    tables = set(connection.introspection.table_names())
    assert {
        "accounts_user",
        "agents_agentcredential",
        "core_maintenancestate",
        "core_ratelimitbucket",
        "tunnels_tunnel",
        "tunnels_tunneladdress",
        "tunnels_cycle",
        "tunnels_message",
        "tunnels_tunnelevent",
        "tunnels_activitycheckpoint",
    } <= tables

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 'startunnel_%' AND NOT tgisinternal"
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.tunnels_message'::regclass"
        )
        constraints = {row[0] for row in cursor.fetchall()}

    assert {
        "startunnel_tunnel_address_guard",
        "startunnel_message_tree_guard",
        "startunnel_message_immutable",
        "startunnel_event_reference_guard",
        "startunnel_event_immutable",
        "startunnel_checkpoint_guard",
        "startunnel_cycle_root_from_cycle",
        "startunnel_cycle_root_from_message",
        "startunnel_lifecycle_from_tunnel",
        "startunnel_lifecycle_from_address",
        "startunnel_lifecycle_from_cycle",
    } <= triggers
    assert {
        "message_payload_actual_size_at_most_64k",
        "message_content_digest_is_sha256",
    } <= constraints
