"""Shared public message data for responses and context byte accounting."""

from __future__ import annotations

import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder

from .models import Message


def message_content(message: Message) -> dict[str, Any]:
    if message.payload_type == Message.PayloadType.TEXT:
        return {"type": "text", "text": message.text_payload}
    return {"type": "json", "value": json.loads(message.json_payload or "null")}


def message_payload(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "cycle_id": str(message.cycle_id),
        "cycle_number": message.cycle.number,
        "parent_id": str(message.parent_id) if message.parent_id else None,
        "sender": {"id": str(message.sender_id), "name": message.sender_name},
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
        "created_at": DjangoJSONEncoder().default(message.created_at),
    }
