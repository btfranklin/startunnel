"""Notification waits block on socket readiness and release their resources."""

from __future__ import annotations

import asyncio
import signal
import socket
import threading

import pytest
from pytest_django.fixtures import Settings

from core.activity_listener import _wait_readable
from core.management.commands.maintenance_loop import Command, _wait_for_notification


class SocketNotifications:
    def __init__(self, reader: socket.socket) -> None:
        self.reader = reader
        self.reader.setblocking(False)
        self.polls = 0
        self.pending: list[object] = []

    def fileno(self) -> int:
        return self.reader.fileno()

    def notifies(self, *, timeout: float) -> list[object]:
        assert timeout == 0
        self.polls += 1
        if self.pending:
            result, self.pending = self.pending, []
            return result
        try:
            data = self.reader.recv(1024)
        except BlockingIOError:
            return []
        return [object()] if data else []


def test_notification_wait_drains_buffered_notifications_before_socket_wait() -> None:
    reader, writer = socket.socketpair()
    stop_reader, stop_writer = socket.socketpair()
    with reader, writer, stop_reader, stop_writer:
        connection = SocketNotifications(reader)
        connection.pending.append(object())
        assert _wait_for_notification(connection, stop_reader, 1)  # type: ignore[arg-type]
        assert connection.polls == 1


@pytest.mark.parametrize("stop", [False, True])
def test_notification_wait_does_not_poll_between_socket_wakes(stop: bool) -> None:
    reader, writer = socket.socketpair()
    stop_reader, stop_writer = socket.socketpair()
    with reader, writer, stop_reader, stop_writer:
        connection = SocketNotifications(reader)
        target = stop_writer if stop else writer
        timer = threading.Timer(0.25, target.send, args=(b"wake",))
        timer.start()
        try:
            notified = _wait_for_notification(connection, stop_reader, 2)  # type: ignore[arg-type]
        finally:
            timer.join()
        assert notified is not stop
        assert connection.polls == 2


def test_notification_wait_stops_at_the_deadline_without_repeated_polls() -> None:
    reader, writer = socket.socketpair()
    stop_reader, stop_writer = socket.socketpair()
    with reader, writer, stop_reader, stop_writer:
        connection = SocketNotifications(reader)
        assert not _wait_for_notification(connection, stop_reader, 0.25)  # type: ignore[arg-type]
        assert connection.polls == 1


@pytest.mark.asyncio
async def test_async_socket_wait_wakes_for_data_and_removes_reader_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, writer = socket.socketpair()
    with reader, writer:
        loop = asyncio.get_running_loop()
        removed: list[int] = []
        remove_reader = loop.remove_reader

        def record_removal(fileno: int) -> bool:
            removed.append(fileno)
            return remove_reader(fileno)

        monkeypatch.setattr(loop, "remove_reader", record_removal)
        waiting = asyncio.create_task(_wait_readable(reader.fileno()))
        await asyncio.sleep(0)
        assert not waiting.done()
        writer.send(b"wake")
        await asyncio.wait_for(waiting, 1)
        assert reader.recv(1024) == b"wake"
        waiting = asyncio.create_task(_wait_readable(reader.fileno()))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert removed == [reader.fileno(), reader.fileno()]


def test_maintenance_stop_wakes_socket_and_restores_handlers_after_failure(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.postgresql")
    original_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    sockets: list[socket.socket] = []
    socketpair = socket.socketpair

    def tracked_socketpair() -> tuple[socket.socket, socket.socket]:
        pair = socketpair()
        sockets.extend(pair)
        return pair

    command = Command()

    def stop_then_fail(stop: threading.Event, stop_reader: socket.socket) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stop.is_set()
        assert stop_reader.recv(1) == b"0"
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.socket.socketpair", tracked_socketpair
    )
    monkeypatch.setattr(command, "_run_postgres", stop_then_fail)
    with pytest.raises(RuntimeError, match="cleanup failure"):
        command.handle(once=False)
    assert all(sock.fileno() == -1 for sock in sockets)
    assert {signum: signal.getsignal(signum) for signum in original_handlers} == original_handlers
