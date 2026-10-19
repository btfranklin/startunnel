"""The database can use indexes for Beacon responder and receipt cleanup probes."""

from typing import Any
from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.db.models import Count

from accounts.models import AdminReceipt
from core.models import AgentReceipt
from tunnels.models import Message

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_beacon_lookup_indexes_match_the_actual_query_expressions() -> None:
    cycle_id, sender_id, resource_id = uuid4(), uuid4(), uuid4()
    probes = [
        (
            Message.objects.filter(cycle_id=cycle_id, sender_id=sender_id, sequence__gt=2)
            .filter(parent__isnull=False)
            .values("cycle_id")
            .annotate(total=Count("sender_id", distinct=True)),
            "message_beacon_responders",
        ),
        (AgentReceipt.objects.filter(resource_id=resource_id), "resource_id"),
        (
            AdminReceipt.objects.filter(
                operation="beacons.withdraw", response__resource__id__in=[str(resource_id)]
            ),
            "admin_beacon_receipt_resource",
        ),
    ]
    with transaction.atomic(), connection.cursor() as cursor:
        # Small empty test tables favor scans. Check index eligibility explicitly.
        cursor.execute("SET LOCAL enable_seqscan = off")
        for query, expected_index in probes:
            plan = query.explain(format="json")
            assert expected_index in plan, plan


def test_beacon_indexes_exist_on_their_owned_tables() -> None:
    expected = {
        Message._meta.db_table: "message_beacon_responders",
        AdminReceipt._meta.db_table: "admin_beacon_receipt_resource",
    }
    with connection.cursor() as cursor:
        for table, index in expected.items():
            constraints: dict[str, Any] = connection.introspection.get_constraints(cursor, table)
            assert constraints[index]["index"]
        receipt_indexes = connection.introspection.get_constraints(
            cursor, AgentReceipt._meta.db_table
        )
    assert any(
        item["index"] and item["columns"] == ["resource_id"] for item in receipt_indexes.values()
    )
