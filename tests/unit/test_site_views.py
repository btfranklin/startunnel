"""Admin pages expose one equal instance-administrator interface."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpRequest
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from agents.models import AgentCredential
from core.errors import InvalidRequest, QuotaExceeded, TunnelUnavailable
from site_app.views import _authenticated_user
from tunnels.admin_actions import rollover_cycle_as_admin
from tunnels.cycles import create_tunnel
from tunnels.models import Tunnel

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


def test_admin_pages_require_login_and_render(client: Client, user_factory: Any) -> None:
    for path in ["/app/", "/app/learn/", "/app/agents/", "/app/tunnels/", "/app/account/"]:
        assert client.get(path).status_code == 302
    user = user_factory()
    client.force_login(user)
    for path in ["/app/", "/app/learn/", "/app/agents/", "/app/tunnels/", "/app/account/"]:
        assert client.get(path).status_code == 200


def test_cycle_history_escapes_message_session(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    credential, _ = credential_factory(user=actor, name="Shared agent")
    create_tunnel(
        credential=credential,
        idempotency_key="site-session-root",
        root_content={"type": "text", "text": "Root"},
        root_session="<img src=x onerror=alert(1)>",
    )
    client.force_login(actor)
    page = client.get("/app/tunnels/")
    assert page.status_code == 200
    assert b"Shared agent[&lt;img src=x onerror=alert(1)&gt;]" in page.content
    assert b"<img src=x onerror=alert(1)>" not in page.content


def test_tunnel_board_reads_related_rows_with_a_fixed_query_count(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    first = create_tunnel(
        credential=credential,
        idempotency_key="site-query-first",
        root_content={"type": "text", "text": "First root"},
    )
    client.force_login(actor)
    with CaptureQueriesContext(connection) as one_tunnel_queries:
        first_page = client.get("/app/tunnels/")
    assert first_page.status_code == 200
    assert first_page.context["tunnel_rows"][0]["current_address"].address == first.address

    created_at = timezone.now()
    created_tunnels = []
    for index in range(201):
        created_tunnels.append(
            create_tunnel(
                credential=credential,
                idempotency_key=f"site-query-many-{index}",
                root_content={"type": "text", "text": f"Root {index}"},
                label=f"Tunnel page {index}",
            )
        )
    for index, created in enumerate(created_tunnels, start=1):
        Tunnel.objects.filter(pk=created.tunnel.pk).update(
            created_at=created_at + timedelta(seconds=index)
        )
    with CaptureQueriesContext(connection) as many_tunnel_queries:
        many_page = client.get("/app/tunnels/")
    assert many_page.status_code == 200
    assert len(many_page.context["tunnel_rows"]) == 200
    assert len(many_tunnel_queries) == len(one_tunnel_queries)
    older_page = client.get("/app/tunnels/?page=2")
    assert older_page.status_code == 200
    assert b"Tunnel page 0" in older_page.content
    assert b"Tunnel page 199" not in older_page.content


def test_tunnel_board_keeps_the_latest_ten_cycles_per_tunnel(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    tunnels = [
        create_tunnel(
            credential=credential,
            idempotency_key=f"site-history-{index}",
            root_content={"type": "text", "text": f"Initial root {index}"},
        )
        for index in range(2)
    ]
    expected_sessions: dict[Any, list[str]] = {}
    for tunnel_index, created in enumerate(tunnels):
        current_cycle = created.cycle
        sessions = []
        for cycle_number in range(2, 13):
            session = f"session-{tunnel_index}-{cycle_number}"
            sessions.append(session)
            result = rollover_cycle_as_admin(
                actor=actor,
                tunnel_id=created.tunnel.id,
                expected_cycle_id=current_cycle.id,
                expected_address_generation=1,
                root_content={"type": "text", "text": f"Root {session}"},
                root_session=session,
            )
            current_cycle = result.cycle
        expected_sessions[created.tunnel.id] = sessions

    client.force_login(actor)
    response = client.get("/app/tunnels/")
    assert response.status_code == 200
    rows = {row["tunnel"].id: row for row in response.context["tunnel_rows"]}
    assert set(rows) == {created.tunnel.id for created in tunnels}
    for created in tunnels:
        row = rows[created.tunnel.id]
        assert row["cycle"].number == 12
        assert [cycle.number for cycle in row["cycles"]] == list(range(12, 2, -1))
        assert [cycle.root_message.session for cycle in row["cycles"]] == list(
            reversed(expected_sessions[created.tunnel.id][1:])
        )


def test_create_agent_shows_key_once_and_revoke_is_instance_wide(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    actor = user_factory()
    client.force_login(actor)
    created = client.post(
        "/app/agents/create/", {"name": "Build agent", "idempotency_key": str(uuid4())}
    )
    assert created.status_code == 200
    assert created["Cache-Control"].startswith("no-store, private")
    credential = AgentCredential.objects.get(name="Build agent")
    assert credential.created_by == actor
    other, _ = credential_factory(name="Other agent")
    assert (
        client.post(
            "/app/agents/revoke/", {"credential_id": other.id, "idempotency_key": str(uuid4())}
        ).status_code
        == 302
    )
    other.refresh_from_db()
    assert other.revoked_at is not None
    assert client.post("/app/agents/revoke/", {"credential_id": uuid4()}).status_code == 404


def test_agent_page_renders_form_and_domain_errors(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    assert client.post("/app/agents/create/", {"name": ""}).status_code == 400
    monkeypatch.setattr(
        "accounts.admin_commands.create_credential",
        lambda **kwargs: (_ for _ in ()).throw(QuotaExceeded("No capacity.")),
    )
    response = client.post(
        "/app/agents/create/", {"name": "Agent", "idempotency_key": str(uuid4())}
    )
    assert response.status_code == 400
    assert b"No capacity" in response.content


@pytest.mark.parametrize("credential_id", ["", "not-a-uuid", "é"])
def test_agent_revoke_rejects_malformed_ids(
    client: Client, user_factory: Any, credential_factory: Any, credential_id: str
) -> None:
    actor = user_factory()
    credential, _ = credential_factory(user=actor)
    client.force_login(actor)
    response = client.post(
        "/app/agents/revoke/",
        {"credential_id": credential_id, "idempotency_key": str(uuid4())},
    )
    assert response.status_code == 404
    credential.refresh_from_db()
    assert credential.revoked_at is None


def test_expired_and_revoked_credentials_are_not_active(
    client: Client, user_factory: Any, credential_factory: Any
) -> None:
    client.force_login(user_factory())
    expired, _ = credential_factory(name="Expired agent")
    expired.expires_at = timezone.now() - timedelta(seconds=1)
    expired.save(update_fields=["expires_at"])
    revoked, _ = credential_factory(name="Revoked agent")
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])

    dashboard = client.get("/app/")
    agents = client.get("/app/agents/")
    assert dashboard.context["active_agent_count"] == 0
    assert b"Expired" in agents.content
    assert b"Revoked" in agents.content


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

    monkeypatch.setattr("site_app.views.execute_admin_operation", capture)
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


def test_tunnel_rotate_returns_to_tunnel_board(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    monkeypatch.setattr(
        "site_app.views.execute_admin_operation",
        lambda **kwargs: {"resource": {"address": "safe-address", "address_generation": 2}},
    )
    response = client.post(
        "/app/tunnels/",
        {"action": "rotate", "tunnel_id": uuid4(), "expected_address_generation": "1"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/app/tunnels/"


def test_tunnel_page_maps_bad_input_and_domain_errors(
    client: Client, user_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.force_login(user_factory())
    assert client.post("/app/tunnels/", {"action": "bad", "tunnel_id": uuid4()}).status_code == 302
    assert client.post("/app/tunnels/", {"action": "close", "tunnel_id": "bad"}).status_code == 302
    monkeypatch.setattr(
        "site_app.views.execute_admin_operation",
        lambda **kwargs: (_ for _ in ()).throw(TunnelUnavailable()),
    )
    response = client.post(
        "/app/tunnels/",
        {"action": "close", "tunnel_id": uuid4(), "expected_cycle_id": uuid4()},
    )
    assert response.status_code == 302
    monkeypatch.setattr(
        "site_app.views.execute_admin_operation",
        lambda **kwargs: (_ for _ in ()).throw(InvalidRequest("Invalid operation.")),
    )
    assert (
        client.post(
            "/app/tunnels/",
            {"action": "close", "tunnel_id": uuid4(), "expected_cycle_id": uuid4()},
        ).status_code
        == 302
    )
