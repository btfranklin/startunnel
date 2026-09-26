"""The glyph address codec is exact and lossless."""

import json
import unicodedata

import pytest
from django.test import override_settings
from hypothesis import given
from hypothesis import strategies as st

from tunnels.codec import (
    ADDRESS_GLYPHS,
    GLYPH_ALPHABET,
    InvalidAddress,
    address_digest,
    address_transcription,
    canonical_address,
    derive_token,
    display_address,
    encode_token,
    parse_address,
)


def test_alphabet_is_exact_alchemical_unicode_range() -> None:
    assert len(GLYPH_ALPHABET) == 64
    assert ord(GLYPH_ALPHABET[0]) == 0x1F700
    assert ord(GLYPH_ALPHABET[-1]) == 0x1F73F
    assert max(ord(glyph) for glyph in GLYPH_ALPHABET) <= ord("🝳")
    assert [ord(glyph) for glyph in GLYPH_ALPHABET] == list(range(0x1F700, 0x1F740))


@given(st.binary(min_size=16, max_size=16))
def test_round_trip_for_each_token(token: bytes) -> None:
    address = encode_token(token)
    assert len(address) == ADDRESS_GLYPHS
    assert parse_address(address) == token
    assert canonical_address(display_address(address)) == address


def test_unicode_whitespace_is_accepted() -> None:
    address = encode_token(bytes(range(16)))
    spaced = f"{address[:4]}\u2003{address[4:8]}\n{address[8:]}"
    assert parse_address(spaced) == bytes(range(16))


@given(
    st.binary(min_size=16, max_size=16),
    st.sampled_from([" ", "\t", "\n", "\r", "\u00a0", "\u2003", "\u2028", "\u3000"]),
    st.integers(min_value=0, max_value=24),
)
def test_unicode_whitespace_is_accepted_at_each_position(
    token: bytes, whitespace: str, position: int
) -> None:
    address = encode_token(token)
    candidate = address[:position] + whitespace + address[position:]
    assert parse_address(candidate) == token


def test_display_and_transcription_are_accessible_and_canonical() -> None:
    address = encode_token(bytes(range(16)))
    groups = display_address(address).split(" ")
    transcription = address_transcription(address).split(" ")
    assert len(groups) == 6
    assert all(len(group) == 4 for group in groups)
    assert transcription == [f"U+{ord(glyph):05X}" for glyph in address]


def test_address_survives_json_and_utf8_transport() -> None:
    address = encode_token(bytes(reversed(range(16))))
    transported = json.loads(json.dumps({"address": address}, ensure_ascii=False))["address"]
    assert transported.encode("utf-8").decode("utf-8") == address
    assert parse_address(transported) == bytes(reversed(range(16)))


def test_lossy_unicode_normalization_is_not_applied() -> None:
    address = encode_token(bytes(range(16)))
    normalized = unicodedata.normalize("NFKC", address)
    if normalized != address:
        with pytest.raises(InvalidAddress):
            parse_address(normalized)
    assert parse_address(address) == bytes(range(16))


@pytest.mark.parametrize("glyph", GLYPH_ALPHABET)
def test_every_alchemical_symbol_can_pass_through_the_codec(glyph: str) -> None:
    index = GLYPH_ALPHABET.index(glyph)
    token = bytes([index]) * 16
    address = encode_token(token)
    assert glyph in GLYPH_ALPHABET
    assert parse_address(address) == token


@pytest.mark.parametrize("suffix", ["!", "\ufe0f", "A", "-", "\u200d", "\U0001f740"])
def test_non_glyph_content_is_rejected(suffix: str) -> None:
    address = encode_token(bytes(range(16)))
    with pytest.raises(InvalidAddress):
        parse_address(address[:-1] + suffix)


def test_bad_checksum_is_rejected() -> None:
    address = encode_token(bytes(range(16)))
    replacement = GLYPH_ALPHABET[(GLYPH_ALPHABET.index(address[-1]) + 1) % 64]
    with pytest.raises(InvalidAddress):
        parse_address(address[:-1] + replacement)


def test_noncanonical_base64_padding_bits_are_rejected() -> None:
    address = encode_token(bytes(range(16)))
    final_data_index = GLYPH_ALPHABET.index(address[21])
    assert final_data_index % 16 == 0
    replacement = GLYPH_ALPHABET[final_data_index + 1]
    with pytest.raises(InvalidAddress, match="canonical"):
        parse_address(address[:21] + replacement + address[22:])


def test_decoder_failure_is_reported_as_an_invalid_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = encode_token(bytes(range(16)))

    def fail_decode(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise ValueError("test decoder failure")

    monkeypatch.setattr("tunnels.codec.base64.b64decode", fail_decode)
    with pytest.raises(InvalidAddress, match="canonical"):
        parse_address(address)


@pytest.mark.parametrize("extra", [GLYPH_ALPHABET[0], "!", "\ufe0f"])
def test_extra_non_whitespace_data_is_rejected(extra: str) -> None:
    address = encode_token(bytes(range(16)))
    with pytest.raises(InvalidAddress):
        parse_address(address + extra)


def test_token_must_have_exactly_128_bits() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        encode_token(b"too short")


def test_derivation_and_digest_use_separate_secrets() -> None:
    fixed_token = bytes(range(16))

    def derive() -> bytes:
        return derive_token(
            credential_id="00000000-0000-0000-0000-000000000001",
            operation="create_tunnel",
            idempotency_record_id="00000000-0000-0000-0000-000000000002",
            idempotency_key_digest=bytes(range(32)),
            derivation_nonce=bytes(range(32, 64)),
        )

    with override_settings(
        ADDRESS_DERIVATION_SECRET="derivation-secret-a",
        ADDRESS_SECRET="digest-secret-a",
    ):
        derived_a = derive()
        digest_a = address_digest(fixed_token)
    with override_settings(
        ADDRESS_DERIVATION_SECRET="derivation-secret-b",
        ADDRESS_SECRET="digest-secret-a",
    ):
        assert derive() != derived_a
        assert address_digest(fixed_token) == digest_a
    with override_settings(
        ADDRESS_DERIVATION_SECRET="derivation-secret-a",
        ADDRESS_SECRET="digest-secret-b",
    ):
        assert derive() == derived_a
        assert address_digest(fixed_token) != digest_a


@pytest.mark.parametrize("nonce", [b"", b"short", bytes(31), bytes(33)])
def test_derivation_rejects_a_nonce_that_is_not_32_bytes(nonce: bytes) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        derive_token(
            credential_id="00000000-0000-0000-0000-000000000001",
            operation="create_tunnel",
            idempotency_record_id="00000000-0000-0000-0000-000000000002",
            idempotency_key_digest=bytes(32),
            derivation_nonce=nonce,
        )
