"""Minimal CBOR (RFC 8949) codec.

Covers everything Blizzard's C_EncodingUtil.SerializeCBOR emits for Lua
tables: unsigned/negative integers, byte and text strings, arrays, maps,
floats (16/32/64-bit), booleans and null, plus indefinite-length items and
tags (tags are decoded transparently to their content).

Pure stdlib; no external dependency.
"""

from __future__ import annotations

import math
import struct
from typing import Any


class CBORError(ValueError):
    pass


_BREAK = object()


def loads(data: bytes) -> Any:
    value, _ = _decode_item(data, 0)
    if value is _BREAK:
        raise CBORError("unexpected 'break' code at top level")
    return value


def _decode_head(data: bytes, offset: int) -> tuple[int, int, int | None, int]:
    """Return (major_type, info, argument, new_offset). argument None = indefinite."""
    if offset >= len(data):
        raise CBORError("truncated CBOR data")
    initial = data[offset]
    offset += 1
    major = initial >> 5
    info = initial & 0x1F
    if info < 24:
        return major, info, info, offset
    if info == 24:
        fmt, size = ">B", 1
    elif info == 25:
        fmt, size = ">H", 2
    elif info == 26:
        fmt, size = ">I", 4
    elif info == 27:
        fmt, size = ">Q", 8
    elif info == 31:
        return major, info, None, offset
    else:
        raise CBORError(f"reserved additional info {info}")
    if offset + size > len(data):
        raise CBORError("truncated CBOR data")
    (arg,) = struct.unpack_from(fmt, data, offset)
    return major, info, arg, offset + size


def _decode_item(data: bytes, offset: int) -> tuple[Any, int]:
    major, info, arg, offset = _decode_head(data, offset)

    if major == 0:  # unsigned int
        return arg, offset
    if major == 1:  # negative int
        return -1 - arg, offset
    if major in (2, 3):  # byte / text string
        return _decode_string(data, offset, arg, major)
    if major == 4:  # array
        items: list[Any] = []
        if arg is None:
            while True:
                value, offset = _decode_item(data, offset)
                if value is _BREAK:
                    return items, offset
                items.append(value)
        for _ in range(arg):
            value, offset = _decode_item(data, offset)
            items.append(value)
        return items, offset
    if major == 5:  # map
        table: dict[Any, Any] = {}
        if arg is None:
            while True:
                key, offset = _decode_item(data, offset)
                if key is _BREAK:
                    return table, offset
                value, offset = _decode_item(data, offset)
                table[_hashable(key)] = value
        for _ in range(arg):
            key, offset = _decode_item(data, offset)
            value, offset = _decode_item(data, offset)
            table[_hashable(key)] = value
        return table, offset
    if major == 6:  # tag: decode content transparently
        return _decode_item(data, offset)

    # major == 7: floats, simple values, break
    if info == 31:
        return _BREAK, offset
    if info == 25:
        return _decode_half(arg), offset
    if info == 26:
        (f,) = struct.unpack(">f", struct.pack(">I", arg))
        return f, offset
    if info == 27:
        (d,) = struct.unpack(">d", struct.pack(">Q", arg))
        return d, offset
    if arg == 20:
        return False, offset
    if arg == 21:
        return True, offset
    if arg in (22, 23):  # null / undefined
        return None, offset
    return arg, offset  # unassigned simple value


def _decode_string(data: bytes, offset: int, arg: int | None, major: int) -> tuple[Any, int]:
    if arg is None:  # indefinite: concatenation of definite chunks
        chunks: list[bytes] = []
        while True:
            if offset < len(data) and data[offset] == 0xFF:
                offset += 1
                break
            cmajor, _, carg, offset = _decode_head(data, offset)
            if cmajor != major or carg is None:
                raise CBORError("invalid indefinite-length string chunk")
            if offset + carg > len(data):
                raise CBORError("truncated CBOR string")
            chunks.append(data[offset:offset + carg])
            offset += carg
        raw = b"".join(chunks)
    else:
        if offset + arg > len(data):
            raise CBORError("truncated CBOR string")
        raw = data[offset:offset + arg]
        offset += arg
    if major == 3:
        return raw.decode("utf-8", errors="surrogateescape"), offset
    return raw, offset


def _decode_half(h: int) -> float:
    sign = -1.0 if h & 0x8000 else 1.0
    exp = (h >> 10) & 0x1F
    frac = h & 0x3FF
    if exp == 0:
        return sign * frac * 2.0 ** -24
    if exp == 31:
        return sign * (math.inf if frac == 0 else math.nan)
    return sign * (frac + 1024.0) * 2.0 ** (exp - 25)


def _hashable(key: Any) -> Any:
    if isinstance(key, (dict, list)):
        raise CBORError("map keys of type table are not supported")
    return key


# --- encoding --------------------------------------------------------------

def dumps(value: Any) -> bytes:
    out = bytearray()
    _encode_item(value, out)
    return bytes(out)


def _encode_head(major: int, arg: int, out: bytearray) -> None:
    if arg < 24:
        out.append((major << 5) | arg)
    elif arg < 0x100:
        out.append((major << 5) | 24)
        out.append(arg)
    elif arg < 0x10000:
        out.append((major << 5) | 25)
        out += struct.pack(">H", arg)
    elif arg < 0x100000000:
        out.append((major << 5) | 26)
        out += struct.pack(">I", arg)
    else:
        out.append((major << 5) | 27)
        out += struct.pack(">Q", arg)


def _encode_item(value: Any, out: bytearray) -> None:
    if value is None:
        out.append(0xF6)
    elif value is True:
        out.append(0xF5)
    elif value is False:
        out.append(0xF4)
    elif isinstance(value, int):
        if value >= 0:
            _encode_head(0, value, out)
        else:
            _encode_head(1, -1 - value, out)
    elif isinstance(value, float):
        if value == int(value) and abs(value) < 2 ** 53 and math.isfinite(value):
            _encode_item(int(value), out)
        else:
            out.append(0xFB)
            out += struct.pack(">d", value)
    elif isinstance(value, bytes):
        _encode_head(2, len(value), out)
        out += value
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="surrogateescape")
        _encode_head(3, len(raw), out)
        out += raw
    elif isinstance(value, (list, tuple)):
        _encode_head(4, len(value), out)
        for item in value:
            _encode_item(item, out)
    elif isinstance(value, dict):
        _encode_head(5, len(value), out)
        for k, v in value.items():
            _encode_item(k, out)
            _encode_item(v, out)
    else:
        raise CBORError(f"cannot encode value of type {type(value).__name__}")
