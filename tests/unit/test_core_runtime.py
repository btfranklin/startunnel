"""Core health, metrics, notifications, and listener behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from django.test import Client

from core.activity_listener import ActivityListener, _connection_kwargs
from core.notifications import notify, notify_maintenance

pytestmark = pytest.mark.django_db


def test_health_endpoints_report_database_state(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/health/live").json() == {"status": "live"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["postgres"] == "ready"
    monkeypatch.setattr(
        "core.views.connection.cursor",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    unavailable = client.get("/health/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "not_ready"


def test_metrics_require_token_and_publish_maintenance_status(
    client: Client, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.METRICS_TOKEN = "metrics-test-token"
    assert client.get("/metrics").status_code == 404
    status = SimpleNamespace(
        cleanup_lag_seconds=1.5,
        reconciliation_count=2,
        due_work_count=3,
        notification_wake_count=4,
    )
    monkeypatch.setattr("core.views.read_maintenance_status", lambda: status)
    response = client.get("/metrics", headers={"X-Metrics-Token": "metrics-test-token"})
    assert response.status_code == 200
    assert b"startunnel_cleanup_lag_seconds" in response.content
    monkeypatch.setattr(
        "core.views.read_maintenance_status",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert (
        client.get("/metrics", headers={"X-Metrics-Token": "metrics-test-token"}).status_code == 503
    )


def test_notifications_are_postgres_only(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, list[str]]] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, parameters: list[str]) -> None:
            executed.append((sql, parameters))

    monkeypatch.setattr("core.notifications.connection.vendor", "sqlite")
    notify("channel", "payload")
    assert executed == []
    monkeypatch.setattr("core.notifications.connection.vendor", "postgresql")
    monkeypatch.setattr("core.notifications.connection.cursor", Cursor)
    notify("channel", "payload")
    notify_maintenance()
    assert executed == [
        ("SELECT pg_notify(%s, %s)", ["channel", "payload"]),
        ("SELECT pg_notify(%s, %s)", ["startunnel_maintenance", "1"]),
    ]


def test_listener_connection_settings_are_redacted_shape(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "NAME": "db",
        "USER": "user",
        "PASSWORD": "pass",
        "HOST": "host",
        "PORT": "5432",
    }.items():
        monkeypatch.setitem(settings.DATABASES["default"], name, value)
    assert _connection_kwargs() == {
        "dbname": "db",
        "user": "user",
        "password": "pass",
        "host": "host",
        "port": "5432",
        "connect_timeout": 5,
    }


@pytest.mark.asyncio
async def test_listener_dispatches_valid_notifications_and_ignores_invalid(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.postgresql")
    tunnel_id = uuid4()

    class Notification:
        def __init__(self, payload: str) -> None:
            self.payload = payload

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str) -> None:
            assert sql == "LISTEN startunnel_activity"

        async def notifies(self) -> Any:
            yield Notification("invalid")
            yield Notification(str(tunnel_id))
            raise asyncio.CancelledError

    async def connect(**kwargs: Any) -> Connection:
        return Connection()

    monkeypatch.setattr("core.activity_listener.psycopg.AsyncConnection.connect", connect)
    listener = ActivityListener()
    waiter = listener.register(tunnel_id)
    assert listener._task is not None
    with pytest.raises(asyncio.CancelledError):
        await listener._task
    assert waiter.is_set()
    await listener.stop()


@pytest.mark.asyncio
async def test_listener_reconnects_after_database_error(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.postgresql")
    calls = 0

    async def connect(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise psycopg.OperationalError("down")

    async def sleep(seconds: float) -> None:
        assert seconds == 1.0
        listener._stopping = True

    monkeypatch.setattr("core.activity_listener.psycopg.AsyncConnection.connect", connect)
    monkeypatch.setattr("core.activity_listener.asyncio.sleep", sleep)
    listener = ActivityListener()
    await listener._run()
    assert calls == 1
