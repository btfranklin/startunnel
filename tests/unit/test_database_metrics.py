"""Database metrics include queries made by ASGI request threads."""

from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test import AsyncClient

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


def read_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_asgi_health_query_updates_database_metrics(settings: Any) -> None:
    settings.ALLOWED_HOSTS = ["testserver"]
    before = select_count()
    response = await AsyncClient().get("/health/ready")
    assert response.status_code == 200
    assert select_count() > before


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_sync_to_async_query_updates_database_metrics() -> None:
    before = select_count()
    await sync_to_async(read_database, thread_sensitive=True)()
    assert select_count() == before + 1
