"""Decode / encode MDT export strings.

MDT has shipped three wire formats over the years; all are supported here.

1. Modern (2025+): ``!~MDT2~`` + Base64( Deflate( CBOR(preset) ) )
   (in-game: C_EncodingUtil SerializeCBOR / CompressString / EncodeBase64)
2. Legacy "!" exports: ``!`` + LibDeflate:EncodeForPrint( Deflate(
   AceSerializer(preset) ) )
3. Ancient exports (no prefix): LibCompress + AceSerializer. Only the
   uncompressed LibCompress method is supported; the Huffman/LZW methods
   raise a clear error asking for a re-export from current MDT.
"""

from __future__ import annotations

import base64
import binascii
import zlib
from typing import Any

from . import ace_serializer, cbor
from .print_codec import decode_for_print, encode_for_print

MDT2_PREFIX = "!~MDT2~"


class MDTDecodeError(ValueError):
    pass


def _inflate(data: bytes) -> bytes:
    """Decompress raw-deflate data (LibDeflate / C_EncodingUtil style),
    falling back to zlib/gzip wrappers just in case."""
    for wbits in (-15, 15, 31):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    raise MDTDecodeError("could not decompress route data (not a deflate stream)")


def _deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def decode_mdt_string(text: str) -> Any:
    """Decode any MDT export string into a plain Python structure."""
    text = text.strip()
    if not text:
        raise MDTDecodeError("empty import string")

    if text.startswith(MDT2_PREFIX):
        payload = text[len(MDT2_PREFIX):]
        # Base64 with tolerant padding
        payload = payload.strip()
        try:
            decoded = base64.b64decode(payload + "=" * (-len(payload) % 4))
        except (binascii.Error, ValueError) as exc:
            raise MDTDecodeError(f"invalid base64 in MDT2 string: {exc}") from None
        decompressed = _inflate(decoded)
        try:
            return cbor.loads(decompressed)
        except cbor.CBORError as exc:
            raise MDTDecodeError(f"invalid CBOR in MDT2 string: {exc}") from None

    if text.startswith("!"):
        decoded = decode_for_print(text[1:])
        decompressed = _inflate(decoded)
        values = _load_ace(decompressed)
        return values

    # Ancient LibCompress format: first byte is the compression method.
    decoded = decode_for_print(text)
    if decoded[:1] == b"\x01":  # stored / uncompressed
        return _load_ace(decoded[1:])
    raise MDTDecodeError(
        "this looks like a very old MDT export (LibCompress-compressed); "
        "please re-export the route from a current MDT version"
    )


def _load_ace(data: bytes) -> Any:
    try:
        values = ace_serializer.loads(data.decode("utf-8", errors="surrogateescape"))
    except ace_serializer.AceSerializerError as exc:
        raise MDTDecodeError(f"invalid AceSerializer data: {exc}") from None
    if not values:
        raise MDTDecodeError("decoded stream contained no values")
    return values[0]


def encode_mdt_string(preset: Any, style: str = "mdt2") -> str:
    """Encode a preset structure as an MDT-importable string.

    ``style`` is "mdt2" (current format) or "legacy" ("!"-prefixed
    AceSerializer format). Mostly useful for tests and for re-sharing
    modified routes.
    """
    if style == "mdt2":
        serialized = cbor.dumps(preset)
        return MDT2_PREFIX + base64.b64encode(_deflate(serialized)).decode("ascii")
    if style == "legacy":
        serialized = ace_serializer.dumps(preset).encode("utf-8", errors="surrogateescape")
        return "!" + encode_for_print(_deflate(serialized))
    raise ValueError(f"unknown style {style!r}")
