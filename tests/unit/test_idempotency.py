"""Idempotency keys bind one request to one operation for 24 hours."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import time_machine
from django.utils import timezone

from tunnels.errors import IdempotencyConflict, InvalidRequest
from tunnels.idempotency import begin, finish, validate_idempotency_key
from tunnels.models import IdempotencyRecord

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("key", ["short", "x" * 129, "line\nbreak", "tab\tkey"])
def test_invalid_idempotency_key_is_rejected(key: str) -> None:
    with pytest.raises(InvalidRequest, match="printable ASCII"):
        validate_idempotency_key(key)


@pytest.mark.parametrize("key", ["12345678", "x" * 128, "safe request key 42"])
def test_valid_idempotency_key_is_accepted(key: str) -> None:
    validate_idempotency_key(key)


def test_replay_and_conflict_are_operation_scoped(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    first = begin(
        credential=credential,
        operation="send:one",
        key="request-0001",
        request_data={"value": 1},
    )
    finish(first.record, response={"id": "safe"})
    replay = begin(
        credential=credential,
        operation="send:one",
        key="request-0001",
        request_data={"value": 1},
    )
    assert replay.replay
    assert replay.record.response == {"id": "safe"}
    with pytest.raises(IdempotencyConflict):
        begin(
            credential=credential,
            operation="send:one",
            key="request-0001",
            request_data={"value": 2},
        )
    other_operation = begin(
        credential=credential,
        operation="send:two",
        key="request-0001",
        request_data={"value": 2},
    )
    assert not other_operation.replay


@pytest.mark.parametrize("expired_seconds", [0, 1])
def test_expired_record_can_be_reused(credential_factory: Any, expired_seconds: int) -> None:
    credential, _ = credential_factory()
    first = begin(
        credential=credential,
        operation="create",
        key="request-0002",
        request_data={"label": "old"},
    )
    finish(first.record, response={"old": True}, resource_id=str(uuid4()))
    now = timezone.now()
    first.record.expires_at = now - timedelta(seconds=expired_seconds)
    first.record.save(update_fields=["expires_at"])
    with time_machine.travel(now, tick=False):
        replacement = begin(
            credential=credential,
            operation="create",
            key="request-0002",
            request_data={"label": "new"},
        )
    assert not replacement.replay
    assert replacement.record.id != first.record.id
    assert bytes(replacement.record.key_digest) == bytes(first.record.key_digest)
    assert bytes(replacement.record.request_digest) != bytes(first.record.request_digest)
    assert replacement.record.expires_at == now + timedelta(hours=24)
    assert replacement.record.response == {}
    assert replacement.record.resource_id is None
    assert IdempotencyRecord.objects.count() == 1


def test_request_object_key_order_does_not_change_replay(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    first = begin(
        credential=credential,
        operation="canonical-json",
        key="request-canonical-json",
        request_data={"first": 1, "nested": {"a": 2, "b": 3}},
    )
    replay = begin(
        credential=credential,
        operation="canonical-json",
        key="request-canonical-json",
        request_data={"nested": {"b": 3, "a": 2}, "first": 1},
    )
    assert replay.replay
    assert replay.record.id == first.record.id
