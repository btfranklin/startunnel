"""Strict signed cursor primitives for bounded traversal state."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from django.core import signing
from django.core.signing import BadSignature

from core.limits import Limits
from tunnels.errors import InvalidCursor

CURSOR_SALT = "startunnel.api.cursor"
MAX_CURSOR_CHARACTERS = 4_096


def encode_cursor(kind: str, issued_at: int | None = None, /, **values: object) -> str:
    issued_at = int(time.time()) if issued_at is None else issued_at
    expires_at = issued_at + Limits().cursor_lifetime_seconds
    return signing.Signer(salt=CURSOR_SALT).sign_object(
        {
            "kind": kind,
            "issued_at": issued_at,
            "expires_at": expires_at,
            **values,
        },
        compress=True,
    )


def decode_cursor(value: str, *, kind: str) -> dict[str, Any]:
    if not value or len(value) > MAX_CURSOR_CHARACTERS:
        raise InvalidCursor()
    try:
        decoded = signing.Signer(salt=CURSOR_SALT).unsign_object(value)
    except BadSignature as error:
        raise InvalidCursor() from error
    if not isinstance(decoded, dict) or decoded.get("kind") != kind:
        raise InvalidCursor()
    issued_at = decoded.get("issued_at")
    expires_at = decoded.get("expires_at")
    now = int(time.time())
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at - issued_at != Limits().cursor_lifetime_seconds
        or issued_at > now + 60
        or expires_at < now
    ):
        raise InvalidCursor()
    return decoded


def cursor_uuid(decoded: dict[str, Any], field: str) -> UUID:
    try:
        return UUID(str(decoded[field]))
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidCursor() from error


def cursor_int(decoded: dict[str, Any], field: str) -> int:
    value = decoded.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidCursor()
    return value
