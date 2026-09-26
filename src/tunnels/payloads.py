"""Validate and serialize text and JSON message content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import InvalidRequest, PayloadTooLarge

MAX_DEPTH = 32
MAX_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class ValidatedPayload:
    payload_type: str
    text: str | None
    json_value: Any
    json_text: str | None
    byte_count: int


def validate_storable_text(value: str, *, field: str) -> None:
    """Reject text that PostgreSQL text columns cannot store."""

    if "\x00" in value:
        raise InvalidRequest(f"{field} cannot contain a null character.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidRequest(f"{field} must contain valid Unicode.") from error


def _validate_json_unicode(value: Any) -> None:
    """Reject lone surrogates while keeping JSON control characters valid."""

    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidRequest("JSON text must contain valid Unicode.") from error
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_unicode(key)
            _validate_json_unicode(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_unicode(item)


def _depth(value: Any, current: int = 0) -> int:
    if current > MAX_DEPTH:
        return current
    if isinstance(value, dict):
        if not value:
            return current + 1
        return max(_depth(item, current + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return max([_depth(item, current + 1) for item in value], default=current + 1)
    return current


def validate_content(
    content: dict[str, Any], *, maximum_bytes: int = MAX_BYTES
) -> ValidatedPayload:
    payload_type = content.get("type")
    if payload_type == "text":
        if set(content) != {"type", "text"} or not isinstance(content.get("text"), str):
            raise InvalidRequest("Text content must contain only type and text fields.")
        text = content["text"]
        validate_storable_text(text, field="Message text")
        byte_count = len(text.encode("utf-8"))
        if byte_count > maximum_bytes:
            raise PayloadTooLarge()
        return ValidatedPayload("text", text, None, None, byte_count)
    if payload_type == "json":
        if set(content) != {"type", "value"}:
            raise InvalidRequest("JSON content must contain only type and value fields.")
        value = content.get("value")
        if _depth(value) > MAX_DEPTH:
            raise InvalidRequest("JSON content can contain at most 32 nesting levels.")
        _validate_json_unicode(value)
        try:
            canonical_text = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            canonical = canonical_text.encode("utf-8")
        except (TypeError, UnicodeEncodeError, ValueError) as error:
            raise InvalidRequest("JSON content contains an unsupported value.") from error
        if len(canonical) > maximum_bytes:
            raise PayloadTooLarge()
        return ValidatedPayload("json", None, value, canonical_text, len(canonical))
    raise InvalidRequest("Content type must be text or json.")
