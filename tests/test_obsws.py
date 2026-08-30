"""Tests for WP-D1: the native OBS WebSocket v5 client.

Covers, in order:

- Layer 1 (RFC 6455 framing): send/recv round-trip over a real (but
  purely in-process, AF_UNIX) connected socket pair, including a
  payload long enough to exercise the 126+ byte extended-length path.
- Layer 2 (obs-websocket v5 auth): the challenge/salt -> authentication
  string algorithm against a hand-verified vector (computed
  independently with a standalone hashlib/base64 snippet -- see the
  comment on TestAuthString -- not by calling our own implementation
  and trusting its own output).
- End-to-end connection flow against a small in-process fake
  WebSocket/obs-websocket server (stdlib socket + threading only, real
  RFC 6455 handshake) covering both the plain WebSocketClient and the
  full obs-websocket v5 Hello/Identify/Identified/Request/
  RequestResponse flow, with and without a password/auth challenge.
- Recorder integration: all-failures-are-warnings, connect-once-per-run
  connection reuse, and shell-hook precedence over the native client.

Nothing here touches a real OBS installation or any network beyond
127.0.0.1 loopback / AF_UNIX sockets this test process owns itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time

import pytest
from conftest import build_run_log

from mythic_analyzer.obsws import (
    OBSClient,
    OBSError,
    WebSocketClient,
    compute_auth_string,
)
from mythic_analyzer.recorder import Recorder

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- Layer 1: WebSocket frame round-trip ------------------------------------

class TestFrameRoundTrip:
    """Encode with one WebSocketClient's send path, decode with another's
    recv path, over a real connected socket pair (AF_UNIX, no network)."""

    def _roundtrip(self, payload: str) -> str:
        a, b = socket.socketpair()
        try:
            sender = WebSocketClient.__new__(WebSocketClient)
            sender.timeout = 5.0
            sender._sock = a
            sender._buf = bytearray()

            receiver = WebSocketClient.__new__(WebSocketClient)
            receiver.timeout = 5.0
            receiver._sock = b
            receiver._buf = bytearray()

            sender.send_text(payload)
            return receiver.recv_text()
        finally:
            a.close()
            b.close()

    def test_short_payload(self):
        assert self._roundtrip("hello obs") == "hello obs"

    def test_empty_payload(self):
        assert self._roundtrip("") == ""

    def test_extended_length_payload(self):
        # 300 bytes forces the 126+ byte extended-length (16-bit) framing
        # path (payload length no longer fits the 7-bit inline field).
        payload = "x" * 300
        got = self._roundtrip(payload)
        assert got == payload
        assert len(got) == 300

    def test_json_like_payload(self):
        payload = json.dumps({"op": 6, "d": {"requestType": "StartRecord",
                                              "requestId": "1"}})
        assert self._roundtrip(payload) == payload


# --- Layer 2: obs-websocket v5 auth-string algorithm ------------------------

class TestAuthString:
    def test_known_vector(self):
        # Hand-verified independently of compute_auth_string, via a
        # standalone snippet using only hashlib/base64 directly:
        #
        #   secret_digest = sha256(b"hunter2" + b"saltvalue123==")
        #   secret = base64(secret_digest)
        #     -> "De7dsA6GwqEOrch1gW7m2D+1QMOb0P4xiizTb+HUV88="
        #   auth_digest = sha256(secret.encode() + b"challengevalue456==")
        #   auth = base64(auth_digest)
        #     -> "poz8o+UhT6jbmMBFwphVZwT7gyMJ84guk+3Qj5RaX+o="
        got = compute_auth_string("hunter2", "saltvalue123==", "challengevalue456==")
        assert got == "poz8o+UhT6jbmMBFwphVZwT7gyMJ84guk+3Qj5RaX+o="

    def test_reference_reimplementation_matches(self):
        # A second, independently-written re-implementation of the same
        # documented two-round algorithm (not imported from obsws.py),
        # so this doesn't just check compute_auth_string against itself.
        password, salt, challenge = "swordfish", "abc123==", "xyz789=="
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode("utf-8")).digest()
        ).decode("ascii")
        expected = base64.b64encode(
            hashlib.sha256((secret + challenge).encode("utf-8")).digest()
        ).decode("ascii")
        assert compute_auth_string(password, salt, challenge) == expected

    def test_different_passwords_give_different_strings(self):
        a = compute_auth_string("password-a", "salt", "challenge")
        b = compute_auth_string("password-b", "salt", "challenge")
        assert a != b


# --- fake in-process WebSocket / obs-websocket v5 server --------------------
#
# A real (if minimal) RFC 6455 server: it performs the actual handshake
# response and speaks the actual frame format, all with stdlib
# socket+threading. Deliberately does NOT import anything from obsws.py,
# so tests against it exercise the client's real wire behavior rather
# than two halves of the same implementation agreeing with each other.

def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def _server_recv_frame(conn: socket.socket):
    b0, b1 = _recv_exact(conn, 2)
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(conn, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(conn, 8))
    mask_key = _recv_exact(conn, 4) if masked else b""
    payload = bytearray(_recv_exact(conn, length)) if length else bytearray()
    if masked:
        for i in range(len(payload)):
            payload[i] ^= mask_key[i % 4]
    return opcode, bytes(payload)


def _server_send_frame(conn: socket.socket, opcode: int, payload: bytes) -> None:
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    conn.sendall(header + payload)  # server -> client frames are unmasked


def _server_send_json(conn: socket.socket, obj: dict) -> None:
    _server_send_frame(conn, 0x1, json.dumps(obj).encode("utf-8"))


def _server_recv_json(conn: socket.socket):
    opcode, payload = _server_recv_frame(conn)
    if opcode == 0x8:  # close frame
        return None
    return json.loads(payload)


def _do_ws_handshake(conn: socket.socket) -> bool:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
    header_bytes, _, _ = data.partition(b"\r\n\r\n")
    headers = {}
    for line in header_bytes.split(b"\r\n")[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
    key = headers.get(b"sec-websocket-key", b"").decode("ascii")
    accept = base64.b64encode(
        hashlib.sha1((key + _GUID).encode("ascii")).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    conn.sendall(resp.encode("ascii"))
    return True


class FakeWSServer:
    """Binds 127.0.0.1:0, accepts one connection, performs a real RFC
    6455 handshake, then hands the raw socket to ``handler`` (run in a
    background thread). With ``do_handshake=False`` it instead closes
    the connection immediately, simulating a dead/misbehaving peer."""

    def __init__(self, handler=None, do_handshake: bool = True):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.host, self.port = self.sock.getsockname()
        self.connections = 0
        self._handler = handler
        self._do_handshake = do_handshake
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def _serve(self) -> None:
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        self.connections += 1
        try:
            if not self._do_handshake:
                return  # close immediately without a handshake response
            if not _do_ws_handshake(conn):
                return
            if self._handler is not None:
                self._handler(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def make_obs_handler(calls: list, password: str = None, salt: str = "saltxyz",
                      challenge: str = "challengexyz", rpc_version: int = 1,
                      output_path: str = "/tmp/fake_recording.mp4"):
    """Build a handler speaking the full obs-websocket v5 flow: Hello
    (with an auth challenge iff ``password`` is set) -> Identify (auth
    validated independently of obsws.py's own algorithm) -> Identified
    -> Request/RequestResponse in a loop, recording each requestType
    into ``calls``."""

    def handler(conn: socket.socket) -> None:
        hello_d = {"rpcVersion": rpc_version}
        if password is not None:
            hello_d["authentication"] = {"challenge": challenge, "salt": salt}
        _server_send_json(conn, {"op": 0, "d": hello_d})

        identify = _server_recv_json(conn)
        if identify is None or identify.get("op") != 1:
            return

        if password is not None:
            secret = base64.b64encode(
                hashlib.sha256((password + salt).encode("utf-8")).digest()
            ).decode("ascii")
            expected = base64.b64encode(
                hashlib.sha256((secret + challenge).encode("utf-8")).digest()
            ).decode("ascii")
            got = (identify.get("d") or {}).get("authentication")
            if got != expected:
                return  # simulate auth failure: no Identified, connection closes

        _server_send_json(conn, {"op": 2, "d": {}})

        while True:
            msg = _server_recv_json(conn)
            if msg is None:
                return
            if msg.get("op") != 6:
                continue
            d = msg["d"]
            calls.append(d["requestType"])
            response_data = {}
            if d["requestType"] == "StopRecord":
                response_data = {"outputPath": output_path}
            _server_send_json(conn, {
                "op": 7,
                "d": {
                    "requestType": d["requestType"],
                    "requestId": d["requestId"],
                    "requestStatus": {"result": True, "code": 100},
                    "responseData": response_data,
                },
            })

    return handler


# --- Layer 1 client against the fake server ---------------------------------

class TestWebSocketClientAgainstFakeServer:
    def test_connect_handshake_and_echo(self):
        def echo_handler(conn):
            _opcode, payload = _server_recv_frame(conn)
            _server_send_frame(conn, 0x1, payload)

        server = FakeWSServer(echo_handler)
        try:
            ws = WebSocketClient(timeout=5.0)
            ws.connect(server.host, server.port, "/")
            ws.send_text("ping-pong")
            assert ws.recv_text() == "ping-pong"
            ws.close()
        finally:
            server.close()


# --- Layer 2 client (OBSClient) against the fake server ---------------------

class TestOBSClientAgainstFakeServer:
    def test_start_and_stop_record_no_password(self):
        calls = []
        server = FakeWSServer(make_obs_handler(calls))
        try:
            client = OBSClient(server.url)
            client.connect()
            client.start_record()
            path = client.stop_record()
            client.close()
        finally:
            server.close()
        assert calls == ["StartRecord", "StopRecord"]
        assert path == "/tmp/fake_recording.mp4"

    def test_start_record_with_password_and_challenge(self):
        calls = []
        server = FakeWSServer(make_obs_handler(calls, password="hunter2"))
        try:
            client = OBSClient(server.url, password="hunter2")
            client.connect()
            client.start_record()
            client.close()
        finally:
            server.close()
        assert calls == ["StartRecord"]

    def test_wrong_password_raises_obs_error(self):
        calls = []
        server = FakeWSServer(make_obs_handler(calls, password="hunter2"))
        client = OBSClient(server.url, password="WRONG")
        try:
            with pytest.raises(OBSError):
                client.connect()
        finally:
            client.close()
            server.close()
        assert calls == []  # never got past auth to send a request

    def test_save_replay_buffer(self):
        calls = []
        server = FakeWSServer(make_obs_handler(calls))
        try:
            client = OBSClient(server.url)
            client.connect()
            client.save_replay_buffer()
            client.close()
        finally:
            server.close()
        assert calls == ["SaveReplayBuffer"]


# --- Recorder integration ----------------------------------------------------

class TestRecorderObsFailuresAreWarnings:
    def test_connection_refused_is_a_warning_not_a_crash(self, tmp_path):
        # Grab an ephemeral port and immediately release it so nothing is
        # listening -- a fast, reliable "unreachable OBS" without a
        # multi-second timeout.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        log = tmp_path / "WoWCombatLog.txt"
        log.write_text(build_run_log().text(), encoding="utf-8")
        echoed = []
        rec = Recorder(
            log_path=log, out_dir=tmp_path / "runs", from_start=True,
            echo=echoed.append,
            obs_url=f"ws://127.0.0.1:{port}",
        )
        runs = rec.watch(stop_after_runs=1)

        assert len(runs) == 1
        assert runs[0].completed
        assert runs[0].zone == "Murder Row"
        assert runs[0].player_deaths == 1  # log-slice / death counting unaffected
        assert any("warning: obs connect failed" in line for line in echoed)

    def test_server_that_drops_the_handshake_is_a_warning(self, tmp_path):
        server = FakeWSServer(do_handshake=False)
        try:
            log = tmp_path / "WoWCombatLog.txt"
            log.write_text(build_run_log().text(), encoding="utf-8")
            echoed = []
            rec = Recorder(
                log_path=log, out_dir=tmp_path / "runs", from_start=True,
                echo=echoed.append,
                obs_url=server.url,
                obs_replay_on_death=True,
            )
            runs = rec.watch(stop_after_runs=1)

            assert len(runs) == 1
            assert runs[0].completed
            assert runs[0].player_deaths == 1
            assert any("warning: obs" in line for line in echoed)
        finally:
            server.close()


class TestReplayOnDeath:
    def test_replay_buffer_saved_on_death_same_connection_reused(self, tmp_path):
        calls = []
        server = FakeWSServer(make_obs_handler(calls))
        try:
            log = tmp_path / "WoWCombatLog.txt"
            log.write_text(build_run_log().text(), encoding="utf-8")
            rec = Recorder(
                log_path=log, out_dir=tmp_path / "runs", from_start=True,
                echo=lambda s: None,
                obs_url=server.url,
                obs_replay_on_death=True,
            )
            runs = rec.watch(stop_after_runs=1)
        finally:
            server.close()

        assert len(runs) == 1
        assert runs[0].player_deaths == 1
        assert runs[0].obs_output_path == "/tmp/fake_recording.mp4"
        # The fake server only ever accepts ONE connection (a single
        # accept() call) -- StartRecord, the death-triggered
        # SaveReplayBuffer, and StopRecord all succeeding in this exact
        # order proves the same connection was reused across the run
        # rather than reconnecting per operation.
        assert calls == ["StartRecord", "SaveReplayBuffer", "StopRecord"]
        assert server.connections == 1


class TestShellHookPrecedence:
    def test_native_obs_not_used_when_both_shell_hooks_configured(self, tmp_path):
        calls = []
        server = FakeWSServer(make_obs_handler(calls))
        try:
            log = tmp_path / "WoWCombatLog.txt"
            log.write_text(build_run_log().text(), encoding="utf-8")
            start_marker = tmp_path / "started.txt"
            end_marker = tmp_path / "ended.txt"
            rec = Recorder(
                log_path=log, out_dir=tmp_path / "runs", from_start=True,
                echo=lambda s: None,
                on_start_cmd=f'echo "$MA_ZONE" > "{start_marker}"',
                on_end_cmd=f'echo "$MA_ZONE" > "{end_marker}"',
                obs_url=server.url,
            )
            runs = rec.watch(stop_after_runs=1)
            assert len(runs) == 1

            for _ in range(50):
                if start_marker.exists() and end_marker.exists():
                    break
                time.sleep(0.05)
            assert start_marker.read_text().strip() == "Murder Row"
            assert end_marker.read_text().strip() == "Murder Row"

            # Give a wrongly-attempted native connection a moment to have
            # shown up, if the precedence rule were broken.
            time.sleep(0.2)
            assert server.connections == 0
            assert calls == []
        finally:
            server.close()

    def test_replay_on_death_still_uses_native_client_despite_hooks(self, tmp_path):
        # --obs-replay-on-death has no shell-hook equivalent, so it
        # always uses the native client regardless of --on-run-start/
        # --on-run-end being configured for the start/end events.
        calls = []
        server = FakeWSServer(make_obs_handler(calls))
        try:
            log = tmp_path / "WoWCombatLog.txt"
            log.write_text(build_run_log().text(), encoding="utf-8")
            rec = Recorder(
                log_path=log, out_dir=tmp_path / "runs", from_start=True,
                echo=lambda s: None,
                on_start_cmd="true",
                on_end_cmd="true",
                obs_url=server.url,
                obs_replay_on_death=True,
            )
            runs = rec.watch(stop_after_runs=1)
            assert len(runs) == 1
            assert runs[0].player_deaths == 1
        finally:
            server.close()
        # native start/stop suppressed by the hooks, but the death-time
        # SaveReplayBuffer still went through, and only that call
        assert calls == ["SaveReplayBuffer"]
