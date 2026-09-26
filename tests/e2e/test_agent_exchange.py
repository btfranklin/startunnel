"""Deterministic agents collaborate through stable public-API trees."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client

from tunnels.models import Cycle, Message, Tunnel

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_api_from_database_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)
    monkeypatch.setattr("api.router.consume_tunnel_creation", lambda credential, limits: None)
    monkeypatch.setattr(
        "api.router.consume_address_miss",
        lambda credential, limits, *, source_ip: None,
    )


def _headers(key: str, idempotency: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    return headers


def _post(
    client: Client,
    path: str,
    body: dict[str, Any],
    key: str,
    idempotency: str | None = None,
) -> Any:
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers=_headers(key, idempotency),
    )


def _create_body(label: str, root_text: str) -> dict[str, Any]:
    return {
        "label": label,
        "cycle": {
            "label": "Initial collaboration",
            "root": {
                "content": {"type": "text", "text": root_text},
                "correlation_id": "proof-42",
            },
        },
    }


def test_deterministic_global_agents_build_and_retain_a_tree(
    client: Client,
    credential_factory: Any,
) -> None:
    _, sender_key = credential_factory(name="Agent A")
    _, receiver_key = credential_factory(name="Agent B")
    created_response = _post(
        client,
        "/api/v1/tunnels",
        _create_body("Deterministic proof", "Review artifact proof."),
        sender_key,
        "e2e-global-create",
    )
    assert created_response.status_code == 201
    created = created_response.json()
    tunnel = created["tunnel"]
    cycle = created["cycle"]
    address = tunnel["address"]
    root_id = cycle["root"]["id"]

    reply_response = _post(
        client,
        "/api/v1/messages",
        {
            "address": address,
            "parent_id": root_id,
            "content": {
                "type": "json",
                "value": {"finding": "The artifact is ready for release."},
            },
            "correlation_id": "proof-42",
        },
        receiver_key,
        "e2e-global-reply",
    )
    assert reply_response.status_code == 201
    reply_id = reply_response.json()["message"]["id"]

    nested_response = _post(
        client,
        "/api/v1/messages",
        {
            "address": address,
            "parent_id": reply_id,
            "content": {"type": "text", "text": "Agent A accepted the finding."},
            "correlation_id": "proof-42",
        },
        sender_key,
        "e2e-global-nested-reply",
    )
    assert nested_response.status_code == 201
    nested_id = nested_response.json()["message"]["id"]

    sender_tree = _post(client, "/api/v1/tree", {"address": address}, sender_key)
    receiver_tree = _post(client, "/api/v1/tree", {"address": address}, receiver_key)
    assert sender_tree.status_code == 200
    assert receiver_tree.status_code == 200
    for response in (sender_tree, receiver_tree):
        body = response.json()
        assert body["root_id"] == root_id
        assert [node["id"] for node in body["nodes"]] == [root_id, reply_id, nested_id]
        assert [node["parent_id"] for node in body["nodes"]] == [None, root_id, reply_id]
        assert body["stats"]["messages"] == 3

    closed = _post(
        client,
        "/api/v1/cycles/close",
        {"address": address, "expected_cycle_id": cycle["id"]},
        sender_key,
        "e2e-global-close",
    )
    assert closed.status_code == 204

    retained = _post(
        client,
        "/api/v1/tree",
        {"address": address, "cycle_id": cycle["id"]},
        receiver_key,
    )
    assert retained.status_code == 200
    assert [node["id"] for node in retained.json()["nodes"]] == [root_id, reply_id, nested_id]
    stored_tunnel = Tunnel.objects.get(pk=tunnel["id"])
    stored_cycle = Cycle.objects.get(pk=cycle["id"])
    assert stored_tunnel.state == Tunnel.State.DORMANT
    assert stored_cycle.state == Cycle.State.CLOSED
    assert stored_cycle.final_sequence == 3
    assert Message.objects.filter(cycle=stored_cycle).count() == 3
