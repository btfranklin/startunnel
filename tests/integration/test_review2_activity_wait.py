"""Activity waits reread commits that occur after notification registration."""

import asyncio
from contextlib import suppress

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from tests.factories import CredentialFactory

import tunnels.activity as activity
from agents.models import AgentCredential
from core.activity_listener import ActivityListener
from tunnels.activity import ActivityPage
from tunnels.cycles import CreatedTunnel, create_tunnel
from tunnels.messages import PostedReply, post_reply
from tunnels.models import Tunnel

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.asyncio
async def test_activity_wait_observes_commit_in_registration_window(
    credential_factory: CredentialFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, _ = await sync_to_async(credential_factory)()
    reader, _ = await sync_to_async(credential_factory)()

    def create() -> CreatedTunnel:
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

    def pause_then_read(
        *,
        tunnel: Tunnel,
        credential: AgentCredential,
        address: str,
        after_position: int,
        limit: int,
        event_types: tuple[str, ...] | None,
    ) -> ActivityPage:
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return read(
            tunnel=tunnel,
            credential=credential,
            address=address,
            after_position=after_position,
            limit=limit,
            event_types=event_types,
        )

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

        def reply() -> PostedReply:
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
