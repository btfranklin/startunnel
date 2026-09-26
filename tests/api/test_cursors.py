"""Signed cursor primitives reject altered or ill-typed public state."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.core import signing

from api.cursors import CURSOR_SALT, cursor_int, cursor_uuid, decode_cursor, encode_cursor
from core.limits import Limits
from tunnels.errors import InvalidCursor


def test_cursor_round_trip_keeps_kind_and_typed_values() -> None:
    tunnel_id = uuid4()
    encoded = encode_cursor("activity", tunnel_id=str(tunnel_id), position=42)

    decoded = decode_cursor(encoded, kind="activity")

    assert cursor_uuid(decoded, "tunnel_id") == tunnel_id
    assert cursor_int(decoded, "position") == 42


def test_cursor_rejects_tampering_wrong_purpose_and_oversize() -> None:
    encoded = encode_cursor("activity", tunnel_id=str(uuid4()), position=2)
    changed_last = "A" if encoded[-1] != "A" else "B"

    with pytest.raises(InvalidCursor):
        decode_cursor(encoded[:-1] + changed_last, kind="activity")
    with pytest.raises(InvalidCursor):
        decode_cursor(encoded, kind="tree-snapshot")
    with pytest.raises(InvalidCursor):
        decode_cursor("x" * 4_097, kind="activity")


@pytest.mark.parametrize("value", [True, -1, "1", None])
def test_cursor_int_rejects_boolean_negative_string_and_missing(value: object) -> None:
    decoded = {} if value is None else {"position": value}
    with pytest.raises(InvalidCursor):
        cursor_int(decoded, "position")


@pytest.mark.parametrize("value", [None, "not-a-uuid", 7])
def test_cursor_uuid_rejects_missing_and_invalid_values(value: object) -> None:
    decoded = {} if value is None else {"tunnel_id": value}
    with pytest.raises(InvalidCursor):
        cursor_uuid(decoded, "tunnel_id")


def test_cursor_has_one_fixed_lifetime_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("api.cursors.time.time", lambda: 1_000)
    encoded = encode_cursor("activity", tunnel_id=str(uuid4()), position=2)
    decoded = decode_cursor(encoded, kind="activity")
    assert decoded["issued_at"] == 1_000
    assert decoded["expires_at"] == 1_000 + Limits().cursor_lifetime_seconds

    monkeypatch.setattr(
        "api.cursors.time.time",
        lambda: 1_000 + Limits().cursor_lifetime_seconds + 1,
    )
    with pytest.raises(InvalidCursor):
        decode_cursor(encoded, kind="activity")


def test_cursor_can_reproduce_one_operation_time_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("api.cursors.time.time", lambda: 1_100)
    first = encode_cursor("activity", 1_000, position=2)
    monkeypatch.setattr("api.cursors.time.time", lambda: 1_200)
    replay = encode_cursor("activity", 1_000, position=2)

    assert replay == first
    decoded = decode_cursor(replay, kind="activity")
    assert decoded["issued_at"] == 1_000
    assert decoded["expires_at"] == 1_000 + Limits().cursor_lifetime_seconds


@pytest.mark.parametrize(
    ("issued_at", "expires_at"),
    [(True, 4_600), (1_000, False), (1_000, 4_601), (2_000, 5_600)],
)
def test_cursor_rejects_invalid_or_future_policy_times(
    monkeypatch: pytest.MonkeyPatch, issued_at: object, expires_at: object
) -> None:
    monkeypatch.setattr("api.cursors.time.time", lambda: 1_000)
    encoded = signing.Signer(salt=CURSOR_SALT).sign_object(
        {
            "kind": "activity",
            "issued_at": issued_at,
            "expires_at": expires_at,
            "position": 1,
        },
        compress=True,
    )

    with pytest.raises(InvalidCursor):
        decode_cursor(encoded, kind="activity")
