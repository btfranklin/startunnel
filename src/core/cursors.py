"""Strict signed cursor primitives for bounded traversal state."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from django.core import signing
from django.core.signing import BadSignature

from core.errors import InvalidCursor
from core.json_types import JsonObject, json_object

CURSOR_SALT = "startunnel.api.cursor"
MAX_CURSOR_CHARACTERS = 4_096


def encode_cursor(kind: str, **values: object) -> str:
    return signing.Signer(salt=CURSOR_SALT).sign_object({"kind": kind, **values}, compress=True)


def decode_cursor(value: str, *, kind: str) -> JsonObject:
    if not value or len(value) > MAX_CURSOR_CHARACTERS:
        raise InvalidCursor()
    try:
        decoded = signing.Signer(salt=CURSOR_SALT).unsign_object(value)
    except BadSignature as error:
        raise InvalidCursor() from error
    if not isinstance(decoded, dict) or decoded.get("kind") != kind:
        raise InvalidCursor()
    return json_object(decoded)


def cursor_uuid(decoded: Mapping[str, object], field: str) -> UUID:
    try:
        return UUID(str(decoded[field]))
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidCursor() from error


def cursor_int(decoded: Mapping[str, object], field: str) -> int:
    value = decoded.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidCursor()
    return value
