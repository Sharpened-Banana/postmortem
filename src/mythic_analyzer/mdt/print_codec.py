"""LibDeflate's printable encoding (EncodeForPrint / DecodeForPrint).

MDT's legacy export strings ("!"-prefixed) wrap deflate-compressed data in
this 6-bit-per-character encoding. Three bytes become four characters,
packed little-endian; a 1- or 2-byte tail becomes 2 or 3 characters.

Alphabet (index 0..63): a-z A-Z 0-9 ( )
"""

from __future__ import annotations

_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789()"
)
_CHAR_TO_VAL = {c: i for i, c in enumerate(_ALPHABET)}


def encode_for_print(data: bytes) -> str:
    out: list[str] = []
    n = len(data)
    i = 0
    while i + 3 <= n:
        cache = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
        i += 3
        out.append(_ALPHABET[cache & 63])
        out.append(_ALPHABET[(cache >> 6) & 63])
        out.append(_ALPHABET[(cache >> 12) & 63])
        out.append(_ALPHABET[(cache >> 18) & 63])
    cache = 0
    bitlen = 0
    while i < n:
        cache |= data[i] << bitlen
        bitlen += 8
        i += 1
    while bitlen > 0:
        out.append(_ALPHABET[cache & 63])
        cache >>= 6
        bitlen -= 6
    return "".join(out)


def decode_for_print(text: str) -> bytes:
    text = text.strip()
    out = bytearray()
    n = len(text)
    i = 0
    while i + 4 <= n:
        try:
            cache = (
                _CHAR_TO_VAL[text[i]]
                | (_CHAR_TO_VAL[text[i + 1]] << 6)
                | (_CHAR_TO_VAL[text[i + 2]] << 12)
                | (_CHAR_TO_VAL[text[i + 3]] << 18)
            )
        except KeyError as exc:
            raise ValueError(f"invalid character in printable encoding: {exc}") from None
        i += 4
        out.append(cache & 255)
        out.append((cache >> 8) & 255)
        out.append((cache >> 16) & 255)
    cache = 0
    bitlen = 0
    while i < n:
        try:
            cache |= _CHAR_TO_VAL[text[i]] << bitlen
        except KeyError as exc:
            raise ValueError(f"invalid character in printable encoding: {exc}") from None
        bitlen += 6
        i += 1
    while bitlen >= 8:
        out.append(cache & 255)
        cache >>= 8
        bitlen -= 8
    return bytes(out)
