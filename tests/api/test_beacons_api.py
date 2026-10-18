"""Beacon API routes use the shared domain and keep addresses in bodies."""

from __future__ import annotations

import json
from typing import Any

import pytest

from accounts.admin_access import issue_admin_key

pytestmark = pytest.mark.django_db


def _post(client: Any, path: str, body: dict[str, Any], key: str, retry: str | None = None) -> Any:
    headers = {"HTTP_AUTHORIZATION": "Bearer " + key}
    if retry is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = retry
    return client.post(path, data=json.dumps(body), content_type="application/json", **headers)


def test_agent_board_publication_and_withdrawal(
    client: Any, credential_factory: Any, user_factory: Any
) -> None:
    creator, creator_key = credential_factory()
    _, reader_key = credential_factory()
    owner = user_factory()
    _, admin_key = issue_admin_key(owner=owner, name="beacon-admin")
    created = _post(
        client,
        "/api/v1/tunnels",
        {"label": "Beacon API", "cycle": {"root": {"content": {"type": "text", "text": "Root"}}}},
        creator_key,
        "beacon-api-create",
    )
    assert created.status_code == 201, created.content
    address = created.json()["tunnel"]["address"]
    published = _post(
        client,
        "/api/v1/beacons",
        {"address": address, "text": "Please review this cycle."},
        creator_key,
        "beacon-api-publish",
    )
    assert published.status_code == 201, published.content
    beacon = published.json()["beacon"]
    assert beacon["publisher"]["id"] == str(creator.id)
    assert "address" not in beacon
    assert _post(
        client,
        "/api/v1/beacons",
        {"address": address, "text": "Please review this cycle."},
        creator_key,
        "beacon-api-publish",
    ).json()["idempotent_replay"]
    changed = _post(
        client,
        "/api/v1/beacons",
        {"address": address, "text": "Different text."},
        creator_key,
        "beacon-api-publish",
    )
    assert changed.status_code == 409
    board = _post(client, "/api/v1/beacons/board", {}, reader_key)
    assert board.status_code == 200, board.content
    assert board.json()["items"][0]["address"] == address
    assert board.json()["items"][0]["response_count"] == 0
    assert (
        client.get("/api/v1/admin/beacons", HTTP_AUTHORIZATION="Bearer " + admin_key).json()[
            "items"
        ][0]["id"]
        == beacon["id"]
    )
    assert (
        client.get(
            "/api/v1/admin/beacons/" + beacon["id"],
            HTTP_AUTHORIZATION="Bearer " + admin_key,
        ).json()["id"]
        == beacon["id"]
    )
    withdrawn = _post(
        client,
        "/api/v1/beacons/withdraw",
        {"address": address, "beacon_id": beacon["id"]},
        creator_key,
        "beacon-api-withdraw",
    )
    assert withdrawn.status_code == 200, withdrawn.content
    assert not _post(client, "/api/v1/beacons/board", {}, reader_key).json()["items"]


def test_admin_withdraws_beacon_with_receipt(
    client: Any, credential_factory: Any, user_factory: Any
) -> None:
    _, creator_key = credential_factory()
    owner = user_factory()
    _, admin_key = issue_admin_key(owner=owner, name="beacon-admin")
    created = _post(
        client,
        "/api/v1/tunnels",
        {"cycle": {"root": {"content": {"type": "text", "text": "Root"}}}},
        creator_key,
        "beacon-admin-create",
    )
    assert created.status_code == 201, created.content
    address = created.json()["tunnel"]["address"]
    published = _post(
        client,
        "/api/v1/beacons",
        {"address": address, "text": "Need review."},
        creator_key,
        "beacon-admin-publish",
    )
    beacon_id = published.json()["beacon"]["id"]
    result = _post(
        client,
        "/api/v1/admin/beacons/withdraw",
        {"beacon_id": beacon_id},
        admin_key,
        "beacon-admin-withdraw",
    )
    assert result.status_code == 200, result.content
    assert result.json()["resource"]["id"] == beacon_id
    assert (
        _post(
            client,
            "/api/v1/admin/beacons/withdraw",
            {"beacon_id": beacon_id},
            admin_key,
            "beacon-admin-withdraw",
        ).json()
        == result.json()
    )


def test_beacon_routes_require_an_active_agent_and_preserve_publication_on_revocation(
    client: Any, credential_factory: Any
) -> None:
    from django.utils import timezone

    from tunnels.services import create_tunnel

    creator, key = credential_factory()
    _, reader_key = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="beacon-auth-create",
        root_content={"type": "text", "text": "Review request."},
    )
    body = {"address": created.address, "text": "Review request."}
    assert (
        _post(client, "/api/v1/beacons", body, reader_key, "beacon-other-publish").status_code
        == 404
    )
    published = _post(client, "/api/v1/beacons", body, key, "beacon-auth-publish")
    assert published.status_code == 201
    beacon_id = published.json()["beacon"]["id"]
    withdraw = {"address": created.address, "beacon_id": beacon_id}
    assert (
        _post(
            client, "/api/v1/beacons/withdraw", withdraw, reader_key, "beacon-other-withdraw"
        ).status_code
        == 404
    )
    creator.revoked_at = timezone.now()
    creator.save(update_fields=["revoked_at"])
    assert _post(client, "/api/v1/beacons/board", {}, key).status_code == 401
    assert (
        _post(client, "/api/v1/beacons/withdraw", withdraw, key, "beacon-auth-withdraw").status_code
        == 401
    )
    assert (
        _post(client, "/api/v1/beacons/board", {}, reader_key).json()["items"][0]["id"] == beacon_id
    )


@pytest.mark.django_db(transaction=True)
def test_beacon_events_are_available_through_normal_activity_and_retry_is_not_a_republication(
    client: Any, credential_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _, key = credential_factory()
    created = _post(
        client,
        "/api/v1/tunnels",
        {"cycle": {"root": {"content": {"type": "text", "text": "Review request."}}}},
        key,
        "beacon-events-create",
    ).json()
    address = created["tunnel"]["address"]
    body = {"address": address, "text": "Review request."}
    first = _post(client, "/api/v1/beacons", body, key, "beacon-events-publish").json()["beacon"]
    end = _post(
        client,
        "/api/v1/beacons/withdraw",
        {"address": address, "beacon_id": first["id"]},
        key,
        "beacon-events-withdraw",
    )
    assert end.status_code == 200
    assert end.json()["beacon"]["end_reason"] == "withdrawn"
    retry = _post(client, "/api/v1/beacons", body, key, "beacon-events-publish").json()
    assert retry == {"beacon": first, "idempotent_replay": True}
    assert _post(client, "/api/v1/beacons/board", {}, key).json()["items"] == []
    events = _post(
        client,
        "/api/v1/activity",
        {"address": address, "event_types": ["beacon_lit", "beacon_ended"]},
        key,
    )
    assert events.status_code == 200
    assert [event["type"] for event in events.json()["events"]] == ["beacon_lit", "beacon_ended"]
    assert all(event["message"] is None for event in events.json()["events"])
    assert address not in caplog.text
    assert key not in caplog.text


def test_beacon_board_uses_the_agent_request_allowance(
    client: Any, credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from core.limits import effective_limits

    _, key = credential_factory()
    limits = replace(effective_limits(), api_operations_per_minute=1, api_burst=1)
    monkeypatch.setattr("core.limits.effective_limits", lambda: limits)
    assert _post(client, "/api/v1/beacons/board", {}, key).status_code == 200
    limited = _post(client, "/api/v1/beacons/board", {}, key)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


@pytest.mark.django_db(transaction=True)
def test_board_wait_returns_explicit_refresh_and_accepts_another_active_reader(
    client: Any, credential_factory: Any
) -> None:
    _, publisher_key = credential_factory()
    _, reader_key = credential_factory()
    initial = _post(client, "/api/v1/beacons/board", {}, publisher_key).json()
    unchanged = _post(
        client,
        "/api/v1/beacons/wait",
        {"after_cursor": initial["board_cursor"], "wait_seconds": 0},
        reader_key,
    )
    assert unchanged.status_code == 200
    assert unchanged.json() == {
        "refresh_required": False,
        "board_cursor": initial["board_cursor"],
    }
    created = _post(
        client,
        "/api/v1/tunnels",
        {"cycle": {"root": {"content": {"type": "text", "text": "Review request."}}}},
        publisher_key,
        "board-wait-api-create",
    ).json()
    published = _post(
        client,
        "/api/v1/beacons",
        {"address": created["tunnel"]["address"], "text": "Review request."},
        publisher_key,
        "board-wait-api-publish",
    )
    assert published.status_code == 201
    changed = _post(
        client,
        "/api/v1/beacons/wait",
        {"after_cursor": initial["board_cursor"], "wait_seconds": 0},
        reader_key,
    )
    assert changed.status_code == 200
    assert changed.json()["refresh_required"] is True
    assert set(changed.json()) == {"refresh_required", "board_cursor"}
    refreshed = _post(client, "/api/v1/beacons/board", {}, reader_key).json()
    assert len(refreshed["items"]) == 1
    assert (
        _post(
            client,
            "/api/v1/beacons/wait",
            {"after_cursor": refreshed["board_cursor"], "wait_seconds": 0},
            reader_key,
        ).json()["refresh_required"]
        is False
    )


@pytest.mark.django_db(transaction=True)
def test_board_wait_observes_expiry_without_maintenance(
    client: Any, credential_factory: Any
) -> None:
    from tunnels.beacons import publish_beacon
    from tunnels.models import Beacon
    from tunnels.services import create_tunnel

    creator, key = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="board-expiry-api-create",
        root_content={"type": "text", "text": "Review request."},
    )
    result, _ = publish_beacon(
        credential=creator,
        idempotency_key="board-expiry-api-publish",
        address=created.address,
        text="Review request.",
        lifetime_seconds=1,
    )
    page = _post(client, "/api/v1/beacons/board", {}, key).json()
    waited = _post(
        client,
        "/api/v1/beacons/wait",
        {"after_cursor": page["board_cursor"], "wait_seconds": 2},
        key,
    )
    assert waited.status_code == 200
    assert waited.json()["refresh_required"] is True
    assert Beacon.objects.get(pk=result["id"]).ended_at is None
    assert _post(client, "/api/v1/beacons/board", {}, key).json()["items"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "body",
    [{}, {"after_cursor": "invalid"}, {"after_cursor": "invalid", "wait_seconds": 21}],
)
def test_board_wait_rejects_invalid_requests(
    client: Any, credential_factory: Any, body: dict[str, Any]
) -> None:
    _, key = credential_factory()
    assert _post(client, "/api/v1/beacons/wait", body, key).status_code == 400
