"""Public socket readiness preserves PostgreSQL notification delivery without polling."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from django.db import connection

from core.activity_listener import ActivityListener, _wait_readable
from core.database_options import postgres_connection_options
from core.maintenance import ADVISORY_LOCK_ID, next_deadline
from core.management.commands.maintenance_loop import Command
from core.models import MaintenanceState
from core.notifications import ACTIVITY_CHANNEL, MAINTENANCE_CHANNEL

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def _run(exercise: Callable[[], Coroutine[object, object, None]], loop_kind: str) -> None:
    factory = asyncio.new_event_loop
    if loop_kind == "uvloop":
        factory = pytest.importorskip("uvloop").new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(exercise())


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
def test_zero_timeout_drains_buffered_and_readable_notifications(loop_kind: str) -> None:
    async def exercise() -> None:
        connection = await psycopg.AsyncConnection.connect(
            **postgres_connection_options(),
            autocommit=True,
        )
        sender = await psycopg.AsyncConnection.connect(
            **postgres_connection_options(),
            autocommit=True,
        )
        async with connection, sender:
            await connection.execute(f"LISTEN {ACTIVITY_CHANNEL}")
            await sender.execute("SELECT pg_notify(%s, %s)", [ACTIVITY_CHANNEL, "buffered"])
            # Query completion consumes the notification into Psycopg's backlog.
            await connection.execute("SELECT 1")
            buffered = [item.payload async for item in connection.notifies(timeout=0)]
            assert buffered == ["buffered"]
            for index in range(20):
                await sender.execute("SELECT pg_notify(%s, %s)", [ACTIVITY_CHANNEL, str(index)])
                await asyncio.wait_for(_wait_readable(connection.fileno()), 2)
                received = [item.payload async for item in connection.notifies(timeout=0)]
                assert received == [str(index)]

    _run(exercise, loop_kind)


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
def test_activity_listener_has_no_notification_polls_during_idle(
    loop_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls: list[float | None] = []
    notifies = psycopg.AsyncConnection.notifies

    async def counted_notifies(
        self: psycopg.AsyncConnection[tuple[object, ...]], *, timeout: float | None = None
    ) -> AsyncIterator[psycopg.Notify]:
        polls.append(timeout)
        async for notification in notifies(self, timeout=timeout):
            yield notification

    monkeypatch.setattr(psycopg.AsyncConnection, "notifies", counted_notifies)

    async def exercise() -> None:
        listener = ActivityListener()
        tunnel_id = uuid4()
        waiter = listener.register(tunnel_id)
        try:
            await asyncio.wait_for(listener._ready.wait(), 5)
            waiter.clear()
            await asyncio.sleep(0.35)
            assert polls == [0]
            sender = await psycopg.AsyncConnection.connect(
                **postgres_connection_options(),
                autocommit=True,
            )
            async with sender:
                await sender.execute("SELECT pg_notify(%s, %s)", [ACTIVITY_CHANNEL, str(tunnel_id)])
            await asyncio.wait_for(waiter.wait(), 5)
            assert polls == [0, 0]
        finally:
            listener.unregister(tunnel_id, waiter)
            await asyncio.wait_for(listener.stop(), 1)

    _run(exercise, loop_kind)


def test_maintenance_socket_stop_interrupts_a_full_reconciliation_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    stop = threading.Event()
    command = Command()

    def reconcile(worker_stop: threading.Event) -> dict[str, int]:
        assert worker_stop is stop
        started.set()
        return {"cycles_closed": 0}

    monkeypatch.setattr(command, "_drain_due_work", reconcile)
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.next_deadline", lambda **kwargs: None
    )
    stop_reader, stop_writer = socket.socketpair()
    with (
        stop_reader,
        stop_writer,
        psycopg.connect(
            **postgres_connection_options(),
            autocommit=True,
        ) as connection,
    ):
        connection.execute(f"LISTEN {MAINTENANCE_CHANNEL}")
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(command._postgres_loop, connection, stop, stop_reader)
            assert started.wait(5)
            stop.set()
            stop_writer.send(b"stop")
            assert waiting.result(timeout=2) is None


def test_maintenance_sigterm_interrupts_the_normal_idle_wait() -> None:
    tag = "maintenance-idle-stop-" + uuid4().hex
    environment = os.environ.copy()
    database_url = urlsplit(environment["TEST_DATABASE_URL"])
    environment["TEST_DATABASE_URL"] = urlunsplit(
        database_url._replace(path="/" + quote(str(connection.settings_dict["NAME"]), safe=""))
    )
    environment.update(DJANGO_SETTINGS_MODULE="startunnel.settings.test", PGAPPNAME=tag)
    previous_reconciliations = (
        MaintenanceState.objects.filter(key="worker")
        .values_list("reconciliation_count", flat=True)
        .first()
        or 0
    )
    process = subprocess.Popen(
        [sys.executable, "manage.py", "maintenance_loop"],
        cwd=settings.PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def wait_until(predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            if predicate():
                return
            time.sleep(0.02)
        pytest.fail("The maintenance command did not reach its idle state.")

    def owns_lock() -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks l "
                "JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted "
                "AND l.classid::bigint = %s AND l.objid::bigint = %s "
                "AND a.application_name = %s)",
                [ADVISORY_LOCK_ID >> 32, ADVISORY_LOCK_ID & 0xFFFFFFFF, tag],
            )
            return bool(cursor.fetchone()[0])

    try:
        wait_until(
            lambda: (
                owns_lock()
                and MaintenanceState.objects.filter(
                    key="worker", reconciliation_count__gt=previous_reconciliations
                ).exists()
            )
        )
        initial = MaintenanceState.objects.get(key="worker")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [MAINTENANCE_CHANNEL, "idle-stop-proof"])
        # A processed notification proves LISTEN, then a new reconciliation
        # returns the actual command to its unchanged 60-second wait.
        wait_until(
            lambda: MaintenanceState.objects.filter(
                key="worker",
                notification_wake_count__gt=initial.notification_wake_count,
                reconciliation_count__gt=initial.reconciliation_count,
            ).exists()
        )
        assert next_deadline() is None
        time.sleep(0.2)
        stopped_at = time.monotonic()
        process.terminate()
        assert process.wait(timeout=2) == 0
        assert time.monotonic() - stopped_at < 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
