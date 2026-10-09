"""Atomic PostgreSQL sliding-window limits."""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime, timedelta
from typing import cast

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from core.metrics import RATE_LIMITED
from tunnels.errors import DependencyUnavailable, RateLimited

from .models import RateLimitBucket


def _database_now() -> datetime:
    if connection.vendor != "postgresql":
        return timezone.now()
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cast(datetime, cursor.fetchone()[0])


def _key_digest(key: str) -> bytes:
    return hmac.new(
        settings.IDEMPOTENCY_SECRET.encode(),
        key.encode(),
        hashlib.sha256,
    ).digest()


def _consume(
    *,
    key: str,
    limit: int,
    window_seconds: int,
    rule: str,
    burst_limit: int | None = None,
    burst_window_seconds: int = 1,
) -> None:
    digest = _key_digest(key)
    longest_window = max(window_seconds, burst_window_seconds if burst_limit is not None else 0)
    storage_limit = limit
    if burst_limit is not None:
        if burst_window_seconds > window_seconds:
            storage_limit = burst_limit
        elif burst_window_seconds == window_seconds:
            storage_limit = max(limit, burst_limit)

    def consume_once() -> None:
        with transaction.atomic():
            now = _database_now()
            now_ms = int(now.timestamp() * 1_000)
            oldest_ms = now_ms - longest_window * 1_000
            bucket = RateLimitBucket.objects.select_for_update().filter(key_digest=digest).first()
            if bucket is None:
                bucket = RateLimitBucket.objects.create(
                    key_digest=digest,
                    accepted_at_ms=[],
                    expires_at=now + timedelta(seconds=longest_window),
                )
            accepted = [
                value
                for value in bucket.accepted_at_ms
                if isinstance(value, int) and value > oldest_ms
            ]

            retry_ms = 0
            primary = [value for value in accepted if value > now_ms - window_seconds * 1_000]
            if len(primary) >= limit:
                retry_ms = window_seconds * 1_000 - (now_ms - primary[0])
            if burst_limit is not None:
                burst = [
                    value for value in accepted if value > now_ms - burst_window_seconds * 1_000
                ]
                if len(burst) >= burst_limit:
                    retry_ms = max(
                        retry_ms,
                        burst_window_seconds * 1_000 - (now_ms - burst[0]),
                    )
            if retry_ms > 0:
                retry_after = max(math.ceil(retry_ms / 1_000), 1)
                RATE_LIMITED.labels(rule=rule).inc()
                error = RateLimited(
                    f"The {rule} limit was reached. Try again in {retry_after} seconds."
                )
                error.retry_after = retry_after  # type: ignore[attr-defined]
                raise error

            accepted.append(now_ms)
            bucket.accepted_at_ms = accepted[-storage_limit:]
            bucket.expires_at = now + timedelta(seconds=longest_window)
            bucket.save(update_fields=["accepted_at_ms", "expires_at"])

    try:
        try:
            consume_once()
        except IntegrityError:
            consume_once()
    except RateLimited:
        raise
    except DatabaseError as error:
        raise DependencyUnavailable() from error


def consume_admin_operation(admin_id: object) -> None:
    """Share an administrator's operation quota across all interfaces and keys."""
    from .limits import provider

    limits = provider.for_instance()
    _consume(
        key=f"admin:{admin_id}",
        limit=limits.api_operations_per_minute,
        window_seconds=60,
        burst_limit=limits.api_burst,
        rule="admin operation",
    )
