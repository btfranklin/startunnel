"""Activity waits use durable events and process-local PostgreSQL wakeups."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from agents.models import AgentCredential
from core.activity_listener import ActivityListener
from core.limits import Limits
from tunnels.activity import (
    ActivityPage,
    _ActivityStart,
    _begin_activity_wait_for_async,
    _confirm_activity_page_for_async,
    _continue_activity_wait_for_async,
    _limits_for_async,
    commit_checkpoint,
    read_activity,
    read_activity_async,
    read_activity_once,
)
from tunnels.errors import (
    CheckpointRegression,
    InvalidCredential,
    InvalidRequest,
    TunnelUnavailable,
)
from tunnels.models import ActivityCheckpoint, Tunnel, TunnelEvent
from tunnels.services import create_tunnel, post_reply, rotate_address_as_operator

pytestmark = pytest.mark.django_db


def _create(credential: Any, key: str = "activity-create") -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Coordinate this review."},
    )


def _reply(created: Any, credential: Any, key: str = "activity-reply") -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "One new result."},
    )


def test_immediate_activity_orders_pages_and_advances_filtered_cursor(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator)
    page = read_activity_once(credential=creator, address=created.address, limit=1)
    assert [event.position for event in page.events] == [1]
    assert page.has_more and page.next_position == 1
    filtered = read_activity_once(
        credential=creator,
        address=created.address,
        event_types=(TunnelEvent.Type.CYCLE_CLOSED,),
    )
    assert filtered.events == ()
    assert filtered.next_position == filtered.high_water_position == 2


def test_activity_validation_rejects_bad_positions_limits_and_types(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "activity-validation")
    with pytest.raises(InvalidRequest, match="negative"):
        read_activity_once(credential=creator, address=created.address, after_position=-1)
    with pytest.raises(InvalidRequest, match="limit"):
        read_activity_once(credential=creator, address=created.address, limit=0)
    with pytest.raises(InvalidRequest, match="event type"):
        read_activity_once(credential=creator, address=created.address, event_types=("bad",))
    with pytest.raises(InvalidRequest, match="not valid"):
        read_activity_once(credential=creator, address=created.address, after_position=3)
    with pytest.raises(InvalidRequest, match="wait_seconds"):
        read_activity(credential=creator, address=created.address, wait_seconds=-1)


def test_checkpoint_resume_exact_forward_and_regression(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator, "activity-checkpoint")
    checkpoint, advanced = commit_checkpoint(credential=reader, address=created.address, position=2)
    assert advanced
    _reply(created, creator, "activity-checkpoint-reply")
    page = read_activity_once(credential=reader, address=created.address)
    assert [event.position for event in page.events] == [3]
    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == 2
    assert commit_checkpoint(credential=reader, address=created.address, position=2)[1] is False
    assert commit_checkpoint(credential=reader, address=created.address, position=3)[1] is True
    with pytest.raises(CheckpointRegression):
        commit_checkpoint(credential=reader, address=created.address, position=2)


def test_checkpoint_validation(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = _create(creator, "activity-checkpoint-validation")
    with pytest.raises(InvalidRequest, match="negative"):
        commit_checkpoint(credential=creator, address=created.address, position=-1)
    with pytest.raises(InvalidRequest, match="cursor"):
        commit_checkpoint(
            credential=creator,
            address=created.address,
            position=1,
            expected_tunnel_id=uuid4(),
        )
    Tunnel.objects.filter(pk=created.tunnel.pk).update(next_event_position=5)
    with pytest.raises(InvalidRequest, match="committed event"):
        commit_checkpoint(credential=creator, address=created.address, position=4)
    assert not ActivityCheckpoint.objects.filter(last_event_position=4).exists()


def test_sync_wait_uses_bounded_database_fallback(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator, "activity-sync-wait")
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        _reply(created, creator, "activity-sync-wait-reply")

    monkeypatch.setattr("tunnels.activity.time.sleep", sleep)
    page = read_activity(
        credential=reader,
        address=created.address,
        after_position=2,
        wait_seconds=1,
    )
    assert [event.position for event in page.events] == [3]
    assert sleeps and sleeps[0] <= 1


def test_mentions_are_prefetched_only_when_needed(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    mentioned, _ = credential_factory()
    created = _create(creator, "activity-mentions")
    post_reply(
        credential=creator,
        idempotency_key="activity-mentioned-reply",
        address=created.address,
        parent_id=created.root.id,
        content={"type": "text", "text": "Please review."},
        mentions=[mentioned.id],
    )
    page = read_activity_once(credential=mentioned, address=created.address, after_position=2)
    message = page.events[0].message
    assert message is not None
    assert [item.display_name for item in message.mentions.all()] == [mentioned.name]


def test_async_helpers_revalidate_revocation_and_address_rotation(
    credential_factory: Any, user_factory: Any
) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator, "activity-revalidate")
    _reply(created, creator, "activity-revalidate-reply")
    page = _continue_activity_wait_for_async(
        tunnel=created.tunnel,
        credential=reader,
        address=created.address,
        after_position=2,
        limit=100,
        event_types=None,
    )
    assert page.events and page.authorization_confirmed
    reader.revoked_at = created.tunnel.created_at
    reader.save(update_fields=["revoked_at"])
    revoked = _continue_activity_wait_for_async(
        tunnel=created.tunnel,
        credential=reader,
        address=created.address,
        after_position=2,
        limit=100,
        event_types=None,
    )
    assert revoked.events == () and not revoked.authorization_confirmed
    with pytest.raises(InvalidCredential):
        _confirm_activity_page_for_async(page=revoked, credential=reader, address=created.address)

    actor = user_factory()
    rotate_address_as_operator(
        actor=actor, tunnel_id=created.tunnel.id, expected_address_generation=1
    )
    with pytest.raises(TunnelUnavailable):
        _confirm_activity_page_for_async(page=page, credential=creator, address=created.address)


def test_async_wait_start_uses_checkpoint_and_validates_inputs(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    reader, _ = credential_factory()
    created = _create(creator, "activity-async-start")
    commit_checkpoint(credential=reader, address=created.address, position=2)
    start = _begin_activity_wait_for_async(
        credential=reader,
        address=created.address,
        after_position=None,
        limit=100,
        event_types=None,
    )
    assert start.position == start.high_water_position == 2
    with pytest.raises(InvalidRequest, match="event type"):
        _begin_activity_wait_for_async(
            credential=reader,
            address=created.address,
            after_position=2,
            limit=100,
            event_types=("bad",),
        )


@pytest.mark.asyncio
async def test_async_wait_registers_before_read_and_returns_notified_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = AgentCredential(id=uuid4(), name="Reader")
    tunnel = Tunnel(id=uuid4())
    event = TunnelEvent(position=3)
    empty = ActivityPage(tunnel, (), 2, 2, 2, False)
    ready = ActivityPage(tunnel, (event,), 2, 3, 3, False, True)
    pages = [empty, ready]
    registered: list[Any] = []
    returned_connections: list[bool] = []
    monkeypatch.setattr(
        "tunnels.activity.connection",
        SimpleNamespace(
            vendor="postgresql",
            in_atomic_block=False,
            close=lambda: returned_connections.append(True),
        ),
    )

    class Listener:
        def register(self, tunnel_id: Any) -> asyncio.Event:
            registered.append(tunnel_id)
            return asyncio.Event()

        async def wait(self, waiter: asyncio.Event, timeout: float) -> None:
            assert registered and timeout > 0

        def unregister(self, tunnel_id: Any, waiter: asyncio.Event) -> None:
            registered.append((tunnel_id, waiter))

    monkeypatch.setattr("tunnels.activity.activity_listener", Listener())
    monkeypatch.setattr(
        "tunnels.activity.limits_for", lambda _: SimpleNamespace(maximum_long_poll_seconds=20)
    )
    monkeypatch.setattr(
        "tunnels.activity._begin_activity_wait_for_async",
        lambda **kwargs: _ActivityStart(tunnel=tunnel, position=2, high_water_position=2),
    )
    monkeypatch.setattr(
        "tunnels.activity._continue_activity_wait_for_async", lambda **kwargs: pages.pop(0)
    )
    page = await read_activity_async(
        credential=credential,
        address="address",
        after_position=2,
        wait_seconds=1,
    )
    assert page.events == (event,)
    assert registered[0] == tunnel.id and isinstance(registered[-1], tuple)
    assert returned_connections == [True]


@pytest.mark.parametrize(
    "vendor,in_atomic,should_close",
    [("postgresql", False, True), ("postgresql", True, False), ("sqlite", False, False)],
)
@pytest.mark.parametrize("invalid_credential", [False, True])
def test_async_limits_return_worker_connection_even_when_credential_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
    in_atomic: bool,
    should_close: bool,
    invalid_credential: bool,
) -> None:
    returned_connections: list[bool] = []
    monkeypatch.setattr(
        "tunnels.activity.connection",
        SimpleNamespace(
            vendor=vendor,
            in_atomic_block=in_atomic,
            close=lambda: returned_connections.append(True),
        ),
    )
    credential = AgentCredential(id=uuid4(), name="Reader")
    limits = Limits()

    def check_limits(value: AgentCredential) -> Limits:
        assert value is credential
        if invalid_credential:
            raise InvalidCredential()
        return limits

    monkeypatch.setattr("tunnels.activity.limits_for", check_limits)
    if invalid_credential:
        with pytest.raises(InvalidCredential):
            _limits_for_async(credential)
    else:
        assert _limits_for_async(credential) is limits
    assert returned_connections == ([True] if should_close else [])


@pytest.mark.asyncio
async def test_activity_listener_registration_fallback_and_wakeup(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = ActivityListener()
    tunnel_id = uuid4()
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.sqlite3")
    waiter = listener.register(tunnel_id)
    await listener.wait(waiter, 0)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("core.activity_listener.asyncio.sleep", sleep)
    await listener.wait(waiter, 0.1)
    assert sleeps == [0.1]
    listener._wake_all()
    assert waiter.is_set()
    listener.unregister(tunnel_id, waiter)
    listener.unregister(tunnel_id, waiter)
    await listener.stop()
