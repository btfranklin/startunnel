"""Admin key availability stays consistent across reads and authentication."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from accounts.admin_access import authenticate_admin_key, issue_admin_key, validate_actor
from accounts.admin_data import key_resource
from accounts.admin_services import list_admin_resources
from accounts.models import AdminCredential
from tunnels.errors import InvalidCredential

pytestmark = pytest.mark.django_db


def test_admin_key_availability_and_revoked_lists_share_the_expiry_boundary(
    user_factory: Any, django_assert_num_queries: Any
) -> None:
    now = timezone.now()
    cases = [
        ("available", True, False, None),
        ("future", True, False, now + timedelta(seconds=1)),
        ("at-expiry", True, False, now),
        ("expired", True, False, now - timedelta(seconds=1)),
        ("revoked", True, True, None),
        ("inactive", False, False, None),
        ("inactive-revoked", False, True, None),
    ]
    with time_machine.travel(now, tick=False):
        inspector = user_factory()
        secrets = {}
        expected_available = set()
        expected_revoked = set()
        for name, active, revoked, expiry in cases:
            owner = user_factory()
            key, secret = issue_admin_key(owner=owner, name=name)
            AdminCredential.objects.filter(pk=key.pk).update(
                expires_at=expiry,
                revoked_at=now if revoked else None,
                last_used_at=now,
            )
            owner.is_active = active
            owner.save(update_fields=["is_active"])
            secrets[key.id] = secret
            if name in {"available", "future"}:
                expected_available.add(key.id)
            if revoked:
                expected_revoked.add(key.id)

        with django_assert_num_queries(1):
            available = set(AdminCredential.objects.available(at=now).values_list("id", flat=True))
        assert available == expected_available
        rows = list(AdminCredential.objects.select_related("owner"))
        with django_assert_num_queries(0):
            assert {row.id for row in rows if row.is_available} == expected_available
            assert {row.id for row in rows if key_resource(row)["available"]} == expected_available

        available_page = list_admin_resources("keys", actor=inspector, state="available")
        revoked_page = list_admin_resources("keys", actor=inspector, state="revoked")
        assert {item["id"] for item in available_page["items"]} == {
            str(key_id) for key_id in expected_available
        }
        assert {item["id"] for item in revoked_page["items"]} == {
            str(key_id) for key_id in expected_revoked
        }
        for row in rows:
            if row.id in expected_available:
                with django_assert_num_queries(1):
                    assert authenticate_admin_key(secrets[row.id]).id == row.id
                with django_assert_num_queries(2):
                    assert validate_actor(row.owner, row).id == row.owner_id
            else:
                with django_assert_num_queries(1), pytest.raises(InvalidCredential):
                    authenticate_admin_key(secrets[row.id])
                with (
                    django_assert_num_queries(2 if row.owner.is_active else 1),
                    pytest.raises(InvalidCredential),
                ):
                    validate_actor(row.owner, row)
