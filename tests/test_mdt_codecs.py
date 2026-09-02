"""Codec-level tests: printable encoding, AceSerializer, CBOR, MDT strings."""

import math
import os

import pytest

from postmortem.mdt import ace_serializer, cbor
from postmortem.mdt.decode import (
    MDTDecodeError,
    decode_mdt_string,
    encode_mdt_string,
)
from postmortem.mdt.print_codec import decode_for_print, encode_for_print
from postmortem.mdt.route import Route


class TestPrintCodec:
    def test_round_trip_all_lengths(self):
        for n in range(0, 40):
            data = os.urandom(n)
            assert decode_for_print(encode_for_print(data)) == data

    def test_known_alphabet(self):
        # 0x00 0x00 0x00 -> indices 0,0,0,0 -> "aaaa"
        assert encode_for_print(b"\x00\x00\x00") == "aaaa"
        # a single 0xff byte: 8 bits -> two 6-bit chars: 63, 3 -> ")d"
        assert encode_for_print(b"\xff") == ")d"

    def test_invalid_character(self):
        with pytest.raises(ValueError):
            decode_for_print("abc!")


class TestAceSerializer:
    def test_scalars(self):
        s = ace_serializer.dumps("hi", 42, -7, True, False, None, 3.25)
        assert ace_serializer.loads(s) == ["hi", 42, -7, True, False, None, 3.25]

    def test_escapes(self):
        tricky = "a^b~c\x1e d\x7f\ne,f"
        s = ace_serializer.dumps(tricky)
        assert ace_serializer.loads(s) == [tricky]

    def test_nested_tables(self):
        val = {"a": {1: [10, 20], "b": {"c": True}}, 2: "x"}
        (out,) = ace_serializer.loads(ace_serializer.dumps(val))
        assert out["a"][1] == {1: 10, 2: 20}  # lists become 1-based tables
        assert out["a"]["b"] == {"c": True}
        assert out[2] == "x"

    def test_non_representable_float(self):
        v = 0.1 + 0.2  # not exactly representable in %.14g round trip? it is; use tiny
        v = 2.0 ** -1074  # denormal min: forces ^F encoding path robustness
        (out,) = ace_serializer.loads(ace_serializer.dumps(v))
        assert out == v

    def test_infinity(self):
        out = ace_serializer.loads(ace_serializer.dumps(math.inf, -math.inf))
        assert out == [math.inf, -math.inf]

    def test_wire_format(self):
        assert ace_serializer.dumps(1, "a") == "^1^N1^Sa^^"
        assert ace_serializer.loads("^1^N1^Sa^^") == [1, "a"]

    def test_rejects_garbage(self):
        with pytest.raises(ace_serializer.AceSerializerError):
            ace_serializer.loads("not ace data")


class TestCBOR:
    @pytest.mark.parametrize("value,encoded", [
        (0, b"\x00"),
        (23, b"\x17"),
        (24, b"\x18\x18"),
        (500, b"\x19\x01\xf4"),
        (-1, b"\x20"),
        (-100, b"\x38\x63"),
        ("a", b"\x61a"),
        (b"\x01\x02", b"\x42\x01\x02"),
        ([1, 2], b"\x82\x01\x02"),
        (True, b"\xf5"),
        (False, b"\xf4"),
        (None, b"\xf6"),
    ])
    def test_known_vectors(self, value, encoded):
        assert cbor.dumps(value) == encoded
        assert cbor.loads(encoded) == value

    def test_half_float_decode(self):
        assert cbor.loads(b"\xf9\x3c\x00") == 1.0
        assert cbor.loads(b"\xf9\x80\x00") == -0.0

    def test_float64_round_trip(self):
        v = 123.456
        assert cbor.loads(cbor.dumps(v)) == v

    def test_map_round_trip(self):
        v = {"pulls": {1: [1, 2, 3], "color": "ff8000"}, "week": 2}
        assert cbor.loads(cbor.dumps(v)) == v

    def test_indefinite_length(self):
        # 0x9f = indefinite array, 0xff = break
        assert cbor.loads(b"\x9f\x01\x02\xff") == [1, 2]
        # indefinite text string of two chunks
        assert cbor.loads(b"\x7f\x61a\x61b\xff") == "ab"

    def test_tag_transparent(self):
        # tag 0 (0xc0) wrapping a string
        assert cbor.loads(b"\xc0\x61a") == "a"

    def test_truncated(self):
        with pytest.raises(cbor.CBORError):
            cbor.loads(b"\x19\x01")


class TestMDTString:
    # A real Keystone.guru "Export to MDT" string (public route 0RhVlYt,
    # Altar of Fangs, captured 2026-09-02). Keystone.guru encodes every
    # string as a CBOR *byte* string where MDT uses *text* strings, so
    # this decoded to b'value'/b'currentDungeonIdx' keys and was rejected
    # as "preset has no 'value' table" -- a real report from a user
    # pasting exactly this kind of string into Settings.
    KEYSTONE_GURU_EXPORT = (
        "!~MDT2~TZLLbtpAGIXze8bjwVcIINHLInmAohhaUpYokDQhKmnTVuoqAjwmbge78iUlXcUGIlXqU6Q0aTd9um"
        "ZfO0oQi1mMzvnOmX9mbva8wSc2DIPpVcu9bVnzTqPxsrq9+8xsmNVtuO0dvj9+t8H7Qbgx8IJg88FlbtWqz1PX"
        "dr3ahNvDO9eXiPMNc2mp1+4sNbNZfSGsWhqbB5Zj284w4uF5qf2Vsc9rnbM+j9jvN8PI95kbtiN3xDx335pUFv"
        "sB4+kJHc+96N7LR2kM7IWMjR13tNb1HTvs2XbAwuCik1UEP64hBjIXCRVA+k4kmsMgiEiOQZnmC+udocc9f9e26"
        "8y2f0EskGkOYSkpK8q0VC7ey5nYZAuIEUlURbpcL+iGpq6wdZbWYJJoupQUS3IiIGVWeVR5XHlSebrMaKYpV3oM"
        "S84e1NlCuMzrhgBIjQElgFYaM3EmYomosYBivFLXZDfCVFMVI2NkRY1ROQGhkK4V3LYXaKZqupGLQZuDgLC4jGh"
        "mzeJlKqZ3QGNBm8mKbizhDF+QJG9os/ViIS8vy/tmVn8NCRG1pFSmMS7GkF8ZKSOPmMvG560gcEbuOH2j4BrmE"
        "qVUQn8AI4qJjBWiUg3rxADyF8R0eoxBxIRQlCMKVolGdGzgPKFXAsVUntGUV6dpRu/+3Y+jAWdnjEM7ZJOwx7gT"
        "fov8kz4P+/6JuTO2nJ+vbZ+xU49bB57jMuvfq4c97A/YKHJ4+mNgJ3Ks7tbb0w/8YziZdPf+Aw=="
    )

    def test_keystone_guru_export_decodes_like_an_mdt_one(self):
        from postmortem.mdt.route import Route
        preset = decode_mdt_string(self.KEYSTONE_GURU_EXPORT)
        assert "value" in preset and all(isinstance(k, str) for k in preset)
        route = Route.from_preset(preset)
        assert route.dungeon_idx == 164            # Altar of Fangs
        assert route.name == "elitzur_altar_1"
        assert len(route.pulls) == 12

    def test_mdt2_round_trip(self, route_string):
        preset = decode_mdt_string(route_string)
        route = Route.from_preset(preset)
        assert route.name == "Test MR Route"
        assert route.dungeon_idx == 160
        assert len(route.pulls) == 4
        assert route.pulls[0].enemies == {1: [1, 2]}
        assert route.pulls[0].color == "ff8000"

    def test_legacy_round_trip(self):
        from conftest import ROUTE_PRESET
        enc = encode_mdt_string(ROUTE_PRESET, "legacy")
        assert enc.startswith("!") and not enc.startswith("!~MDT2~")
        route = Route.from_preset(decode_mdt_string(enc))
        assert route.name == "Test MR Route"
        assert [p.index for p in route.pulls] == [1, 2, 3, 4]

    def test_whitespace_tolerated(self, route_string):
        assert decode_mdt_string("  " + route_string + "\n")

    def test_garbage_rejected(self):
        with pytest.raises(MDTDecodeError):
            decode_mdt_string("!~MDT2~definitely-not-base64!!!")
        with pytest.raises(MDTDecodeError):
            decode_mdt_string("")

    def test_garbled_print_and_legacy_strings_raise_MDTDecodeError_not_valueerror(self):
        # Regression (2026-09-01 debug sweep): the "!"-print and ancient
        # LibCompress paths ran decode_for_print / ace decoding, which
        # raise bare ValueError (char outside the alphabet) or
        # OverflowError (ace ^F float) -- these escaped past _load_route's
        # `except MDTDecodeError`, giving a raw traceback on any mistyped
        # or garbled paste instead of a clean "could not decode" message.
        for bad in ["!abc$def", "!abc=def", "~garbage~", "xxxxxxxxxx", "!!!"]:
            with pytest.raises(MDTDecodeError):
                decode_mdt_string(bad)
