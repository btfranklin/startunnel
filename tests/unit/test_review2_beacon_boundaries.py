"""Beacon reads and writes enforce clock and credential boundaries."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.utils import timezone

import tunnels.beacons as beacons
from accounts.models import User
from core.errors import CycleClosed, InvalidCredential, LifecycleConflict, TunnelUnavailable
from core.models import AuditEvent
from tunnels.access import credential_is_active
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.models import Beacon, BoardState, TunnelEvent

pytestmark = pytest.mark.django_db


def test_publication_that_reaches_cycle_expiry_rolls_back(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="deadline-publication-create",
        root_content={"type": "text", "text": "Review"},
    )
    monkeypatch.setattr(timezone, "now", lambda: created.cycle.expires_at)
    with pytest.raises(CycleClosed):
        beacons.publish_beacon(
            credential=creator,
            address=created.address,
            idempotency_key="deadline-publication",
            text="Review this",
        )
    assert not Beacon.objects.exists()
    assert not BoardState.objects.exists()


def test_closed_cycle_rejects_publication(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="closed-publication-create",
        root_content={"type": "text", "text": "Review"},
    )
    close_cycle(
        credential=creator,
        address=created.address,
        idempotency_key="close-before-publish",
        expected_cycle_id=created.cycle.id,
    )
    with pytest.raises(CycleClosed):
        beacons.publish_beacon(
            credential=creator,
            address=created.address,
            idempotency_key="closed-publication",
            text="Review this",
        )


def test_expired_beacon_withdrawal_and_admin_end_reason(
    credential_factory: Any, user_factory: Any
) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="expired-withdraw-create",
        root_content={"type": "text", "text": "Review"},
    )
    published, _ = beacons.publish_beacon(
        credential=creator,
        address=created.address,
        idempotency_key="expired-withdraw-publish",
        text="Review this",
    )
    expiry = timezone.now() - timedelta(seconds=1)
    Beacon.objects.filter(pk=published["id"]).update(expires_at=expiry)
    with pytest.raises(LifecycleConflict):
        beacons.withdraw_beacon(
            credential=creator,
            address=created.address,
            idempotency_key="expired-withdraw",
            beacon_id=UUID(published["id"]),
        )
    ended = beacons.withdraw_beacon_as_admin(actor=user_factory(), beacon_id=UUID(published["id"]))
    assert ended["end_reason"] == Beacon.EndReason.EXPIRED
    assert Beacon.objects.get(pk=published["id"]).ended_at == expiry
    with pytest.raises(LifecycleConflict):
        beacons.withdraw_beacon_as_admin(actor=user_factory(), beacon_id=UUID(published["id"]))


def test_admin_withdrawal_rejects_missing_beacon(user_factory: Any) -> None:
    actor = user_factory()
    with pytest.raises(TunnelUnavailable):
        beacons.withdraw_beacon_as_admin(actor=actor, beacon_id=uuid4())


@pytest.mark.parametrize("active", [False, True])
def test_admin_withdrawal_checks_current_actor_state_for_a_live_beacon(
    active: bool,
    user_factory: Any,
    credential_factory: Any,
) -> None:
    actor = user_factory()
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="admin-authority-create",
        root_content={"type": "text", "text": "Check current authority"},
    )
    published, _ = beacons.publish_beacon(
        credential=creator,
        address=created.address,
        idempotency_key="admin-authority-publish",
        text="Review this",
    )
    beacon = Beacon.objects.get(pk=published["id"])
    position = BoardState.objects.get(pk=1).position
    audit_ids = set(AuditEvent.objects.values_list("id", flat=True))
    event_ids = set(TunnelEvent.objects.values_list("id", flat=True))
    assert beacon.ended_at is None
    assert beacon.expires_at > timezone.now()
    if not active:
        User.objects.filter(pk=actor.pk).update(is_active=False)
    assert actor.is_active
    assert User.objects.filter(pk=actor.pk, is_active=active).exists()

    if active:
        ended = beacons.withdraw_beacon_as_admin(actor=actor, beacon_id=beacon.id)
        beacon.refresh_from_db()
        assert ended["end_reason"] == Beacon.EndReason.WITHDRAWN
        assert beacon.ended_at is not None
        assert BoardState.objects.get(pk=1).position == position + 1
        assert AuditEvent.objects.exclude(pk__in=audit_ids).get().actor_user_id == actor.id
        assert TunnelEvent.objects.exclude(pk__in=event_ids).count() == 1
    else:
        with pytest.raises(TunnelUnavailable):
            beacons.withdraw_beacon_as_admin(actor=actor, beacon_id=beacon.id)
        beacon.refresh_from_db()
        assert beacon.ended_at is None
        assert beacon.end_reason == ""
        assert BoardState.objects.get(pk=1).position == position
        assert set(AuditEvent.objects.values_list("id", flat=True)) == audit_ids
        assert set(TunnelEvent.objects.values_list("id", flat=True)) == event_ids


def test_board_rechecks_credential_before_returning_content(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, _ = credential_factory()
    real_check = credential_is_active
    calls = 0

    def revoke_before_final_check(credential: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            type(credential).objects.filter(pk=credential.pk).update(revoked_at=timezone.now())
        return real_check(credential)

    monkeypatch.setattr(beacons, "credential_is_active", revoke_before_final_check)
    with pytest.raises(InvalidCredential):
        beacons.read_board(credential=reader)
    assert calls == 2
    with pytest.raises(InvalidCredential):
        beacons.read_board(credential=reader)
