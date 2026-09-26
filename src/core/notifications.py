"""Opaque PostgreSQL wake notifications."""

from __future__ import annotations

from django.db import connection

ACTIVITY_CHANNEL = "startunnel_activity"
MAINTENANCE_CHANNEL = "startunnel_maintenance"


def notify(channel: str, payload: str = "1") -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [channel, payload])


def notify_maintenance() -> None:
    notify(MAINTENANCE_CHANNEL)
