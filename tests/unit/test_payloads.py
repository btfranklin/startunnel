"""Payload validation is canonical and bounded."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tunnels.errors import InvalidRequest, PayloadTooLarge
from tunnels.payloads import validate_content


def test_text_payload_counts_utf8_bytes() -> None:
    payload = validate_content({"type": "text", "text": "𐀀"})
    assert payload.byte_count == 4


def test_json_accepts_scalar_and_null() -> None:
    assert validate_content({"type": "json", "value": None}).byte_count == 4
    assert validate_content({"type": "json", "value": 2}).byte_count == 1


def test_json_rejects_more_than_32_levels() -> None:
    value: object = "leaf"
    for _ in range(33):
        value = [value]
    with pytest.raises(InvalidRequest):
        validate_content({"type": "json", "value": value})


def test_payload_rejects_more_than_64_kib() -> None:
    with pytest.raises(PayloadTooLarge):
        validate_content({"type": "text", "text": "x" * 65_537})


def test_payload_accepts_exactly_64_kib() -> None:
    payload = validate_content({"type": "text", "text": "x" * 65_536})
    assert payload.byte_count == 65_536


def test_multibyte_payload_limit_uses_encoded_size() -> None:
    assert validate_content({"type": "text", "text": "𐀀" * 16_384}).byte_count == 65_536
    with pytest.raises(PayloadTooLarge):
        validate_content({"type": "text", "text": "𐀀" * 16_385})


@pytest.mark.parametrize("value", ["before\x00after", "\ud800"])
def test_text_payload_rejects_text_postgresql_cannot_store(value: str) -> None:
    with pytest.raises(InvalidRequest):
        validate_content({"type": "text", "text": value})


def test_json_allows_null_characters_as_canonical_escapes() -> None:
    payload = validate_content({"type": "json", "value": {"nested": "before\x00after"}})
    assert payload.json_text == '{"nested":"before\\u0000after"}'


def test_json_rejects_lone_surrogates() -> None:
    with pytest.raises(InvalidRequest):
        validate_content({"type": "json", "value": {"nested": "\ud800"}})


def test_json_canonical_size_has_no_optional_whitespace() -> None:
    payload = validate_content({"type": "json", "value": {"b": 2, "a": 1}})
    assert payload.byte_count == len(b'{"a":1,"b":2}')


def test_json_payload_rejects_more_than_64_kib_after_canonicalization() -> None:
    with pytest.raises(PayloadTooLarge):
        validate_content({"type": "json", "value": "x" * 65_535})


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"type": "binary", "value": "x"},
        {"type": "text", "text": 1},
        {"type": "text", "text": "x", "extra": True},
        {"type": "json"},
        {"type": "json", "value": 1, "extra": True},
    ],
)
def test_payload_shape_is_exact(content: dict[str, object]) -> None:
    with pytest.raises(InvalidRequest):
        validate_content(content)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1, 2}, object()])
def test_json_rejects_values_that_are_not_canonical_json(value: object) -> None:
    with pytest.raises(InvalidRequest):
        validate_content({"type": "json", "value": value})


@given(
    st.text(max_size=512).filter(
        lambda value: (
            "\x00" not in value
            and all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)
        )
    )
)
def test_text_byte_count_matches_utf8(value: str) -> None:
    assert validate_content({"type": "text", "text": value}).byte_count == len(
        value.encode("utf-8")
    )


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(max_size=20),
        lambda values: (
            st.lists(values, max_size=4) | st.dictionaries(st.text(max_size=8), values, max_size=4)
        ),
        max_leaves=20,
    )
)
def test_supported_json_values_have_a_stable_nonnegative_size(value: object) -> None:
    payload = validate_content({"type": "json", "value": value})
    assert 0 <= payload.byte_count <= 65_536
