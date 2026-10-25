"""Initial and later cycles keep the same root data and separate audit actions."""

from __future__ import annotations

import pytest
from pytest_django.fixtures import Settings
from tests.factories import CredentialFactory

from core.errors import TunnelUnavailable
from core.models import AuditEvent
from tunnels.cycles import CreatedTunnel, StartedCycle, close_cycle, create_tunnel, start_cycle
from tunnels.models import Cycle, MessageMention, TunnelEvent

pytestmark = pytest.mark.django_db


def test_initial_and_later_cycle_roots_preserve_data_and_audit_attribution(
    credential_factory: CredentialFactory, settings: Settings
) -> None:
    creator, _ = credential_factory(name="Cycle creator")
    mentioned, _ = credential_factory(name="Mentioned agent")
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 60
    first = create_tunnel(
        credential=creator,
        idempotency_key="shared-cycle-create",
        root_content={"type": "text", "text": "Initial root."},
        root_mentions=[mentioned.id],
        root_correlation_id="initial-correlation",
        root_session="initial-session",
        cycle_label="First cycle",
    )
    close_cycle(
        credential=creator,
        idempotency_key="shared-cycle-close",
        address=first.address,
        expected_cycle_id=first.cycle.id,
    )
    settings.STARTUNNEL_HISTORY_RETENTION_SECONDS = 120
    second = start_cycle(
        credential=creator,
        idempotency_key="shared-cycle-start",
        address=first.address,
        expected_address_generation=1,
        root_content={"type": "text", "text": "Later root."},
        root_mentions=[mentioned.id],
        root_correlation_id="later-correlation",
        root_session="later-session",
        cycle_label="Second cycle",
    )
    cases: list[tuple[CreatedTunnel | StartedCycle, int, str, str, str, str, int]] = [
        (first, 1, "First cycle", "Initial root.", "initial-correlation", "initial-session", 60),
        (second, 2, "Second cycle", "Later root.", "later-correlation", "later-session", 120),
    ]
    for opened, number, label, text, correlation, session, retention in cases:
        cycle = Cycle.objects.get(pk=opened.cycle.id)
        assert cycle.number == number and cycle.label == label
        assert cycle.root_message_id == opened.root.id
        assert cycle.next_sequence == 2 and cycle.message_count == 1
        assert cycle.content_bytes == opened.root.byte_count
        assert cycle.retention_seconds == retention
        assert opened.root.sequence == 1 and opened.root.parent_id is None
        assert opened.root.text_payload == text
        assert opened.root.correlation_id == correlation and opened.root.session == session
        mention = MessageMention.objects.get(message=opened.root)
        assert mention.credential_id == mentioned.id and mention.display_name == mentioned.name
    actions = list(
        AuditEvent.objects.filter(action__in=["tunnel.created", "cycle.started"])
        .order_by("created_at")
        .values_list("action", "actor_credential_id", "target_id")
    )
    assert actions == [
        ("tunnel.created", creator.id, first.tunnel.id),
        ("cycle.started", creator.id, second.cycle.id),
    ]
    assert list(
        TunnelEvent.objects.filter(tunnel=first.tunnel)
        .order_by("position")
        .values_list("position", flat=True)
    ) == list(range(1, 7))


def test_later_cycle_replay_rejects_corrupt_snapshot_without_another_write(
    credential_factory: CredentialFactory,
) -> None:
    creator, _ = credential_factory()
    first = create_tunnel(
        credential=creator,
        idempotency_key="invalid-cycle-snapshot-create",
        root_content={"type": "text", "text": "Initial root."},
    )
    close_cycle(
        credential=creator,
        idempotency_key="invalid-cycle-snapshot-close",
        address=first.address,
        expected_cycle_id=first.cycle.id,
    )

    def start() -> StartedCycle:
        return start_cycle(
            credential=creator,
            idempotency_key="invalid-cycle-snapshot-start",
            address=first.address,
            expected_address_generation=1,
            root_content={"type": "text", "text": "Later root."},
        )

    second = start()
    receipt = creator.agent_receipts.get(operation="start_cycle")
    receipt.response = {**receipt.response, "event_position": False}
    receipt.save(update_fields=["response"])
    with pytest.raises(TunnelUnavailable):
        start()
    assert Cycle.objects.filter(tunnel=first.tunnel).count() == 2
    second.cycle.refresh_from_db()
    assert second.cycle.message_count == 1
