"""Admin administration pages do not expose stored credential digests."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from accounts.models import User
from agents.models import AgentCredential
from tunnels.models import TunnelAddress
from tunnels.services import create_tunnel, rotate_address_as_operator

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


@pytest.mark.parametrize("payload_type", ["text", "json"])
def test_tunnel_inspection_shows_stored_data_but_rejects_changes(
    client: Client, credential_factory: Any, payload_type: str
) -> None:
    administrator = User.objects.create_superuser(
        username="inspection-admin", password="test-password-42"
    )
    credential, _ = credential_factory()
    marker = "Visible inspection payload"
    content: dict[str, Any] = (
        {"type": "text", "text": marker}
        if payload_type == "text"
        else {"type": "json", "value": {"detail": marker}}
    )
    created = create_tunnel(
        credential=credential, idempotency_key="inspection-create", root_content=content
    )
    rotated = rotate_address_as_operator(
        actor=administrator, tunnel_id=created.tunnel.pk, expected_address_generation=1
    )
    address = TunnelAddress.objects.get(tunnel=created.tunnel, state="current")
    event = created.tunnel.events.get(event_type="address_rotated")
    client.force_login(administrator)
    for model, identifier, visible in (
        ("tunneladdress", address.pk, rotated.address),
        ("message", created.root.pk, marker),
        ("tunnelevent", event.pk, "generation"),
        ("cycle", created.cycle.pk, str(created.root.pk)),
    ):
        page = client.get(reverse(f"admin:tunnels_{model}_change", args=[identifier]))
        assert page.status_code == 200
        assert visible.encode() in page.content

    change_url = reverse("admin:tunnels_message_change", args=[created.root.pk])
    assert client.post(change_url, {"text_payload": "Changed"}).status_code == 403
    assert client.post(reverse("admin:tunnels_message_add"), {}).status_code == 403
    assert (
        client.post(reverse("admin:tunnels_message_delete", args=[created.root.pk]), {}).status_code
        == 403
    )
    created.root.refresh_from_db()
    assert marker in (created.root.text_payload or created.root.json_payload or "")
