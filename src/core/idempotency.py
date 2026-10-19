"""Durable request idempotency with exact request fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from agents.models import AgentCredential
from core.errors import IdempotencyConflict, InvalidRequest
from core.limits import MAX_IDEMPOTENCY_KEY_CHARACTERS, MIN_IDEMPOTENCY_KEY_CHARACTERS
from core.models import AgentReceipt


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    record: AgentReceipt
    replay: bool


def validate_idempotency_key(key: str) -> None:
    if not MIN_IDEMPOTENCY_KEY_CHARACTERS <= len(key) <= MAX_IDEMPOTENCY_KEY_CHARACTERS or any(
        ord(character) < 32 or ord(character) > 126 for character in key
    ):
        raise InvalidRequest(
            f"Idempotency-Key must contain {MIN_IDEMPOTENCY_KEY_CHARACTERS} to "
            f"{MAX_IDEMPOTENCY_KEY_CHARACTERS} printable ASCII characters."
        )


def _key_digest(key: str) -> bytes:
    return hmac.digest(settings.IDEMPOTENCY_SECRET.encode(), key.encode(), "sha256")


def _request_digest(request_data: dict[str, Any]) -> bytes:
    try:
        canonical = json.dumps(
            request_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InvalidRequest("The request contains a value that cannot be serialized.") from error
    return hashlib.sha256(canonical).digest()


def begin(
    *, credential: AgentCredential, operation: str, key: str, request_data: dict[str, Any]
) -> IdempotencyResult:
    """Reserve or replay a receipt inside the caller's write transaction."""

    validate_idempotency_key(key)
    now = timezone.now()
    key_hash = _key_digest(key)
    request_hash = _request_digest(request_data)
    defaults = {
        "request_digest": request_hash,
        "expires_at": now + timedelta(hours=24),
    }
    record, created = AgentReceipt.objects.select_for_update().get_or_create(
        credential=credential, operation=operation, key_digest=key_hash, defaults=defaults
    )
    if not created and record.expires_at > now:
        if not hmac.compare_digest(bytes(record.request_digest), request_hash):
            raise IdempotencyConflict()
        return IdempotencyResult(record=record, replay=True)
    if not created:
        record.delete()
        record = AgentReceipt.objects.create(
            credential=credential, operation=operation, key_digest=key_hash, **defaults
        )
    return IdempotencyResult(record=record, replay=False)


def finish(
    record: AgentReceipt, *, response: dict[str, Any], resource_id: str | None = None
) -> None:
    record.response = response
    record.resource_id = resource_id
    record.save(update_fields=["response", "resource_id"])
