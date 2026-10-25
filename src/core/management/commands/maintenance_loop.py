"""Wait for persisted deadlines or PostgreSQL wake notifications."""

from __future__ import annotations

import json
import logging
import selectors
import signal
import socket
import threading
from collections.abc import Mapping
from contextlib import suppress

import psycopg
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.db import DatabaseError, close_old_connections
from django.utils import timezone

from core.database_options import postgres_connection_options
from core.maintenance import (
    ADVISORY_LOCK_ID,
    cleanup_once,
    has_due_work,
    next_deadline,
    record_maintenance_error,
    record_notification_wake,
)
from core.notifications import MAINTENANCE_CHANNEL

RECONCILIATION_SECONDS = 60.0
logger = logging.getLogger(__name__)


def _should_report_cycle(counts: Mapping[str, int], *, once: bool) -> bool:
    return once or any(count != 0 for count in counts.values())


def _wait_for_notification(
    listener: psycopg.Connection[tuple[object, ...]], stop_socket: socket.socket, timeout: float
) -> bool:
    if list(listener.notifies(timeout=0)):
        return True
    with selectors.DefaultSelector() as selector:
        selector.register(listener, selectors.EVENT_READ)
        selector.register(stop_socket, selectors.EVENT_READ)
        return bool(selector.select(timeout)) and bool(list(listener.notifies(timeout=0)))


class Command(BaseCommand):
    help = "Run deadline-based StarTunnel maintenance."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        del args
        if options["once"]:
            self._reconcile(once=True)
            return
        stop = threading.Event()
        stop_reader, stop_writer = socket.socketpair()
        with stop_reader, stop_writer:
            stop_writer.setblocking(False)

            def request_stop(signum: int, frame: object) -> None:
                del signum, frame
                stop.set()
                with suppress(BlockingIOError):
                    stop_writer.send(b"0")

            handlers = [
                (signum, signal.signal(signum, request_stop))
                for signum in (signal.SIGTERM, signal.SIGINT)
            ]
            try:
                if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
                    self._sqlite_loop(stop)
                else:
                    self._run_postgres(stop, stop_reader)
            finally:
                for signum, handler in handlers:
                    signal.signal(signum, handler)

    def _run_postgres(self, stop: threading.Event, stop_reader: socket.socket) -> None:
        self.retry_delay = 1.0
        while not stop.is_set():
            try:
                with psycopg.connect(
                    **postgres_connection_options(),
                    autocommit=True,
                ) as listener:
                    listener.execute(f"LISTEN {MAINTENANCE_CHANNEL}")
                    acquired = listener.execute(
                        "SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID]
                    ).fetchone()
                    if not acquired or not acquired[0]:
                        stop.wait(RECONCILIATION_SECONDS)
                        continue
                    self._postgres_loop(listener, stop, stop_reader)
            except (psycopg.Error, DatabaseError) as error:
                logger.exception("Maintenance database operation failed.")
                close_old_connections()
                with suppress(Exception):
                    record_maintenance_error(type(error).__name__)
                if stop.wait(self.retry_delay):
                    return
                self.retry_delay = min(self.retry_delay * 2, RECONCILIATION_SECONDS)

    def _postgres_loop(
        self,
        listener: psycopg.Connection[tuple[object, ...]],
        stop: threading.Event,
        stop_reader: socket.socket,
    ) -> None:
        while not stop.is_set():
            counts = self._drain_due_work(stop)
            failed = counts.get("cycle_delete_failures", 0) > 0
            deadline = next_deadline(skip_overdue_deletions=failed)
            now = timezone.now()
            timeout = RECONCILIATION_SECONDS
            if deadline is not None and deadline > now:
                timeout = min((deadline - now).total_seconds(), RECONCILIATION_SECONDS)
            elif deadline is not None:
                timeout = 0
            if _wait_for_notification(listener, stop_reader, max(timeout, 0)):
                record_notification_wake()

    def _sqlite_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            counts = self._drain_due_work(stop)
            failed = counts.get("cycle_delete_failures", 0) > 0
            deadline = next_deadline(skip_overdue_deletions=failed)
            timeout = RECONCILIATION_SECONDS
            if deadline is not None:
                timeout = min(max((deadline - timezone.now()).total_seconds(), 0), timeout)
            stop.wait(timeout)

    def _drain_due_work(self, stop: threading.Event) -> dict[str, int]:
        counts = self._reconcile(once=False)
        while (
            any(count for name, count in counts.items() if name != "cycle_delete_failures")
            and has_due_work()
            and not stop.is_set()
        ):
            counts = self._reconcile(once=False)
        self.retry_delay = 1.0
        return counts

    def _reconcile(self, *, once: bool) -> dict[str, int]:
        counts = cleanup_once()
        if _should_report_cycle(counts, once=once):
            self.stdout.write(
                json.dumps({"event": "maintenance_cycle", "counts": counts}, sort_keys=True)
            )
        return counts
