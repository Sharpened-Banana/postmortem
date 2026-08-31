"""AceSerializer-3.0 (de)serialization, matching the Lua reference exactly.

Wire format: a stream of "^X" control codes with payload text between them.

    ^1        stream header (protocol rev 1)
    ^S<str>   string, with "~"-escapes for nonprints, "^", "~", DEL
    ^N<num>   number as written by Lua tostring()
    ^F<m>^f<e>  non-representable float: value = m * 2^e
    ^B / ^b   true / false
    ^Z        nil
    ^T ... ^t table: alternating key, value entries
    ^^        end of stream

Escapes ("~" == byte 126):
    byte 30      -> "~z"   (0x7a)
    bytes <= 32  -> "~" + chr(b + 64)
    byte 94 "^"  -> "~}"   (0x7d)
    byte 126 "~" -> "~|"   (0x7c)
    byte 127     -> "~{"   (0x7b)
"""

from __future__ import annotations

import math
import re
from typing import Any

_INF_STRINGS = {"1.#INF", "inf", "1.#INF00"}
_NEG_INF_STRINGS = {"-1.#INF", "-inf", "-1.#INF00"}


class AceSerializerError(ValueError):
    pass


# --- deserialization -------------------------------------------------------

def _unescape_char(m: re.Match[str]) -> str:
    esc = m.group(0)
    b = ord(esc[1])
    if b < 0x7A:  # "~" + chr(n + 64) for n <= 32
        return chr(b - 64)
    if b == 0x7A:
        return "\x1e"
    if b == 0x7B:
        return "\x7f"
    if b == 0x7C:
        return "~"
    if b == 0x7D:
        return "^"
    raise AceSerializerError(f"invalid escape sequence '~{esc[1]}'")


def _parse_number(text: str) -> float | int:
    if text in _NEG_INF_STRINGS:
        return -math.inf
    if text in _INF_STRINGS:
        return math.inf
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        raise AceSerializerError(f"invalid serialized number: {text!r}") from None


def loads(data: str) -> list[Any]:
    """Deserialize an AceSerializer stream; returns the list of top-level values."""
    # The Lua implementation strips all control characters and spaces first.
    data = re.sub(r"[\x00-\x1f\x7f ]", "", data)
    tokens = re.findall(r"(\^.)([^^]*)", data)
    it = iter(tokens)
    try:
        ctl, _ = next(it)
    except StopIteration:
        raise AceSerializerError("empty stream") from None
    if ctl != "^1":
        raise AceSerializerError("not AceSerializer data (rev 1)")

    values: list[Any] = []
    while True:
        try:
            ctl, payload = next(it)
        except StopIteration:
            raise AceSerializerError("missing terminator '^^'") from None
        if ctl == "^^":
            return values
        values.append(_read_value(it, ctl, payload))


def _read_value(it, ctl: str, payload: str) -> Any:
    if ctl == "^S":
        return re.sub(r"~.", _unescape_char, payload)
    if ctl == "^N":
        return _parse_number(payload)
    if ctl == "^F":
        ctl2, exp = next(it, (None, None))
        if ctl2 != "^f":
            raise AceSerializerError(f"expected '^f' after '^F', got {ctl2!r}")
        return float(payload) * (2.0 ** float(exp))
    if ctl == "^B":
        return True
    if ctl == "^b":
        return False
    if ctl == "^Z":
        return None
    if ctl == "^T":
        table: dict[Any, Any] = {}
        while True:
            try:
                ctl, payload = next(it)
            except StopIteration:
                raise AceSerializerError("unterminated table") from None
            if ctl == "^t":
                return table
            key = _read_value(it, ctl, payload)
            if key is None:
                raise AceSerializerError("invalid table format (nil key)")
            try:
                ctl, payload = next(it)
            except StopIteration:
                raise AceSerializerError("unterminated table") from None
            value = _read_value(it, ctl, payload)
            table[key] = value
    raise AceSerializerError(f"invalid control code {ctl!r}")


# --- serialization ---------------------------------------------------------

_ESCAPE_RE = re.compile(r"[\x00-\x20\x5e\x7e\x7f]")


def _escape_char(m: re.Match[str]) -> str:
    n = ord(m.group(0))
    if n == 30:
        return "~z"
    if n <= 32:
        return "~" + chr(n + 64)
    if n == 94:
        return "~}"
    if n == 126:
        return "~|"
    return "~{"  # n == 127


def _lua_tostring(v: float) -> str:
    # Lua 5.1 tostring() uses "%.14g"
    return f"{v:.14g}"


def _write_value(v: Any, out: list[str]) -> None:
    if isinstance(v, str):
        out.append("^S")
        out.append(_ESCAPE_RE.sub(_escape_char, v))
    elif isinstance(v, bool):
        out.append("^B" if v else "^b")
    elif isinstance(v, int):
        out.append("^N")
        out.append(str(v))
    elif isinstance(v, float):
        if math.isinf(v):
            out.append("^N")
            out.append("1.#INF" if v > 0 else "-1.#INF")
        elif float(_lua_tostring(v)) == v:
            out.append("^N")
            out.append(_lua_tostring(v))
        else:
            m, e = math.frexp(v)
            out.append("^F")
            out.append(f"{m * (2 ** 53):.0f}")
            out.append("^f")
            out.append(str(e - 53))
    elif isinstance(v, dict):
        out.append("^T")
        for k, val in v.items():
            if k is None:
                raise AceSerializerError("cannot serialize nil table key")
            _write_value(k, out)
            _write_value(val, out)
        out.append("^t")
    elif isinstance(v, (list, tuple)):
        # Lua arrays are tables with 1-based integer keys.
        out.append("^T")
        for i, val in enumerate(v, start=1):
            _write_value(i, out)
            _write_value(val, out)
        out.append("^t")
    elif v is None:
        out.append("^Z")
    else:
        raise AceSerializerError(f"cannot serialize value of type {type(v).__name__}")


def dumps(*values: Any) -> str:
    """Serialize values into an AceSerializer stream (protocol rev 1)."""
    out: list[str] = ["^1"]
    for v in values:
        _write_value(v, out)
    out.append("^^")
    return "".join(out)
