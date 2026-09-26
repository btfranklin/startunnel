"""The public API exposes stable tunnels and immutable cycle trees."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import IntegrityError, OperationalError
from django.test import Client
from django.utils import timezone

from tunnels.codec import encode_token
from tunnels.errors import RateLimited
from tunnels.models import Cycle, Message, Tunnel, TunnelAddress

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_api_from_database_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)
    monkeypatch.setattr("api.router.consume_tunnel_creation", lambda credential, limits: None)
    monkeypatch.setattr(
        "api.router.consume_address_miss",
        lambda credential, limits, *, source_ip: None,
    )


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _post(
    client: Client,
    path: str,
    data: dict[str, Any],
    *,
    key: str,
    idempotency_key: str | None = None,
) -> Any:
    headers = _auth(key)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        path,
        data=json.dumps(data),
        content_type="application/json",
        headers=headers,
    )


def _create_request(*, text: str = "Coordinate the release review.") -> dict[str, Any]:
    return {
        "label": "Release review",
        "cycle": {
            "label": "Initial review",
            "expires_in_seconds": 3600,
            "root": {
                "content": {"type": "text", "text": text},
                "mentions": [],
                "correlation_id": "release-42",
            },
        },
    }


def _create_global(
    client: Client, key: str, idempotency_key: str = "api-create-0001"
) -> dict[str, Any]:
    response = _post(
        client,
        "/api/v1/tunnels",
        _create_request(),
        key=key,
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 201, response.content
    return cast(dict[str, Any], response.json())


def _reply(
    client: Client,
    *,
    key: str,
    address: str,
    parent_id: str,
    idempotency_key: str,
    text: str,
    path: str = "/api/v1/messages",
) -> Any:
    return _post(
        client,
        path,
        {
            "address": address,
            "parent_id": parent_id,
            "content": {"type": "text", "text": text},
            "correlation_id": "release-42",
        },
        key=key,
        idempotency_key=idempotency_key,
    )


def _assert_safe_error(response: Any, *, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body = cast(dict[str, Any], response.json())
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["request_id"] == response["X-Request-ID"]
    assert isinstance(body["error"]["field_errors"], list)
    assert response["Content-Security-Policy"]
    assert response["Permissions-Policy"]
    assert response["X-Frame-Options"] == "DENY"
    return body


def test_me_returns_tree_limits_usage_and_security_headers(
    client: Client, credential_factory: Any
) -> None:
    credential, key = credential_factory(name="Status agent")
    response = client.get("/api/v1/me", headers=_auth(key))
    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["id"] == str(credential.id)
    assert body["agent"]["name"] == "Status agent"
    assert credential.created_by.username not in json.dumps(body)
    assert "plan" not in body
    assert body["limits"]["messages_per_cycle"] == 10_000
    assert body["limits"]["maximum_tree_depth"] == 128
    assert body["limits"]["maximum_search_results"] == 100
    assert body["limits"]["maximum_search_query_characters"] == 512
    assert body["usage"] == {"tunnels": 0}
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response["Content-Security-Policy"]


def test_missing_malformed_and_unavailable_credentials_use_safe_errors(
    client: Client, credential_factory: Any
) -> None:
    _assert_safe_error(client.get("/api/v1/me"), status=401, code="invalid_credential")
    malformed = client.get("/api/v1/me", headers=_auth("not-a-key"))
    _assert_safe_error(malformed, status=401, code="invalid_credential")

    credential, key = credential_factory()
    credential.revoked_at = timezone.now()
    credential.save(update_fields=["revoked_at"])
    revoked = client.get("/api/v1/me", headers=_auth(key))
    _assert_safe_error(revoked, status=401, code="invalid_credential")


def test_create_is_atomic_rooted_and_idempotent(client: Client, credential_factory: Any) -> None:
    credential, key = credential_factory(name="Coordinator")
    created = _create_global(client, key, "api-create-rooted")
    assert "scope" not in created["tunnel"]
    assert created["tunnel"]["state"] == "active"
    assert len(created["tunnel"]["address"]) == 24
    assert created["cycle"]["number"] == 1
    assert created["cycle"]["root"]["parent_id"] is None
    assert created["cycle"]["root"]["sequence"] == 1
    assert created["cycle"]["root"]["depth"] == 0
    assert created["cycle"]["message_count"] == 1
    assert created["activity_cursor"]

    tunnel = Tunnel.objects.get(pk=created["tunnel"]["id"])
    cycle = Cycle.objects.get(pk=created["cycle"]["id"])
    root = Message.objects.get(pk=created["cycle"]["root"]["id"])
    assert tunnel.creator == credential
    assert cycle.tunnel == tunnel
    assert cycle.root_message == root
    assert root.cycle == cycle
    assert root.parent is None
    assert len(bytes(TunnelAddress.objects.get(tunnel=tunnel).address_digest)) == 32

    replay = _post(
        client,
        "/api/v1/tunnels",
        _create_request(),
        key=key,
        idempotency_key="api-create-rooted",
    )
    assert replay.status_code == 201
    replay_body = replay.json()
    assert replay_body["idempotent_replay"] is True
    assert replay_body["tunnel"]["id"] == created["tunnel"]["id"]
    assert replay_body["tunnel"]["address"] == created["tunnel"]["address"]
    assert replay_body["cycle"]["id"] == created["cycle"]["id"]
    assert replay_body["cycle"]["root"]["id"] == created["cycle"]["root"]["id"]

    changed = _create_request(text="A different root.")
    conflict = _post(
        client,
        "/api/v1/tunnels",
        changed,
        key=key,
        idempotency_key="api-create-rooted",
    )
    _assert_safe_error(conflict, status=409, code="idempotency_conflict")


def test_create_replay_does_not_consume_creation_allowance(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, key = credential_factory()
    calls = 0

    def allow_only_first(credential: object, limits: object) -> None:
        nonlocal calls
        del credential, limits
        calls += 1
        if calls > 1:
            raise RateLimited()

    monkeypatch.setattr("api.router.consume_tunnel_creation", allow_only_first)
    _create_global(client, key, "api-create-rate-replay")
    replay = _post(
        client,
        "/api/v1/tunnels",
        _create_request(),
        key=key,
        idempotency_key="api-create-rate-replay",
    )
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert calls == 1

    limited = _post(
        client,
        "/api/v1/tunnels",
        _create_request(),
        key=key,
        idempotency_key="api-create-rate-new",
    )
    _assert_safe_error(limited, status=429, code="rate_limited")


def test_create_replay_keeps_the_original_wire_state_after_later_activity(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    request = _create_request()
    first = _post(
        client,
        "/api/v1/tunnels",
        request,
        key=key,
        idempotency_key="api-create-exact-later",
    )
    assert first.status_code == 201
    original = first.json()
    reply = _reply(
        client,
        key=key,
        address=original["tunnel"]["address"],
        parent_id=original["cycle"]["root"]["id"],
        idempotency_key="api-create-exact-reply",
        text="Later activity must not change the replay.",
    )
    assert reply.status_code == 201
    closed = _post(
        client,
        "/api/v1/cycles/close",
        {
            "address": original["tunnel"]["address"],
            "expected_cycle_id": original["cycle"]["id"],
        },
        key=key,
        idempotency_key="api-create-exact-close",
    )
    assert closed.status_code == 204

    replay = _post(
        client,
        "/api/v1/tunnels",
        request,
        key=key,
        idempotency_key="api-create-exact-later",
    )
    assert replay.status_code == 201
    replay_body = replay.json()
    assert replay_body["idempotent_replay"] is True
    original["idempotent_replay"] = True
    assert replay_body == original


def test_agents_build_and_navigate_one_immutable_tree(
    client: Client, credential_factory: Any
) -> None:
    _, coordinator_key = credential_factory(name="Coordinator")
    worker, worker_key = credential_factory(name="Worker")
    created = _create_global(client, coordinator_key, "api-tree-create")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]

    first = _reply(
        client,
        key=worker_key,
        address=address,
        parent_id=root_id,
        idempotency_key="api-tree-first",
        text="I found a missing restore assertion.",
    )
    assert first.status_code == 201
    first_id = first.json()["message"]["id"]
    assert first.json()["message"]["sequence"] == 2
    assert first.json()["message"]["depth"] == 1

    nested = _post(
        client,
        "/api/v1/messages",
        {
            "address": address,
            "parent_id": first_id,
            "content": {"type": "json", "value": {"evidence": ["restore.sh:83"]}},
            "mentions": [str(worker.id)],
            "correlation_id": "release-42",
        },
        key=coordinator_key,
        idempotency_key="api-tree-nested",
    )
    assert nested.status_code == 201
    nested_id = nested.json()["message"]["id"]
    assert nested.json()["message"]["depth"] == 2

    tree = _post(client, "/api/v1/tree", {"address": address}, key=worker_key)
    assert tree.status_code == 200
    assert [node["id"] for node in tree.json()["nodes"]] == [root_id, first_id, nested_id]
    assert [node["sequence"] for node in tree.json()["nodes"]] == [1, 2, 3]
    assert tree.json()["nodes"][2]["content"] == {
        "type": "json",
        "value": {"evidence": ["restore.sh:83"]},
    }
    assert tree.json()["nodes"][2]["mentions"] == [{"id": str(worker.id), "name": "Worker"}]
    assert tree.json()["complete"] is True
    assert tree.json()["stats"]["messages"] == 3

    exact = _post(
        client,
        "/api/v1/tree/message",
        {"address": address, "message_id": first_id},
        key=worker_key,
    )
    assert exact.status_code == 200
    assert exact.json()["message"]["id"] == first_id
    assert exact.json()["child_count"] == 1

    branch = _post(
        client,
        "/api/v1/tree/branch",
        {"address": address, "leaf_id": nested_id},
        key=worker_key,
    )
    assert branch.status_code == 200
    assert [item["id"] for item in branch.json()["messages"]] == [root_id, first_id, nested_id]

    replies = _post(
        client,
        "/api/v1/tree/replies",
        {"address": address, "message_id": root_id},
        key=worker_key,
    )
    assert replies.status_code == 200
    assert [item["id"] for item in replies.json()["messages"]] == [first_id]


def test_tree_snapshot_pagination_excludes_later_replies(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create_global(client, key, "api-snapshot-create")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    first = _reply(
        client,
        key=key,
        address=address,
        parent_id=root_id,
        idempotency_key="api-snapshot-first",
        text="First",
    ).json()

    page_one = _post(
        client,
        "/api/v1/tree",
        {"address": address, "limit": 1},
        key=key,
    )
    assert page_one.status_code == 200
    first_page = page_one.json()
    assert [item["id"] for item in first_page["nodes"]] == [root_id]
    assert first_page["complete"] is False
    assert first_page["next_page_cursor"]

    later = _reply(
        client,
        key=key,
        address=address,
        parent_id=root_id,
        idempotency_key="api-snapshot-later",
        text="Committed after the snapshot",
    ).json()

    page_two = _post(
        client,
        "/api/v1/tree",
        {
            "address": address,
            "limit": 10,
            "snapshot_cursor": first_page["snapshot_cursor"],
            "page_cursor": first_page["next_page_cursor"],
        },
        key=key,
    )
    assert page_two.status_code == 200
    assert [item["id"] for item in page_two.json()["nodes"]] == [first["message"]["id"]]
    assert later["message"]["id"] not in json.dumps(page_two.json())
    assert page_two.json()["complete"] is True

    current = _post(client, "/api/v1/tree", {"address": address}, key=key)
    assert current.status_code == 200
    assert [item["sequence"] for item in current.json()["nodes"]] == [1, 2, 3]


def test_direct_reply_pagination_is_complete_and_excludes_later_siblings(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create_global(client, key, "api-reply-page-create")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    reply_ids = [
        _reply(
            client,
            key=key,
            address=address,
            parent_id=root_id,
            idempotency_key=f"api-reply-page-{index}",
            text=f"Reply {index}",
        ).json()["message"]["id"]
        for index in range(3)
    ]

    first = _post(
        client,
        "/api/v1/tree/replies",
        {"address": address, "message_id": root_id, "limit": 2},
        key=key,
    )
    assert first.status_code == 200
    first_page = first.json()
    assert [message["id"] for message in first_page["messages"]] == reply_ids[:2]
    assert first_page["complete"] is False
    assert first_page["snapshot_cursor"]
    assert first_page["next_page_cursor"]

    later_id = _reply(
        client,
        key=key,
        address=address,
        parent_id=root_id,
        idempotency_key="api-reply-page-later",
        text="Later sibling",
    ).json()["message"]["id"]
    second = _post(
        client,
        "/api/v1/tree/replies",
        {
            "address": address,
            "message_id": root_id,
            "limit": 2,
            "snapshot_cursor": first_page["snapshot_cursor"],
            "page_cursor": first_page["next_page_cursor"],
        },
        key=key,
    )
    assert second.status_code == 200
    assert [message["id"] for message in second.json()["messages"]] == reply_ids[2:]
    assert later_id not in json.dumps(second.json())
    assert second.json()["complete"] is True
    assert second.json()["next_page_cursor"] is None

    wrong_parent = _post(
        client,
        "/api/v1/tree/replies",
        {
            "address": address,
            "message_id": reply_ids[0],
            "snapshot_cursor": first_page["snapshot_cursor"],
        },
        key=key,
    )
    assert wrong_parent.status_code == 400
    assert wrong_parent.json()["error"]["code"] == "invalid_request"


def test_direct_reply_page_serialization_has_one_bounded_query_budget(
    client: Client,
    credential_factory: Any,
    django_assert_num_queries: Any,
) -> None:
    _, key = credential_factory(name="Query-bound reader")
    created = _create_global(client, key, "api-reply-query-create")
    address = created["tunnel"]["address"]
    root_id = created["cycle"]["root"]["id"]
    for index in range(25):
        response = _reply(
            client,
            key=key,
            address=address,
            parent_id=root_id,
            idempotency_key=f"api-reply-query-{index}",
            text=f"Bounded reply {index}",
        )
        assert response.status_code == 201

    with django_assert_num_queries(15, exact=False):
        response = _post(
            client,
            "/api/v1/tree/replies",
            {"address": address, "message_id": root_id, "limit": 25},
            key=key,
        )

    assert response.status_code == 200
    assert len(response.json()["messages"]) == 25


def test_reply_requires_parent_in_current_tunnel(client: Client, credential_factory: Any) -> None:
    _, key = credential_factory()
    first = _create_global(client, key, "api-parent-first")
    second = _create_global(client, key, "api-parent-second")
    response = _reply(
        client,
        key=key,
        address=first["tunnel"]["address"],
        parent_id=second["cycle"]["root"]["id"],
        idempotency_key="api-cross-tunnel-parent",
        text="This parent is in another tunnel.",
    )
    _assert_safe_error(response, status=400, code="invalid_request")
    assert Message.objects.filter(tunnel_id=first["tunnel"]["id"]).count() == 1


def test_reply_is_idempotent_and_same_agent_can_reply_to_itself(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory(name="Solo agent")
    created = _create_global(client, key, "api-self-create")
    request = {
        "address": created["tunnel"]["address"],
        "parent_id": created["cycle"]["root"]["id"],
        "content": {"type": "text", "text": "Self reply"},
    }
    first = _post(
        client,
        "/api/v1/messages",
        request,
        key=key,
        idempotency_key="api-self-reply",
    )
    assert first.status_code == 201
    replay = _post(
        client,
        "/api/v1/messages",
        request,
        key=key,
        idempotency_key="api-self-reply",
    )
    assert replay.status_code == 201
    assert replay.json()["message"]["id"] == first.json()["message"]["id"]
    assert replay.json()["idempotent_replay"] is True
    assert Message.objects.filter(cycle_id=created["cycle"]["id"]).count() == 2

    changed = {**request, "content": {"type": "text", "text": "Changed"}}
    conflict = _post(
        client,
        "/api/v1/messages",
        changed,
        key=key,
        idempotency_key="api-self-reply",
    )
    _assert_safe_error(conflict, status=409, code="idempotency_conflict")


def test_cycle_close_is_creator_only_and_tree_remains_read_only(
    client: Client, credential_factory: Any
) -> None:
    _, creator_key = credential_factory(name="Creator")
    _, outsider_key = credential_factory(name="Outsider")
    created = _create_global(client, creator_key, "api-close-create")
    body = {
        "address": created["tunnel"]["address"],
        "expected_cycle_id": created["cycle"]["id"],
    }
    outside = _post(
        client,
        "/api/v1/cycles/close",
        body,
        key=outsider_key,
        idempotency_key="api-close-outside",
    )
    _assert_safe_error(outside, status=404, code="tunnel_unavailable")

    closed = _post(
        client,
        "/api/v1/cycles/close",
        body,
        key=creator_key,
        idempotency_key="api-close-owner",
    )
    assert closed.status_code == 204
    assert Tunnel.objects.get(pk=created["tunnel"]["id"]).state == Tunnel.State.DORMANT
    assert Cycle.objects.get(pk=created["cycle"]["id"]).state == Cycle.State.CLOSED

    post_after_close = _reply(
        client,
        key=creator_key,
        address=created["tunnel"]["address"],
        parent_id=created["cycle"]["root"]["id"],
        idempotency_key="api-close-reply",
        text="Too late",
    )
    _assert_safe_error(post_after_close, status=409, code="cycle_closed")

    history = _post(
        client,
        "/api/v1/tree",
        {"address": created["tunnel"]["address"]},
        key=outsider_key,
    )
    assert history.status_code == 200
    assert history.json()["cycle"]["state"] == "closed"
    assert history.json()["nodes"][0]["id"] == created["cycle"]["root"]["id"]


def test_invalid_unknown_and_tampered_cursor_errors_are_safe(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    invalid = _post(
        client,
        "/api/v1/tree",
        {"address": "not glyphs"},
        key=key,
    )
    _assert_safe_error(invalid, status=400, code="invalid_address")
    unknown = _post(
        client,
        "/api/v1/tree",
        {"address": encode_token(bytes(range(16)))},
        key=key,
    )
    _assert_safe_error(unknown, status=404, code="tunnel_unavailable")

    created = _create_global(client, key, "api-cursor-create")
    tampered = _post(
        client,
        "/api/v1/tree",
        {"address": created["tunnel"]["address"], "snapshot_cursor": "not-a-cursor"},
        key=key,
    )
    _assert_safe_error(tampered, status=400, code="invalid_cursor")


def test_create_post_and_close_require_idempotency_key(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    create = _post(client, "/api/v1/tunnels", _create_request(), key=key)
    _assert_safe_error(create, status=400, code="invalid_request")
    created = _create_global(client, key, "api-idempotency-create")
    post = _post(
        client,
        "/api/v1/messages",
        {
            "address": created["tunnel"]["address"],
            "parent_id": created["cycle"]["root"]["id"],
            "content": {"type": "text", "text": "Missing key"},
        },
        key=key,
    )
    _assert_safe_error(post, status=400, code="invalid_request")
    close = _post(
        client,
        "/api/v1/cycles/close",
        {
            "address": created["tunnel"]["address"],
            "expected_cycle_id": created["cycle"]["id"],
        },
        key=key,
    )
    _assert_safe_error(close, status=400, code="invalid_request")


def test_root_and_reply_payload_limits_return_413(client: Client, credential_factory: Any) -> None:
    _, key = credential_factory()
    oversized_root = _post(
        client,
        "/api/v1/tunnels",
        _create_request(text="x" * 65_537),
        key=key,
        idempotency_key="api-large-root",
    )
    _assert_safe_error(oversized_root, status=413, code="payload_too_large")

    created = _create_global(client, key, "api-large-reply-create")
    oversized_reply = _reply(
        client,
        key=key,
        address=created["tunnel"]["address"],
        parent_id=created["cycle"]["root"]["id"],
        idempotency_key="api-large-reply",
        text="x" * 65_537,
    )
    _assert_safe_error(oversized_reply, status=413, code="payload_too_large")


def test_validation_framework_and_dependency_errors_use_public_envelope(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, key = credential_factory()
    bad_label = _create_request()
    bad_label["cycle"]["label"] = []
    validation = _post(
        client,
        "/api/v1/tunnels",
        bad_label,
        key=key,
        idempotency_key="api-validation-fields",
    )
    body = _assert_safe_error(validation, status=400, code="invalid_request")
    assert any("cycle.label" in error["field"] for error in body["error"]["field_errors"])

    unknown_route = client.get("/api/v1/not-a-route", headers=_auth(key))
    _assert_safe_error(unknown_route, status=404, code="invalid_request")

    def fail_authentication(raw_key: str) -> None:
        del raw_key
        raise OperationalError("safe test database failure")

    monkeypatch.setattr("api.auth.authenticate_key", fail_authentication)
    dependency = client.get("/api/v1/me", headers=_auth(key))
    _assert_safe_error(dependency, status=503, code="dependency_unavailable")


def test_integrity_failure_is_not_reported_as_dependency_outage(
    client: Client,
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, key = credential_factory()
    created = _create_global(client, key, "api-integrity-create")

    def fail_read(**kwargs: Any) -> None:
        del kwargs
        raise IntegrityError("safe test invariant failure")

    monkeypatch.setattr("api.router.read_tree", fail_read)
    client.raise_request_exception = False
    response = _post(
        client,
        "/api/v1/tree",
        {"address": created["tunnel"]["address"]},
        key=key,
    )
    _assert_safe_error(response, status=500, code="internal_error")


def test_expired_cycle_rejects_reply_without_mutation(
    client: Client, credential_factory: Any
) -> None:
    _, key = credential_factory()
    created = _create_global(client, key, "api-expired-cycle")
    Cycle.objects.filter(pk=created["cycle"]["id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    response = _reply(
        client,
        key=key,
        address=created["tunnel"]["address"],
        parent_id=created["cycle"]["root"]["id"],
        idempotency_key="api-expired-reply",
        text="Too late",
    )
    _assert_safe_error(response, status=409, code="cycle_closed")
    assert Message.objects.filter(cycle_id=created["cycle"]["id"]).count() == 1
