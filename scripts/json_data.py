"""Check JSON values at tool input boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Tool scripts run with Python 3.12 or later.
type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


def json_value(value: object) -> JsonValue:
    """Return JSON members with checked keys and value types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        elements: Sequence[object] = value
        return [json_value(element) for element in elements]
    if isinstance(value, dict):
        members: Mapping[object, object] = value
        result: JsonObject = {}
        for key, member in members.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings.")
            result[key] = json_value(member)
        return result
    raise ValueError("Expected a JSON value.")


def json_object(value: object) -> JsonObject:
    checked = json_value(value)
    if not isinstance(checked, dict):
        raise ValueError("Expected a JSON object.")
    return checked


def json_array(value: object) -> list[JsonValue]:
    checked = json_value(value)
    if not isinstance(checked, list):
        raise ValueError("Expected a JSON array.")
    return checked


def json_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a JSON string.")
    return value


def json_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Expected a JSON integer.")
    return value


def json_number(value: object) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Expected a JSON number.")
    return value


def json_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Expected a JSON boolean.")
    return value


def object_array(value: object) -> list[JsonObject]:
    return [json_object(member) for member in json_array(value)]
