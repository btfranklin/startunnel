"""Narrow decoded JSON values before test code uses them."""

from core.json_types import JsonValue, json_value


def json_array(value: object) -> list[JsonValue]:
    result = json_value(value)
    assert isinstance(result, list)
    return result


def json_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def json_integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def json_at(value: object, *keys: str | int) -> JsonValue:
    current = json_value(value)
    for key in keys:
        if isinstance(key, str):
            assert isinstance(current, dict)
            current = current[key]
        else:
            assert isinstance(current, list)
            current = current[key]
    return current


def json_members(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def json_elements(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value
