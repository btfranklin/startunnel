"""The database can use indexes for Beacon responder and receipt cleanup probes."""

from typing import Any
from uuid import uuid4

import pytest
from django.db import connection, transaction

from accounts.models import AdminReceipt
from core.models import AgentReceipt
from tunnels.cycles import create_tunnel
from tunnels.models import Cycle, Message

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_beacon_lookup_indexes_match_the_actual_query_expressions(
    credential_factory: Any,
) -> None:
    publisher, _ = credential_factory(name="Index publisher")
    responder, _ = credential_factory(name="Index responder")
    replies = []
    for number in range(24):
        created = create_tunnel(
            credential=publisher,
            idempotency_key=f"beacon-index-cycle-{number:02d}",
            root_content={"type": "text", "text": "x"},
        )
        for sequence in range(2, 129):
            sender = responder if sequence % 16 == 0 else publisher
            replies.append(
                Message(
                    tunnel=created.tunnel,
                    cycle=created.cycle,
                    parent=created.root,
                    sender=sender,
                    sender_name=sender.name,
                    sequence=sequence,
                    depth=1,
                    payload_type=Message.PayloadType.TEXT,
                    text_payload="x",
                    byte_count=1,
                )
            )
    Message.objects.bulk_create(replies, batch_size=500)
    Cycle.objects.update(message_count=128, next_sequence=129, content_bytes=128)
    current = replies[-1]
    resource_id = uuid4()
    # Match note_beacon_response's earlier-response probe. The sender occurs in
    # many cycles, while only a few of its replies follow this publication.
    earlier_responses = (
        Message.objects.filter(
            cycle_id=current.cycle_id,
            sender_id=responder.pk,
            sequence__gt=64,
            parent__isnull=False,
        )
        .exclude(pk=current.pk)
        .order_by()
        .values("pk")[:1]
    )
    probes = [
        (earlier_responses, "message_beacon_responders"),
        (AgentReceipt.objects.filter(resource_id=resource_id), "resource_id"),
        (
            AdminReceipt.objects.filter(
                operation="beacons.withdraw", response__resource__id__in=[str(resource_id)]
            ),
            "admin_beacon_receipt_resource",
        ),
    ]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("ANALYZE tunnels_message")
        # Receipt expression probes need no fixture rows; check their eligibility.
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
