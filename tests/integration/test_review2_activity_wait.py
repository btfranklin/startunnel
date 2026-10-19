"""Activity waits reread commits that occur after notification registration."""

import asyncio
from contextlib import suppress
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

import tunnels.activity as activity
from core.activity_listener import ActivityListener
from tunnels.cycles import create_tunnel
from tunnels.messages import post_reply

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.asyncio
async def test_activity_wait_observes_commit_in_registration_window(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, _ = await sync_to_async(credential_factory)()
    reader, _ = await sync_to_async(credential_factory)()

    def create() -> Any:
        try:
            return create_tunnel(
                credential=publisher,
                idempotency_key="activity-window-create",
                root_content={"type": "text", "text": "Review"},
            )
        finally:
            connection.close()

    created = await sync_to_async(create)()
    initial = await activity.read_activity_async(credential=reader, address=created.address)
    listener = ActivityListener()
    monkeypatch.setattr(activity, "activity_listener", listener)
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    read = activity._continue_activity_wait_for_async

    def pause_then_read(**arguments: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return read(**arguments)

    monkeypatch.setattr(activity, "_continue_activity_wait_for_async", pause_then_read)
    waiting = asyncio.create_task(
        activity.read_activity_async(
            credential=reader,
            address=created.address,
            after_position=initial.next_position,
            wait_seconds=5,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert listener._waiters

        def reply() -> Any:
            try:
                return post_reply(
                    credential=publisher,
                    address=created.address,
                    parent_id=created.root.id,
                    idempotency_key="activity-window-reply",
                    content={"type": "text", "text": "Committed during registration"},
                )
            finally:
                connection.close()

        posted = await sync_to_async(reply)()
        release.set()
        page = await asyncio.wait_for(waiting, timeout=5)
        assert [event.message_id for event in page.events] == [posted.message.id]
        assert page.next_position > initial.next_position
    finally:
        release.set()
        if not waiting.done():
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting
        await listener.stop()
