"""One bounded worker pool for asynchronous database reads."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

# Leave one database pool slot for request handling.
ASYNC_DATABASE_WORKERS = max(min(settings.DATABASE_POOL_MAX_SIZE - 1, 16), 1)
ASYNC_DATABASE_EXECUTOR = ThreadPoolExecutor(
    max_workers=ASYNC_DATABASE_WORKERS,
    thread_name_prefix="startunnel-async-db",
)
