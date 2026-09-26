"""Durable request idempotency without stored bearer addresses."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from agents.models import AgentCredential

from .errors import IdempotencyConflict, InvalidRequest
from .models import IdempotencyRecord


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    record: IdempotencyRecord
    replay: bool


def validate_idempotency_key(key: str) -> None:
    if not 8 <= len(key) <= 128 or any(
        ord(character) < 32 or ord(character) > 126 for character in key
    ):
        raise InvalidRequest("Idempotency-Key must contain 8 to 128 printable ASCII characters.")


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


def find_replay(
    *, credential: AgentCredential, operation: str, key: str, request_data: dict[str, Any]
) -> IdempotencyResult | None:
    """Return an exact current replay without creating a record for a new request."""

    validate_idempotency_key(key)
    now = timezone.now()
    key_hash = _key_digest(key)
    request_hash = _request_digest(request_data)
    record = (
        IdempotencyRecord.objects.select_for_update()
        .filter(
            credential=credential,
            operation=operation,
            key_digest=key_hash,
            expires_at__gt=now,
        )
        .first()
    )
    if not record:
        return None
    if not hmac.compare_digest(bytes(record.request_digest), request_hash):
        raise IdempotencyConflict()
    return IdempotencyResult(record=record, replay=True)


@transaction.atomic
def begin(
    *, credential: AgentCredential, operation: str, key: str, request_data: dict[str, Any]
) -> IdempotencyResult:
    existing = find_replay(
        credential=credential,
        operation=operation,
        key=key,
        request_data=request_data,
    )
    if existing:
        return existing
    now = timezone.now()
    key_hash = _key_digest(key)
    request_hash = _request_digest(request_data)
    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                credential=credential,
                operation=operation,
                key_digest=key_hash,
                request_digest=request_hash,
                expires_at=now + timedelta(hours=24),
            )
    except IntegrityError:
        record = IdempotencyRecord.objects.select_for_update().get(
            credential=credential, operation=operation, key_digest=key_hash
        )
        if record.expires_at <= now:
            record.delete()
            record = IdempotencyRecord.objects.create(
                credential=credential,
                operation=operation,
                key_digest=key_hash,
                request_digest=request_hash,
                expires_at=now + timedelta(hours=24),
            )
            return IdempotencyResult(record=record, replay=False)
        if not hmac.compare_digest(bytes(record.request_digest), request_hash):
            raise IdempotencyConflict() from None
        return IdempotencyResult(record=record, replay=True)
    return IdempotencyResult(record=record, replay=False)


def finish(
    record: IdempotencyRecord, *, response: dict[str, Any], resource_id: str | None = None
) -> None:
    record.response = response
    record.resource_id = resource_id
    record.save(update_fields=["response", "resource_id"])
