"""Signed cursor primitives reject altered or ill-typed public state."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.cursors import cursor_int, cursor_uuid, decode_cursor, encode_cursor
from core.errors import InvalidCursor


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


def test_cursor_replays_the_same_traversal_state() -> None:
    first = encode_cursor("activity", position=2)
    assert encode_cursor("activity", position=2) == first
