"""Human pages expose one equal instance-administrator interface."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.test import Client
from django.utils import timezone

from agents.models import AgentCredential
from agents.services import CredentialError
from site_app.views import _authenticated_user
from tunnels.errors import InvalidRequest, TunnelUnavailable

pytestmark = pytest.mark.django_db


def test_public_home_docs_and_download(client: Client) -> None:
    assert client.get("/").status_code == 302
    assert client.get("/docs/").status_code == 200
    assert client.get("/docs/not-a-page/").status_code == 404
    response = client.get("/downloads/star_tunnel.py")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/x-python"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_authenticated_user_requires_custom_user() -> None:
    request = HttpRequest()
    request.user = cast(Any, SimpleNamespace())
    with pytest.raises(PermissionDenied):
        _authenticated_user(request)


def test_human_pages_require_login_and_render(client: Client, user_factory: Any) -> None:
    for path in ["/app/", "/app/learn/", "/app/agents/", "/app/tunnels/", "/app/account/"]:
        assert client.get(path).status_code == 302
    user = user_factory()
    client.force_login(user)
    for path in ["/app/", "/app/learn/", "/app/agents/", "/app/tunnels/", "/app/account/"]:
        assert client.get(path).status_code == 200


def test_create_agent_shows_key_once_and_revoke_is_instance_wide(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    client.force_login(actor)
    created = client.post("/app/agents/create/", {"name": "Build agent"})
    assert created.status_code == 200
    assert created["Cache-Control"].startswith("no-store, private")
    credential = AgentCredential.objects.get(name="Build agent")
    assert credential.created_by == actor
    other, _ = credential_factory(name="Other agent")
    assert client.post("/app/agents/revoke/", {"credential_id": other.id}).status_code == 302
    other.refresh_from_db()
    assert other.revoked_at is not None
    assert client.post("/app/agents/revoke/", {"credential_id": uuid4()}).status_code == 404


def test_agent_page_renders_form_and_domain_errors(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    assert client.post("/app/agents/create/", {"name": ""}).status_code == 400
    monkeypatch.setattr(
        "site_app.views.create_credential",
        lambda **kwargs: (_ for _ in ()).throw(CredentialError("No capacity.")),
    )
    response = client.post("/app/agents/create/", {"name": "Agent"})
    assert response.status_code == 400
    assert b"No capacity" in response.content


def test_expired_and_suspended_credentials_are_not_active(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    client.force_login(user_factory())
    expired, _ = credential_factory(name="Expired agent")
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])
    suspended, _ = credential_factory(name="Suspended agent")
    suspended.suspended_at = timezone.now()
    suspended.save(update_fields=["suspended_at"])

    dashboard = client.get("/app/")
    agents = client.get("/app/agents/")
    assert dashboard.context["active_agent_count"] == 0
    assert b"Expired" in agents.content
    assert b"Suspended" in agents.content


@pytest.mark.parametrize("action", ["close", "start", "rollover", "retire"])
def test_tunnel_actions_call_instance_operator_services(
    action: str,
    client: Client,
    user_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user_factory()
    client.force_login(actor)
    called: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> None:
        called.append(kwargs)

    target = {
        "close": "close_cycle_as_operator",
        "start": "start_cycle_as_operator",
        "rollover": "rollover_cycle_as_operator",
        "retire": "retire_tunnel_as_operator",
    }[action]
    monkeypatch.setattr(f"site_app.views.{target}", capture)
    response = client.post(
        "/app/tunnels/",
        {
            "action": action,
            "tunnel_id": uuid4(),
            "expected_cycle_id": uuid4(),
            "expected_address_generation": "1",
            "root_text": "New root.",
            "cycle_label": "New cycle",
            "expires_in_seconds": "600",
            "confirmation": "retire",
        },
    )
    assert response.status_code == 302
    assert called and called[0]["actor"] == actor


def test_tunnel_rotate_returns_one_time_address(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    monkeypatch.setattr(
        "site_app.views.rotate_address_as_operator",
        lambda **kwargs: SimpleNamespace(
            address="safe-address", display_address="safe display", generation=2
        ),
    )
    response = client.post(
        "/app/tunnels/",
        {"action": "rotate", "tunnel_id": uuid4(), "expected_address_generation": "1"},
    )
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store, private"
    assert b"safe display" in response.content


def test_tunnel_page_maps_bad_input_and_domain_errors(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    assert client.post("/app/tunnels/", {"action": "bad", "tunnel_id": uuid4()}).status_code == 302
    assert client.post("/app/tunnels/", {"action": "close", "tunnel_id": "bad"}).status_code == 302
    monkeypatch.setattr(
        "site_app.views.close_cycle_as_operator",
        lambda **kwargs: (_ for _ in ()).throw(TunnelUnavailable()),
    )
    response = client.post(
        "/app/tunnels/",
        {"action": "close", "tunnel_id": uuid4(), "expected_cycle_id": uuid4()},
    )
    assert response.status_code == 302
    monkeypatch.setattr(
        "site_app.views.close_cycle_as_operator",
        lambda **kwargs: (_ for _ in ()).throw(InvalidRequest("Invalid operation.")),
    )
    assert (
        client.post(
            "/app/tunnels/",
            {"action": "close", "tunnel_id": uuid4(), "expected_cycle_id": uuid4()},
        ).status_code
        == 302
    )


def test_private_response_helpers_do_not_cache(
    rf: Any, monkeypatch: pytest.MonkeyPatch, user_factory: Any
) -> None:
    from site_app.views import _one_time_address_response, _one_time_key_response

    request = rf.get("/")
    request.user = user_factory()
    key_response = _one_time_key_response(
        request,
        key="st_test",
        agent_name="Agent",
        subject_name="Instance",
        return_url="/app/agents/",
    )
    address_response = _one_time_address_response(
        request,
        address="address",
        display_value="display",
        generation=1,
        subject_name="Tunnel",
        return_url="/app/tunnels/",
    )
    assert key_response["Cache-Control"] == address_response["Cache-Control"] == "no-store, private"
