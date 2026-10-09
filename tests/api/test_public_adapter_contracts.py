"""Public adapters preserve domain errors and supported input limits."""

from __future__ import annotations

import json
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from django.forms import IntegerField
from django.test import AsyncClient, Client

from api.router import api
from core.models import RateLimitBucket
from site_app.forms import TunnelCycleForm
from tunnels.codec import encode_token
from tunnels.models import Cycle
from tunnels.services import close_cycle, create_tunnel


@pytest.mark.django_db(transaction=True)
def test_async_activity_missing_address_preserves_error_and_records_miss(
    credential_factory: Any,
) -> None:
    _, key = credential_factory()
    client = AsyncClient(raise_request_exception=False)
    response = async_to_sync(client.post)(
        "/api/v1/activity",
        data=json.dumps({"address": encode_token(bytes(16)), "wait_seconds": 0}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tunnel_unavailable"
    # Authentication and the failed address each consume one separate bucket.
    assert RateLimitBucket.objects.count() == 2
    assert all(len(bucket.accepted_at_ms) == 1 for bucket in RateLimitBucket.objects.all())


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["start", "rollover"])
def test_browser_cycle_lifetime_matches_supported_server_limit(
    action: str, client: Client, credential_factory: Any
) -> None:
    credential, _ = credential_factory()
    created = create_tunnel(
        credential=credential,
        idempotency_key=f"browser-{action}-create",
        root_content={"type": "text", "text": "Initial root."},
    )
    if action == "start":
        close_cycle(
            credential=credential,
            idempotency_key="browser-start-close",
            address=created.address,
            expected_cycle_id=created.cycle.id,
        )
    client.force_login(credential.created_by)
    page = client.get("/app/tunnels/")
    document = BeautifulSoup(page.content, "html.parser")
    action_input = document.select_one(f'input[name="action"][value="{action}"]')
    assert action_input is not None
    form = action_input.find_parent("form")
    assert form is not None
    lifetime_input = form.select_one('input[name="expires_in_seconds"]')
    assert lifetime_input is not None

    response = client.post(
        "/app/tunnels/",
        {
            "action": action,
            "idempotency_key": f"browser-{action}-operation",
            "tunnel_id": created.tunnel.id,
            "expected_cycle_id": created.cycle.id,
            "expected_address_generation": "1",
            "root_text": "Next root.",
            "expires_in_seconds": "604800",
        },
    )
    assert response.status_code == 302
    cycle = Cycle.objects.get(tunnel=created.tunnel, number=2)
    assert (cycle.expires_at - cycle.created_at).total_seconds() == pytest.approx(604_800, abs=1)
    maximum = lifetime_input["max"]
    minimum = lifetime_input["min"]
    assert isinstance(maximum, str) and isinstance(minimum, str)
    field = TunnelCycleForm().fields["expires_in_seconds"]
    assert isinstance(field, IntegerField)
    assert int(maximum) == 604_800
    assert int(maximum) == field.max_value
    assert int(minimum) == field.min_value


@pytest.mark.django_db
def test_create_payload_error_is_in_openapi(client: Client, credential_factory: Any) -> None:
    _, key = credential_factory()
    response = client.post(
        "/api/v1/tunnels",
        data=json.dumps({"cycle": {"root": {"content": {"type": "text", "text": "x" * 65_537}}}}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "openapi-large-root"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    schema = json.loads(json.dumps(api.get_openapi_schema()))
    responses = schema["paths"]["/api/v1/tunnels"]["post"]["responses"]
    assert str(response.status_code) in responses
    assert responses["413"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
