"""Idempotency keys bind one request to one operation for 24 hours."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from tunnels.errors import IdempotencyConflict, InvalidRequest
from tunnels.idempotency import begin, finish, validate_idempotency_key

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


def test_expired_record_can_be_reused(credential_factory: Any) -> None:
    credential, _ = credential_factory()
    first = begin(
        credential=credential,
        operation="create",
        key="request-0002",
        request_data={"label": "old"},
    )
    first.record.expires_at = timezone.now() - timedelta(seconds=1)
    first.record.save(update_fields=["expires_at"])
    replacement = begin(
        credential=credential,
        operation="create",
        key="request-0002",
        request_data={"label": "new"},
    )
    assert not replacement.replay
    assert replacement.record.id != first.record.id
