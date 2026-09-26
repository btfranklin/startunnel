"""Critical instance-tree branches reject invalid state and race outcomes."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from core.limits import Limits
from tunnels.access import cycle_is_readable, resolve_tunnel
from tunnels.errors import (
    CycleUnavailable,
    InvalidCredential,
    InvalidRequest,
    LifecycleConflict,
    QuotaExceeded,
    TunnelUnavailable,
)
from tunnels.models import Cycle, Message, Tunnel
from tunnels.services import (
    _close_locked_cycle,
    close_cycle,
    close_cycle_as_operator,
    create_tunnel,
    get_message,
    get_tunnel,
    list_replies,
    post_reply,
    read_tree,
    rollover_cycle_as_operator,
    start_cycle_as_operator,
)

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Inspect this branch."},
    )


def test_create_rejects_inactive_credential(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    credential.revoked_at = timezone.now()
    credential.save(update_fields=["revoked_at"])
    with pytest.raises(InvalidCredential):
        _create(credential, "critical-inactive")


def test_create_rejects_long_correlation_and_instance_quota(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    with pytest.raises(InvalidRequest, match="correlation_id"):
        create_tunnel(
            credential=credential,
            idempotency_key="critical-correlation",
            root_content={"type": "text", "text": "Root."},
            root_correlation_id="x" * 129,
        )
    monkeypatch.setattr(
        "tunnels.services.limits_for",
        lambda value: replace(Limits(), non_retired_tunnels=0),
    )
    with pytest.raises(QuotaExceeded):
        _create(credential, "critical-quota")


def test_create_replay_rejects_missing_resources(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-create-replay")
    record = credential.idempotency_records.get(operation="create_tunnel")
    record.resource_id = None
    record.save(update_fields=["resource_id"])
    with pytest.raises(TunnelUnavailable):
        _create(credential, "critical-create-replay")
    record.resource_id = created.tunnel.id
    record.save(update_fields=["resource_id"])
    Cycle.objects.filter(pk=created.cycle.pk).update(root_message=None)
    with pytest.raises(TunnelUnavailable):
        _create(credential, "critical-create-replay")


def test_resolve_exact_fallback_and_locked_address_race(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-resolve")
    monkeypatch.setattr(
        "tunnels.access._resolve_current_tunnel_if_authorized", lambda **kwargs: None
    )
    assert resolve_tunnel(credential=credential, address=created.address).id == created.tunnel.id
    assert (
        resolve_tunnel(credential=credential, address=created.address, for_update=True).id
        == created.tunnel.id
    )

    from tunnels import access

    original = access.address_query
    calls = 0

    def disappearing(*, digest: bytes) -> Any:
        nonlocal calls
        calls += 1
        query = original(digest=digest)
        return query if calls == 1 else query.none()

    monkeypatch.setattr(access, "address_query", disappearing)
    with pytest.raises(TunnelUnavailable):
        resolve_tunnel(credential=credential, address=created.address, for_update=True)


def test_fast_locked_resolve_rejects_address_removed_after_lookup(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-fast-resolve")
    from tunnels import access

    original = access.address_query
    monkeypatch.setattr(
        access,
        "_resolve_current_tunnel_if_authorized",
        lambda **kwargs: created.tunnel,
    )
    monkeypatch.setattr(
        access,
        "address_query",
        lambda *, digest: original(digest=digest).none(),
    )
    with pytest.raises(TunnelUnavailable):
        resolve_tunnel(credential=credential, address=created.address, for_update=True)


@pytest.mark.parametrize("for_update", [False, True])
def test_resolve_rejects_retired_tunnel(credential_factory: Any, for_update: bool) -> None:
    credential, _ = credential_factory()
    created = _create(credential, f"critical-retired-{for_update}")
    Tunnel.objects.filter(pk=created.tunnel.pk).update(
        state=Tunnel.State.RETIRED, retired_at=timezone.now()
    )
    with pytest.raises(TunnelUnavailable):
        resolve_tunnel(
            credential=credential,
            address=created.address,
            for_update=for_update,
        )


def test_post_reply_replay_rejects_missing_resource(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-reply-replay")
    reply = post_reply(
        credential=credential,
        idempotency_key="critical-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Reply."},
    )
    record = credential.idempotency_records.get(operation="post_reply")
    record.resource_id = None
    record.save(update_fields=["resource_id"])
    with pytest.raises(TunnelUnavailable):
        post_reply(
            credential=credential,
            idempotency_key="critical-reply",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
        )
    record.resource_id = uuid4()
    record.save(update_fields=["resource_id"])
    with pytest.raises(TunnelUnavailable):
        post_reply(
            credential=credential,
            idempotency_key="critical-reply",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
        )
    assert Message.objects.filter(pk=reply.message.pk).exists()


def test_post_reply_rejects_correlation_and_cycle_limits(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-reply-limits")
    with pytest.raises(InvalidRequest, match="correlation_id"):
        post_reply(
            credential=credential,
            idempotency_key="critical-long-correlation",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
            correlation_id="x" * 129,
        )
    monkeypatch.setattr(
        "tunnels.services.limits_for",
        lambda value: replace(Limits(), maximum_tree_depth=0),
    )
    with pytest.raises(QuotaExceeded, match="depth"):
        post_reply(
            credential=credential,
            idempotency_key="critical-depth",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
        )
    monkeypatch.setattr(
        "tunnels.services.limits_for",
        lambda value: replace(Limits(), messages_per_cycle=1),
    )
    with pytest.raises(QuotaExceeded, match="message limit"):
        post_reply(
            credential=credential,
            idempotency_key="critical-message-count",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
        )
    monkeypatch.setattr(
        "tunnels.services.limits_for",
        lambda value: replace(Limits(), content_bytes_per_cycle=1),
    )
    with pytest.raises(QuotaExceeded, match="content-byte"):
        post_reply(
            credential=credential,
            idempotency_key="critical-content-bytes",
            address=created.address,
            parent_id=created.root.id,
            content={"type": "text", "text": "Reply."},
        )


def test_tree_message_and_reply_validation(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-read-validation")
    with pytest.raises(InvalidRequest, match="limit"):
        read_tree(credential=credential, address=created.address, limit=0)
    with pytest.raises(InvalidRequest, match="negative"):
        read_tree(credential=credential, address=created.address, after_sequence=-1)
    with pytest.raises(InvalidRequest, match="snapshot"):
        read_tree(credential=credential, address=created.address, snapshot_sequence=2)
    with pytest.raises(CycleUnavailable):
        get_message(
            credential=credential,
            address=created.address,
            cycle_id=created.cycle.id,
            message_id=uuid4(),
        )
    with pytest.raises(InvalidRequest, match="limit"):
        list_replies(
            credential=credential,
            address=created.address,
            message_id=created.root.id,
            limit=0,
        )


def test_close_conflict_replay_race_and_inactive_noop(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, "critical-close")
    with pytest.raises(LifecycleConflict):
        close_cycle(
            credential=credential,
            idempotency_key="critical-close-conflict",
            address=created.address,
            expected_cycle_id=uuid4(),
        )
    monkeypatch.setattr("tunnels.services.find_idempotency_replay", lambda **kwargs: None)
    monkeypatch.setattr(
        "tunnels.services.begin_idempotency",
        lambda **kwargs: SimpleNamespace(replay=True, record=SimpleNamespace()),
    )
    close_cycle(
        credential=credential,
        idempotency_key="critical-close-race",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.state = Cycle.State.CLOSED
    initial = created.tunnel.next_event_position
    _close_locked_cycle(tunnel=created.tunnel, cycle=created.cycle, reason="test")
    created.tunnel.refresh_from_db()
    assert created.tunnel.next_event_position == initial


def test_cycle_readability_rejects_deleted_and_unknown_state() -> None:
    assert not cycle_is_readable(Cycle(state=Cycle.State.DELETED))
    assert not cycle_is_readable(Cycle(state="unknown"))


def test_operator_can_close_start_and_roll_over_instance_cycle(
    credential_factory: Any, user_factory: Any
) -> None:
    credential, _ = credential_factory()
    actor = user_factory()
    created = _create(credential, "critical-operator-lifecycle")
    assert get_tunnel(credential=credential, tunnel_id=created.tunnel.id) == created.tunnel
    with pytest.raises(TunnelUnavailable):
        get_tunnel(credential=credential, tunnel_id=uuid4())
    with pytest.raises(LifecycleConflict):
        close_cycle_as_operator(
            actor=actor,
            tunnel_id=created.tunnel.id,
            expected_cycle_id=uuid4(),
        )
    close_cycle_as_operator(
        actor=actor,
        tunnel_id=created.tunnel.id,
        expected_cycle_id=created.cycle.id,
    )
    started = start_cycle_as_operator(
        actor=actor,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        root_content={"type": "text", "text": "Second cycle."},
    )
    rolled = rollover_cycle_as_operator(
        actor=actor,
        tunnel_id=created.tunnel.id,
        expected_cycle_id=started.cycle.id,
        expected_address_generation=1,
        root_content={"type": "text", "text": "Third cycle."},
    )
    assert rolled.cycle.number == 3


def test_inactive_operator_cannot_control_or_find_tunnel(
    credential_factory: Any, user_factory: Any
) -> None:
    credential, _ = credential_factory()
    actor = user_factory()
    created = _create(credential, "critical-inactive-operator")
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    with pytest.raises(TunnelUnavailable):
        close_cycle_as_operator(
            actor=actor,
            tunnel_id=created.tunnel.id,
            expected_cycle_id=created.cycle.id,
        )
    with pytest.raises(TunnelUnavailable):
        start_cycle_as_operator(
            actor=actor,
            tunnel_id=created.tunnel.id,
            expected_address_generation=1,
            root_content={"type": "text", "text": "No access."},
        )
