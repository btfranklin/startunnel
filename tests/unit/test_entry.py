"""Tunnel entry reads one consistent and bounded message snapshot."""

from __future__ import annotations

from typing import Any

import pytest
import time_machine
from django.utils import timezone

from tunnels.entry import enter_tunnel
from tunnels.errors import InvalidCredential, InvalidRequest, TunnelUnavailable
from tunnels.models import ActivityCheckpoint, Cycle
from tunnels.services import close_cycle, create_tunnel, post_reply, start_cycle_as_operator

pytestmark = pytest.mark.django_db


def _create(credential: Any) -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key="entry-create-0001",
        root_content={"type": "text", "text": "Root café 🜣."},
    )


def _reply(created: Any, credential: Any, key: str, text: str) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": text},
    ).message


def test_entry_returns_root_once_and_the_latest_messages(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Entry creator")
    created = _create(creator)
    replies = [
        _reply(created, creator, f"entry-reply-{index:04d}", f"Reply {index}.")
        for index in range(1, 5)
    ]

    view = enter_tunnel(credential=creator, address=created.address, limit=3)

    assert view.tunnel.id == created.tunnel.id
    assert view.cycle is not None and view.cycle.id == created.cycle.id
    assert view.root is not None and view.root.id == created.root.id
    assert [message.id for message in view.recent_messages] == [
        replies[-2].id,
        replies[-1].id,
    ]
    assert view.snapshot_sequence == 5
    assert view.activity_position == 6
    assert view.history_omitted is True
    assert view.history_upper_sequence == 3
    assert not ActivityCheckpoint.objects.filter(tunnel=created.tunnel).exists()


def test_entry_with_no_active_cycle_does_not_select_closed_history(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Dormant entry creator")
    created = _create(creator)
    close_cycle(
        credential=creator,
        idempotency_key="entry-close-0001",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )

    view = enter_tunnel(credential=creator, address=created.address)

    assert view.tunnel.state == "dormant"
    assert view.cycle is None
    assert view.root is None
    assert view.recent_messages == ()
    assert view.snapshot_sequence is None
    assert view.activity_position == 4


def test_entry_follows_a_replacement_cycle(credential_factory: Any, user_factory: Any) -> None:
    creator, _ = credential_factory(name="Replacement entry creator")
    operator = user_factory()
    created = _create(creator)
    close_cycle(
        credential=creator,
        idempotency_key="entry-replacement-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    start_cycle_as_operator(
        actor=operator,
        tunnel_id=created.tunnel.id,
        expected_address_generation=1,
        root_content={"type": "text", "text": "Replacement root."},
    )

    view = enter_tunnel(credential=creator, address=created.address)

    assert view.cycle is not None
    assert view.cycle.id != created.cycle.id
    assert view.root is not None and view.root.text_payload == "Replacement root."
    assert view.snapshot_sequence == 1
    assert view.history_omitted is False


def test_entry_hides_the_cycle_at_its_deadline_before_maintenance(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Expired entry creator")
    created = _create(creator)
    with time_machine.travel(created.cycle.expires_at, tick=False):
        view = enter_tunnel(credential=creator, address=created.address)
    assert view.cycle is None
    assert view.root is None
    assert not view.recent_messages
    assert not ActivityCheckpoint.objects.exists()


def test_entry_rejects_an_active_cycle_without_a_root(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Missing root creator")
    created = _create(creator)
    Cycle.objects.filter(pk=created.cycle.pk).update(root_message=None)
    with pytest.raises(TunnelUnavailable):
        enter_tunnel(credential=creator, address=created.address)


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 1001}, {"max_bytes": 0}, {"max_bytes": 1_048_577}],
)
def test_entry_rejects_limits_outside_instance_bounds(
    credential_factory: Any, arguments: dict[str, int]
) -> None:
    creator, _ = credential_factory(name="Bounded entry creator")
    created = _create(creator)
    with pytest.raises(InvalidRequest):
        enter_tunnel(credential=creator, address=created.address, **arguments)


def test_entry_rechecks_revocation_after_assembly(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.models import AgentCredential
    from tunnels import entry

    creator, _ = credential_factory()
    created = _create(creator)
    original_size = entry._response_wire_size

    def revoke_after_assembly(*args: Any, **kwargs: Any) -> int:
        size = original_size(*args, **kwargs)
        AgentCredential.objects.filter(pk=creator.pk).update(revoked_at=timezone.now())
        return size

    monkeypatch.setattr(entry, "_response_wire_size", revoke_after_assembly)
    with pytest.raises(InvalidCredential):
        enter_tunnel(credential=creator, address=created.address)


def test_entry_keeps_its_snapshot_without_repeated_tunnel_queries(
    credential_factory: Any, django_assert_num_queries: Any
) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    with django_assert_num_queries(11):
        view = enter_tunnel(credential=creator, address=created.address)
    assert view.root is not None and view.root.pk == created.root.pk
