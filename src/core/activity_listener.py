"""One PostgreSQL activity listener for each web process."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

import psycopg
from django.conf import settings

from .notifications import ACTIVITY_CHANNEL

logger = logging.getLogger(__name__)


def _connection_kwargs() -> dict[str, object]:
    database = settings.DATABASES["default"]
    return {
        "dbname": database["NAME"],
        "user": database.get("USER") or None,
        "password": database.get("PASSWORD") or None,
        "host": database.get("HOST") or None,
        "port": database.get("PORT") or None,
        "connect_timeout": 5,
    }


class ActivityListener:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stopping = False
        self._waiters: dict[UUID, set[asyncio.Event]] = {}

    def start(self) -> None:
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
            return
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="startunnel-activity-listener")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def register(self, tunnel_id: UUID) -> asyncio.Event:
        self.start()
        waiter = asyncio.Event()
        self._waiters.setdefault(tunnel_id, set()).add(waiter)
        return waiter

    def unregister(self, tunnel_id: UUID, waiter: asyncio.Event) -> None:
        tunnel_waiters = self._waiters.get(tunnel_id)
        if tunnel_waiters is None:
            return
        tunnel_waiters.discard(waiter)
        if not tunnel_waiters:
            self._waiters.pop(tunnel_id, None)

    async def wait(self, waiter: asyncio.Event, timeout: float) -> None:
        if timeout <= 0:
            return
        if not self._ready.is_set():
            await asyncio.sleep(min(timeout, 1.0))
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(waiter.wait(), timeout)
        waiter.clear()

    def _wake_all(self) -> None:
        for tunnel_waiters in self._waiters.values():
            for waiter in tunnel_waiters:
                waiter.set()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                connection = await psycopg.AsyncConnection.connect(
                    **_connection_kwargs(),  # type: ignore[arg-type]
                    autocommit=True,
                )
                async with connection:
                    await connection.execute(f"LISTEN {ACTIVITY_CHANNEL}")
                    self._ready.set()
                    backoff = 1.0
                    async for notification in connection.notifies():
                        try:
                            tunnel_id = UUID(notification.payload)
                        except ValueError:
                            continue
                        for waiter in self._waiters.get(tunnel_id, set()):
                            waiter.set()
            except asyncio.CancelledError:
                raise
            except psycopg.Error:
                logger.warning("The PostgreSQL activity listener disconnected.")
            finally:
                self._ready.clear()
                self._wake_all()
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


activity_listener = ActivityListener()
