"""Database metrics include queries made by ASGI request threads."""

import httpx
import pytest
from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIHandler
from django.db import connection
from django.test import AsyncClient
from pytest_django.fixtures import Settings

from core.metrics import DB_OPERATION_SECONDS


def select_count() -> float:
    DB_OPERATION_SECONDS.labels(operation="select")
    return next(
        float(sample.value)
        for metric in DB_OPERATION_SECONDS.collect()
        for sample in metric.samples
        if sample.name == "startunnel_db_operation_seconds_count"
        and sample.labels == {"operation": "select"}
    )


def close_database() -> None:
    """Resolve and close the test connection inside its database worker."""
    connection.close()


def read_database() -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
    finally:
        close_database()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_asgi_health_query_updates_database_metrics(settings: Settings) -> None:
    settings.ALLOWED_HOSTS = ["testserver"]
    before = select_count()
    try:
        response = await AsyncClient().get("/health/ready")
        assert response.status_code == 200
        assert select_count() > before
    finally:
        await sync_to_async(close_database, thread_sensitive=True)()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_local_asgi_readiness_checks_the_database(settings: Settings) -> None:
    settings.ALLOWED_HOSTS = ["internal.example"]
    settings.STARTUNNEL_BASE_URL = "https://public.example"
    settings.STARTUNNEL_TRUSTED_PROXY_CIDRS = ["10.0.0.0/8"]
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    transport = httpx.ASGITransport(
        # HTTPX uses broad ASGI mapping types. Django uses typed scopes and events.
        app=ASGIHandler(),  # type: ignore[arg-type]
        client=("127.0.0.1", 12345),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ready"}}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_sync_to_async_query_updates_database_metrics() -> None:
    before = select_count()
    await sync_to_async(read_database, thread_sensitive=True)()
    assert select_count() == before + 1
