"""The API stores session labels and returns them with message attribution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest
from django.test import Client
from tests.factories import CredentialFactory

from tunnels.models import Message
from tunnels.search import SearchHit, SearchPage

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("api.auth.consume_api_operation", lambda credential, limits: None)


def _post(
    client: Client, path: str, body: Mapping[str, object], key: str, retry_key: str | None = None
) -> _MonkeyPatchedWSGIResponse:
    headers = {"Authorization": f"Bearer {key}"}
    if retry_key:
        headers["Idempotency-Key"] = retry_key
    return client.post(
        path, data=json.dumps(body), content_type="application/json", headers=headers
    )


@pytest.mark.django_db(transaction=True)
def test_labeled_root_and_reply_appear_in_public_reads(
    client: Client, credential_factory: CredentialFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, key = credential_factory(name="Shared agent")
    creation = {
        "cycle": {"root": {"content": {"type": "text", "text": "Root"}, "session": "chat-a"}}
    }
    created = _post(client, "/api/v1/tunnels", creation, key, "api-session-create")
    assert created.status_code == 201, created.content
    result = created.json()
    address = result["tunnel"]["address"]
    root_id = result["cycle"]["root"]["id"]
    reply_body = {
        "address": address,
        "parent_id": root_id,
        "content": {"type": "text", "text": "Reply"},
        "session": "chat-b",
    }
    reply = _post(client, "/api/v1/messages", reply_body, key, "api-session-reply")
    assert reply.status_code == 201, reply.content
    reply_id = reply.json()["message"]["id"]
    assert _post(client, "/api/v1/messages", reply_body, key, "api-session-reply").json()[
        "idempotent_replay"
    ]
    changed = {**reply_body, "session": "chat-c"}
    assert _post(client, "/api/v1/messages", changed, key, "api-session-reply").status_code == 409

    tree = _post(client, "/api/v1/tree", {"address": address}, key).json()["nodes"]
    assert [(item["sender"]["name"], item["sender"]["session"]) for item in tree] == [
        ("Shared agent", "chat-a"),
        ("Shared agent", "chat-b"),
    ]
    exact = _post(
        client, "/api/v1/tree/message", {"address": address, "message_id": reply_id}, key
    ).json()
    assert exact["message"]["sender"]["session"] == "chat-b"
    entry = _post(client, "/api/v1/enter", {"address": address}, key).json()
    assert entry["root"]["sender"]["session"] == "chat-a"
    assert entry["recent_messages"][0]["sender"]["session"] == "chat-b"
    context = _post(
        client, "/api/v1/context", {"address": address, "focus_message_id": reply_id}, key
    ).json()
    assert {item["message"]["sender"]["session"] for item in context["items"]} == {
        "chat-a",
        "chat-b",
    }
    activity = _post(client, "/api/v1/activity", {"address": address}, key).json()
    assert {
        event["message"]["sender"]["session"] for event in activity["events"] if event["message"]
    } == {"chat-a", "chat-b"}
    directory = _post(client, "/api/v1/cycles", {"address": address}, key).json()
    assert directory["items"][0]["root"]["sender"]["session"] == "chat-a"
    root = Message.objects.get(pk=root_id)
    monkeypatch.setattr(
        "api.router.search_messages",
        lambda **arguments: SearchPage(
            tunnel=root.tunnel,
            cycle=root.cycle,
            hits=(SearchHit(message=root, snippet="Root", rank=1),),
            snapshot_sequence=2,
            next_after_rank=None,
            next_after_sequence=None,
            next_after_message_id=None,
            complete=True,
            used_bytes=0,
        ),
    )
    search = _post(client, "/api/v1/search", {"address": address, "query": "Root"}, key)
    assert search.status_code == 200, search.content
    assert search.json()["results"][0]["sender"]["session"] == "chat-a"


@pytest.mark.parametrize("label", ["", " outer", "inner ", "a\u0085", "a\u202e", "a" * 33])
def test_api_rejects_invalid_session_labels(
    client: Client, credential_factory: CredentialFactory, label: str
) -> None:
    _, key = credential_factory()
    body = {"cycle": {"root": {"content": {"type": "text", "text": "Root"}, "session": label}}}
    response = _post(client, "/api/v1/tunnels", body, key, "api-session-invalid")
    assert response.status_code in {400, 422}
