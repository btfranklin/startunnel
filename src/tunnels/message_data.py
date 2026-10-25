"""Shared public message data for responses and context byte accounting."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder

from core.errors import InvalidRequest
from core.json_types import JsonValue, load_json

from .models import Cycle, Message


class TextContentData(TypedDict):
    type: Literal["text"]
    text: str


class JsonContentData(TypedDict):
    type: Literal["json"]
    value: JsonValue


type MessageContentData = TextContentData | JsonContentData


class SenderData(TypedDict):
    id: str
    name: str
    session: str | None


class MentionData(TypedDict):
    id: str
    name: str


class MessageData(TypedDict):
    id: str
    cycle_id: str
    cycle_number: int
    parent_id: str | None
    sender: SenderData
    content: MessageContentData
    mentions: list[MentionData]
    correlation_id: str | None
    sequence: int
    depth: int
    created_at: str | datetime


class CycleData(TypedDict):
    id: UUID
    number: int
    label: str
    state: str
    root_id: UUID
    created_at: datetime
    expires_at: datetime
    message_count: int
    content_bytes: int


def message_content(message: Message) -> MessageContentData:
    if message.payload_type == Message.PayloadType.TEXT:
        assert message.text_payload is not None
        return {"type": "text", "text": message.text_payload}
    return {"type": "json", "value": load_json(message.json_payload or "null")}


def message_payload(message: Message) -> MessageData:
    created_at: object = DjangoJSONEncoder().default(message.created_at)
    # Django encodes datetimes as text; check its generic encoder return at this boundary.
    assert isinstance(created_at, str)
    return {
        "id": str(message.id),
        "cycle_id": str(message.cycle_id),
        "cycle_number": message.cycle.number,
        "parent_id": str(message.parent_id) if message.parent_id else None,
        "sender": {
            "id": str(message.sender_id),
            "name": message.sender_name,
            "session": message.session,
        },
        "content": message_content(message),
        "mentions": [
            {"id": str(mention.credential_id), "name": mention.display_name}
            for mention in sorted(
                message.mentions.all(),
                key=lambda item: (str(item.credential_id), str(item.id)),
            )
        ],
        "correlation_id": message.correlation_id or None,
        "sequence": message.sequence,
        "depth": message.depth,
        "created_at": created_at,
    }


def cycle_payload(cycle: Cycle) -> CycleData:
    if cycle.root_message_id is None:
        raise InvalidRequest("The cycle root is not available.")
    return {
        "id": cycle.id,
        "number": cycle.number,
        "label": cycle.label,
        "state": cycle.state,
        "root_id": cycle.root_message_id,
        "created_at": cycle.created_at,
        "expires_at": cycle.expires_at,
        "message_count": cycle.message_count,
        "content_bytes": cycle.content_bytes,
    }
