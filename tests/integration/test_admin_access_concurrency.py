"""PostgreSQL serializes administrator lockout checks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from django.db import close_old_connections, connection

from accounts.admin_access import change_admin_state
from accounts.models import User
from tunnels.errors import LifecycleConflict

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_concurrent_deactivation_preserves_access() -> None:
    assert connection.vendor == "postgresql"
    first = User.objects.create_user(username="first", password="test-long-password-7291")
    second = User.objects.create_user(username="second", password="test-long-password-7282")
    barrier = Barrier(2)

    def deactivate(account_id: UUID) -> bool:
        close_old_connections()
        try:
            actor = User.objects.get(pk=account_id)
            barrier.wait(timeout=10)
            try:
                change_admin_state(
                    actor=actor, admin_id=actor.id, active=False, expected_active=True
                )
            except LifecycleConflict:
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(deactivate, [first.id, second.id]))
    assert sorted(results) == [False, True]
    assert User.objects.filter(is_active=True).count() == 1
