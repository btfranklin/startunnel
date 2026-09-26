"""Encode and validate the canonical alchemical-glyph address."""

from __future__ import annotations

import base64
import hashlib
import hmac

from django.conf import settings

GLYPH_START = 0x1F700
GLYPH_END = 0x1F73F
GLYPH_ALPHABET = tuple(chr(codepoint) for codepoint in range(GLYPH_START, GLYPH_END + 1))
ASCII_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
GLYPH_TO_VALUE = {glyph: value for value, glyph in enumerate(GLYPH_ALPHABET)}
TOKEN_SIZE = 16
DATA_GLYPHS = 22
CHECKSUM_GLYPHS = 2
ADDRESS_GLYPHS = DATA_GLYPHS + CHECKSUM_GLYPHS


class InvalidAddress(ValueError):
    """The supplied glyph sequence is not one canonical address."""


def _checksum(token: bytes) -> tuple[int, int]:
    twelve_bits = int.from_bytes(hashlib.blake2s(token, digest_size=2).digest(), "big") >> 4
    return twelve_bits >> 6, twelve_bits & 0x3F


def encode_token(token: bytes) -> str:
    if len(token) != TOKEN_SIZE:
        raise ValueError("A tunnel token must contain 16 bytes.")
    ascii_data = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    data = "".join(GLYPH_ALPHABET[ASCII_ALPHABET.index(character)] for character in ascii_data)
    checksum = "".join(GLYPH_ALPHABET[value] for value in _checksum(token))
    return data + checksum


def parse_address(address: str) -> bytes:
    compact = "".join(character for character in address if not character.isspace())
    if len(compact) != ADDRESS_GLYPHS:
        raise InvalidAddress("The glyph address must contain 24 symbols.")
    try:
        values = [GLYPH_TO_VALUE[character] for character in compact]
    except KeyError as error:
        raise InvalidAddress("The glyph address contains an unknown symbol.") from error
    ascii_data = "".join(ASCII_ALPHABET[value] for value in values[:DATA_GLYPHS])
    try:
        token = base64.b64decode(ascii_data + "==", altchars=b"-_", validate=True)
    except ValueError as error:
        raise InvalidAddress("The glyph address is not canonical.") from error
    if len(token) != TOKEN_SIZE or encode_token(token)[:DATA_GLYPHS] != compact[:DATA_GLYPHS]:
        raise InvalidAddress("The glyph address is not canonical.")
    if not hmac.compare_digest(bytes(values[DATA_GLYPHS:]), bytes(_checksum(token))):
        raise InvalidAddress("The glyph address checksum is not valid.")
    return token


def canonical_address(address: str) -> str:
    return encode_token(parse_address(address))


def display_address(address: str) -> str:
    canonical = canonical_address(address)
    return " ".join(canonical[index : index + 4] for index in range(0, ADDRESS_GLYPHS, 4))


def address_transcription(address: str) -> str:
    canonical = canonical_address(address)
    return " ".join(f"U+{ord(glyph):05X}" for glyph in canonical)


def address_digest(token: bytes) -> bytes:
    return hmac.digest(settings.ADDRESS_SECRET.encode(), token, "sha256")


def derive_token(
    *,
    credential_id: str,
    operation: str,
    idempotency_record_id: str,
    idempotency_key_digest: bytes,
    derivation_nonce: bytes,
) -> bytes:
    if len(derivation_nonce) != 32:
        raise ValueError("An address derivation nonce must contain 32 bytes.")
    material = "\x1f".join(
        [
            "startunnel-address",
            credential_id,
            operation,
            idempotency_record_id,
            idempotency_key_digest.hex(),
            derivation_nonce.hex(),
        ]
    ).encode()
    return hmac.digest(settings.ADDRESS_DERIVATION_SECRET.encode(), material, "sha256")[:TOKEN_SIZE]
