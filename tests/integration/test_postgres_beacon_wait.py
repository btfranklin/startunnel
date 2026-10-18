"""PostgreSQL board waits observe committed, shared board changes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, transaction
from django.utils import timezone

import tunnels.beacons as beacon_domain
from accounts.models import User
from agents.models import AgentCredential
from core.activity_listener import ActivityListener
from core.notifications import ACTIVITY_CHANNEL, BOARD_CHANNEL, notify
from tunnels.beacons import (
    _advance_board_position,
    publish_beacon,
    read_board,
    wait_for_board,
    withdraw_beacon,
)
from tunnels.errors import InvalidCredential
from tunnels.services import (
    CreatedTunnel,
    create_tunnel,
    post_reply,
    rotate_address_as_operator,
)

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def board_listener(monkeypatch: pytest.MonkeyPatch) -> ActivityListener:
    listener = ActivityListener()
    monkeypatch.setattr(beacon_domain, "activity_listener", listener)
    return listener


@pytest.fixture(autouse=True)
def require_postgresql() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL board-wait tests.")


def _worker[T](function: Callable[[], T]) -> T:
    try:
        return function()
    finally:
        connection.close()


def _advance_then_rollback() -> None:
    try:
        with transaction.atomic():
            _advance_board_position()
            raise RuntimeError("rollback board notice")
    except RuntimeError:
        return


def _advance_and_commit() -> None:
    with transaction.atomic():
        _advance_board_position()


async def _wait_until_registered(board_listener: ActivityListener) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while not board_listener._board_waiters:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Board waiter did not register.")
        await asyncio.sleep(0.005)
    await asyncio.wait_for(board_listener._ready.wait(), timeout=5)


async def _start_board_wait(
    *, credential: AgentCredential, cursor: str, board_listener: ActivityListener
) -> asyncio.Task[dict[str, Any]]:
    task = asyncio.create_task(
        wait_for_board(credential=credential, after_cursor=cursor, wait_seconds=5)
    )
    await _wait_until_registered(board_listener)
    return task


@pytest.mark.asyncio
async def test_board_notice_is_commit_only_and_reaches_two_listener_connections() -> None:
    listeners = [ActivityListener(), ActivityListener()]
    board_waiters = [listener.register_board() for listener in listeners]
    tunnel_id = uuid4()
    activity_waiter = listeners[0].register(tunnel_id)
    try:
        await asyncio.gather(
            *(asyncio.wait_for(item._ready.wait(), timeout=5) for item in listeners)
        )
        assert all(item._task is not None for item in listeners)

        await sync_to_async(_worker)(_advance_then_rollback)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(board_waiters[0].wait(), timeout=0.15)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(board_waiters[1].wait(), timeout=0.15)

        await sync_to_async(_worker)(_advance_and_commit)
        await asyncio.gather(
            *(asyncio.wait_for(waiter.wait(), timeout=5) for waiter in board_waiters)
        )
        assert all(waiter.is_set() for waiter in board_waiters)
        assert not activity_waiter.is_set()

        for waiter in board_waiters:
            waiter.clear()
        await sync_to_async(_worker)(lambda: notify(ACTIVITY_CHANNEL, str(tunnel_id)))
        await asyncio.wait_for(activity_waiter.wait(), timeout=5)
        assert activity_waiter.is_set()
        assert all(not waiter.is_set() for waiter in board_waiters)
    finally:
        listeners[0].unregister(tunnel_id, activity_waiter)
        for listener, waiter in zip(listeners, board_waiters, strict=True):
            listener.unregister_board(waiter)
        await asyncio.gather(*(listener.stop() for listener in listeners))


def _create(credential: AgentCredential, *, key: str) -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key=f"{key}-create-0001",
        root_content={"type": "text", "text": "Coordinate the review."},
    )


@pytest.mark.asyncio
async def test_wait_wakes_for_publication_reply_rotation_and_withdrawal(
    credential_factory: Any,
    board_listener: ActivityListener,
    user_factory: Callable[..., User],
) -> None:
    publisher, _ = await sync_to_async(credential_factory)(name="Beacon publisher")
    responder, _ = await sync_to_async(credential_factory)(name="Beacon responder")
    reader, _ = await sync_to_async(credential_factory)(name="Board reader")
    operator = await sync_to_async(user_factory)(username="beacon-wait-operator")
    created = await sync_to_async(_worker)(lambda: _create(publisher, key="wait-wakes"))
    try:
        empty_cursor = (await sync_to_async(read_board)(credential=reader)).board_cursor
        waiting = await _start_board_wait(
            credential=reader, cursor=empty_cursor, board_listener=board_listener
        )
        result, _ = await sync_to_async(_worker)(
            lambda: publish_beacon(
                credential=publisher,
                idempotency_key="wait-wakes-publish-0001",
                address=created.address,
                text="Review this proposal.",
            )
        )
        assert (await asyncio.wait_for(waiting, timeout=5))["refresh_required"] is True

        after_publish = (await sync_to_async(read_board)(credential=reader)).board_cursor
        waiting = await _start_board_wait(
            credential=reader, cursor=after_publish, board_listener=board_listener
        )
        await sync_to_async(_worker)(
            lambda: post_reply(
                credential=responder,
                idempotency_key="wait-wakes-reply-0001",
                address=created.address,
                parent_id=created.root.id,
                content={"type": "text", "text": "I reviewed it."},
            )
        )
        assert (await asyncio.wait_for(waiting, timeout=5))["refresh_required"] is True

        after_reply = (await sync_to_async(read_board)(credential=reader)).board_cursor
        waiting = await _start_board_wait(
            credential=reader, cursor=after_reply, board_listener=board_listener
        )
        rotated = await sync_to_async(_worker)(
            lambda: rotate_address_as_operator(
                actor=operator,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
            )
        )
        assert (await asyncio.wait_for(waiting, timeout=5))["refresh_required"] is True

        after_rotation = (await sync_to_async(read_board)(credential=reader)).board_cursor
        waiting = await _start_board_wait(
            credential=reader, cursor=after_rotation, board_listener=board_listener
        )
        await sync_to_async(_worker)(
            lambda: withdraw_beacon(
                credential=publisher,
                idempotency_key="wait-wakes-withdraw-0001",
                address=rotated.address,
                beacon_id=UUID(result["id"]),
            )
        )
        assert (await asyncio.wait_for(waiting, timeout=5))["refresh_required"] is True
    finally:
        await board_listener.stop()


@pytest.mark.asyncio
async def test_wait_observes_commit_between_registration_and_database_reread(
    credential_factory: Any,
    board_listener: ActivityListener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _ = await sync_to_async(credential_factory)(name="Race publisher")
    reader, _ = await sync_to_async(credential_factory)(name="Race reader")
    created = await sync_to_async(_worker)(lambda: _create(publisher, key="wait-race"))
    cursor = (await sync_to_async(read_board)(credential=reader)).board_cursor
    entered = asyncio.Event()
    release = asyncio.Event()
    read_state = beacon_domain._read_board_wait_state_for_async
    loop = asyncio.get_running_loop()

    def pause_then_read(**arguments: Any) -> tuple[bool, float | None]:
        loop.call_soon_threadsafe(entered.set)
        future = asyncio.run_coroutine_threadsafe(release.wait(), loop)
        future.result(timeout=5)
        return read_state(**arguments)

    monkeypatch.setattr("tunnels.beacons._read_board_wait_state_for_async", pause_then_read)
    task = asyncio.create_task(
        wait_for_board(credential=reader, after_cursor=cursor, wait_seconds=5)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert board_listener._board_waiters
        await sync_to_async(_worker)(
            lambda: publish_beacon(
                credential=publisher,
                idempotency_key="wait-race-publish-0001",
                address=created.address,
                text="Committed during registration.",
            )
        )
        release.set()
        assert (await asyncio.wait_for(task, timeout=5))["refresh_required"] is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await board_listener.stop()


@pytest.mark.asyncio
async def test_wait_falls_back_after_listener_disconnect_and_detects_commit(
    credential_factory: Any,
    board_listener: ActivityListener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher, _ = await sync_to_async(credential_factory)(name="Fallback publisher")
    reader, _ = await sync_to_async(credential_factory)(name="Fallback reader")
    created = await sync_to_async(_worker)(lambda: _create(publisher, key="wait-fallback"))
    cursor = (await sync_to_async(read_board)(credential=reader)).board_cursor
    real_wait = board_listener.wait
    fallback_started = asyncio.Event()

    async def disconnect_then_fallback(waiter: asyncio.Event, timeout: float) -> None:
        await board_listener.stop()
        await sync_to_async(_worker)(
            lambda: publish_beacon(
                credential=publisher,
                idempotency_key="wait-fallback-publish-0001",
                address=created.address,
                text="Committed after disconnect.",
            )
        )
        fallback_started.set()
        await real_wait(waiter, min(timeout, 0.1))

    monkeypatch.setattr(board_listener, "wait", disconnect_then_fallback)
    task = asyncio.create_task(
        wait_for_board(credential=reader, after_cursor=cursor, wait_seconds=5)
    )
    try:
        await asyncio.wait_for(fallback_started.wait(), timeout=5)
        assert (await asyncio.wait_for(task, timeout=2))["refresh_required"] is True
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await board_listener.stop()


@pytest.mark.asyncio
async def test_positive_timeout_returns_false_and_revocation_is_rechecked(
    credential_factory: Any,
    board_listener: ActivityListener,
) -> None:
    reader, _ = await sync_to_async(credential_factory)(name="Wait reader")
    cursor = (await sync_to_async(read_board)(credential=reader)).board_cursor
    try:
        response = await wait_for_board(credential=reader, after_cursor=cursor, wait_seconds=1)
        assert response == {"refresh_required": False, "board_cursor": cursor}

        waiting = asyncio.create_task(
            wait_for_board(credential=reader, after_cursor=cursor, wait_seconds=5)
        )
        await _wait_until_registered(board_listener)

        def revoke_and_notify() -> None:
            try:
                with transaction.atomic():
                    AgentCredential.objects.filter(pk=reader.pk).update(revoked_at=timezone.now())
                    notify(BOARD_CHANNEL)
            finally:
                connection.close()

        await sync_to_async(revoke_and_notify)()
        with pytest.raises(InvalidCredential):
            await asyncio.wait_for(waiting, timeout=5)
    finally:
        await board_listener.stop()
