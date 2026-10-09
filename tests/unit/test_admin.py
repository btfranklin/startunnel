"""Admin administration pages do not expose stored credential digests."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from accounts.models import User
from agents.models import AgentCredential

pytestmark = pytest.mark.django_db


def test_admin_accounts_are_not_managed_through_django_admin() -> None:
    assert User not in admin.site._registry


def test_agent_admin_change_page_hides_key_digest(
    client: Client,
    user_factory: Any,
    credential_factory: Any,
) -> None:
    administrator = user_factory(username="admin")
    administrator.is_staff = True
    administrator.is_superuser = True
    administrator.save(update_fields=["is_staff", "is_superuser"])
    credential, _key = credential_factory()
    digest_marker = "stored-key-digest-field"
    credential.key_digest = digest_marker.encode()
    credential.save(update_fields=["key_digest"])
    client.force_login(administrator)

    response = client.get(reverse("admin:agents_agentcredential_change", args=[credential.id]))

    assert response.status_code == 200
    assert b'name="key_digest"' not in response.content
    assert digest_marker.encode() not in response.content
    assert AgentCredential.objects.get(pk=credential.id).key_digest == digest_marker.encode()
