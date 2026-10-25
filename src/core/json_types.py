"""Bound JSON values at decoder and storage boundaries."""

from __future__ import annotations

import json

from pydantic import JsonValue as JsonValue

type JsonObject = dict[str, JsonValue]


def json_value(value: object) -> JsonValue:
    """Check a decoded value before typed application code uses it."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings.")
            result[key] = json_value(item)
        return result
    raise ValueError("The value is not JSON data.")


def json_object(value: object) -> JsonObject:
    """Require a JSON object with string keys and JSON values."""

    result = json_value(value)
    if not isinstance(result, dict):
        raise ValueError("A JSON object is required.")
    return result


def load_json(text: str | bytes | bytearray) -> JsonValue:
    """Decode JSON without returning an unchecked value."""

    value: object = json.loads(text)
    return json_value(value)
