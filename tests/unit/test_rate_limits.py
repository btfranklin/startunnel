"""PostgreSQL-owned rate limits preserve bounded sliding-window behavior."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.db import DatabaseError, IntegrityError
from django.utils import timezone

from api.rate_limits import (
    _consume,
    consume_address_miss,
    consume_api_operation,
    consume_login_attempt,
    consume_tunnel_creation,
)
from core.limits import Limits
from core.models import RateLimitBucket
from tunnels.errors import DependencyUnavailable, RateLimited

pytestmark = pytest.mark.django_db


def test_primary_window_rejects_and_reports_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    _consume(key="subject", limit=2, window_seconds=60, rule="test")
    _consume(key="subject", limit=2, window_seconds=60, rule="test")
    with pytest.raises(RateLimited) as caught:
        _consume(key="subject", limit=2, window_seconds=60, rule="test")
    assert caught.value.retry_after == 60  # type: ignore[attr-defined]
    assert len(RateLimitBucket.objects.get().accepted_at_ms) == 2


def test_old_entries_are_pruned_and_bad_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    now_ms = int(now.timestamp() * 1000)
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    digest = __import__("api.rate_limits", fromlist=["_key_digest"])._key_digest("prune")
    RateLimitBucket.objects.create(
        key_digest=digest,
        accepted_at_ms=[now_ms - 61_000, "bad", now_ms - 1_000],
        expires_at=now,
    )
    _consume(key="prune", limit=2, window_seconds=60, rule="test")
    assert RateLimitBucket.objects.get().accepted_at_ms == [now_ms - 1_000, now_ms]


def test_burst_window_can_reject_before_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    _consume(key="burst", limit=10, window_seconds=60, rule="test", burst_limit=1)
    with pytest.raises(RateLimited) as caught:
        _consume(key="burst", limit=10, window_seconds=60, rule="test", burst_limit=1)
    assert caught.value.retry_after == 1  # type: ignore[attr-defined]


def test_equal_windows_enforce_the_stricter_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    _consume(
        key="equal-windows",
        limit=4,
        window_seconds=10,
        rule="test",
        burst_limit=1,
        burst_window_seconds=10,
    )
    with pytest.raises(RateLimited) as caught:
        _consume(
            key="equal-windows",
            limit=4,
            window_seconds=10,
            rule="test",
            burst_limit=1,
            burst_window_seconds=10,
        )
    assert caught.value.retry_after == 10  # type: ignore[attr-defined]


def test_integrity_race_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RateLimitBucket.objects
    original = manager.create
    calls = 0

    def racing_create(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("race")
        return original(**kwargs)

    monkeypatch.setattr(manager, "create", racing_create)
    _consume(key="race", limit=2, window_seconds=60, rule="test")
    assert calls == 2


def test_repeated_integrity_race_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        RateLimitBucket.objects,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(IntegrityError("race")),
    )
    with pytest.raises(DependencyUnavailable):
        _consume(key="repeated-race", limit=2, window_seconds=60, rule="test")


def test_database_errors_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.rate_limits._database_now", lambda: (_ for _ in ()).throw(DatabaseError())
    )
    with pytest.raises(DependencyUnavailable):
        _consume(key="broken", limit=1, window_seconds=1, rule="test")


def test_named_limit_helpers_do_not_store_raw_subjects(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    limits = Limits(api_operations_per_minute=5, api_burst=5)
    consume_api_operation(credential, limits)
    consume_tunnel_creation(credential, limits)
    consume_address_miss(credential, limits, source_ip="192.0.2.44")
    consume_login_attempt(source_ip="192.0.2.44", username="Person")
    assert RateLimitBucket.objects.count() == 4
    serialized = " ".join(str(bucket.accepted_at_ms) for bucket in RateLimitBucket.objects.all())
    assert "192.0.2.44" not in serialized and "Person" not in serialized


def test_bucket_expiry_uses_longest_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    _consume(
        key="expiry",
        limit=10,
        window_seconds=10,
        rule="test",
        burst_limit=2,
        burst_window_seconds=30,
    )
    assert RateLimitBucket.objects.get().expires_at == now + timedelta(seconds=30)


def test_longer_burst_window_keeps_its_full_accepted_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = timezone.now()
    monkeypatch.setattr("api.rate_limits._database_now", lambda: now)
    for _ in range(4):
        _consume(
            key="long-burst",
            limit=2,
            window_seconds=1,
            rule="test",
            burst_limit=4,
            burst_window_seconds=30,
        )
        now += timedelta(seconds=2)
    assert len(RateLimitBucket.objects.get().accepted_at_ms) == 4
    with pytest.raises(RateLimited):
        _consume(
            key="long-burst",
            limit=2,
            window_seconds=1,
            rule="test",
            burst_limit=4,
            burst_window_seconds=30,
        )
