"""Wait for persisted deadlines or PostgreSQL wake notifications."""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Mapping
from contextlib import suppress

import psycopg
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.notifications import MAINTENANCE_CHANNEL
from tunnels.maintenance import (
    ADVISORY_LOCK_ID,
    cleanup_once,
    has_due_work,
    next_deadline,
    record_maintenance_error,
    record_notification_wake,
)

RECONCILIATION_SECONDS = 60.0


def _should_report_cycle(counts: Mapping[str, int], *, once: bool) -> bool:
    return once or any(count != 0 for count in counts.values())


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


class Command(BaseCommand):
    help = "Run deadline-based StarTunnel maintenance."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--once", action="store_true")  # type: ignore[attr-defined]

    def handle(self, *args: object, **options: object) -> None:
        del args
        stop = threading.Event()
        once = bool(options["once"])

        def request_stop(signum: int, frame: object) -> None:
            del signum, frame
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        if once:
            self._reconcile(once=True)
            return
        if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
            self._sqlite_loop(stop)
            return

        backoff = 1.0
        while not stop.is_set():
            try:
                with psycopg.connect(
                    **_connection_kwargs(),  # type: ignore[arg-type]
                    autocommit=True,
                ) as listener:
                    listener.execute(f"LISTEN {MAINTENANCE_CHANNEL}")
                    acquired = listener.execute(
                        "SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID]
                    ).fetchone()
                    if not acquired or not acquired[0]:
                        stop.wait(RECONCILIATION_SECONDS)
                        continue
                    backoff = 1.0
                    self._postgres_loop(listener, stop)
            except psycopg.Error as error:
                with suppress(Exception):
                    record_maintenance_error(type(error).__name__)
                if stop.wait(backoff):
                    return
                backoff = min(backoff * 2, RECONCILIATION_SECONDS)

    def _postgres_loop(
        self,
        listener: psycopg.Connection[tuple[object, ...]],
        stop: threading.Event,
    ) -> None:
        while not stop.is_set():
            counts = self._reconcile(once=False)
            failed = counts.get("cycle_delete_failures", 0) > 0
            while not failed and has_due_work() and not stop.is_set():
                counts = self._reconcile(once=False)
                failed = counts.get("cycle_delete_failures", 0) > 0
            deadline = next_deadline()
            now = timezone.now()
            timeout = RECONCILIATION_SECONDS
            if deadline is not None and deadline > now:
                timeout = min((deadline - now).total_seconds(), RECONCILIATION_SECONDS)
            elif deadline is not None and not failed:
                timeout = 0
            notifications = list(listener.notifies(timeout=max(timeout, 0), stop_after=1))
            if notifications:
                record_notification_wake()

    def _sqlite_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._reconcile(once=False)
            deadline = next_deadline()
            timeout = RECONCILIATION_SECONDS
            if deadline is not None:
                timeout = min(max((deadline - timezone.now()).total_seconds(), 0), timeout)
            stop.wait(timeout)

    def _reconcile(self, *, once: bool) -> dict[str, int]:
        counts = cleanup_once()
        if _should_report_cycle(counts, once=once):
            self.stdout.write(
                json.dumps({"event": "maintenance_cycle", "counts": counts}, sort_keys=True)
            )
        return counts
