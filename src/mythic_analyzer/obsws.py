"""Native OBS WebSocket v5 client, built from scratch on the stdlib.

Two layers:

- :class:`WebSocketClient` -- a small, generic RFC 6455 WebSocket client
  (``ws://`` only, no TLS). Not a general-purpose implementation: it
  assumes the client role (outgoing frames are always masked, per spec),
  small single-frame text messages, and no outgoing fragmentation. That's
  everything obs-websocket needs and nothing more.

- :class:`OBSClient` -- the obs-websocket v5 protocol on top of that:
  Hello -> Identify -> Identified, then Request/RequestResponse for the
  handful of ops this tool needs (StartRecord, StopRecord,
  SaveReplayBuffer).

This exists so ``mythic_analyzer.recorder`` can drive OBS directly instead
of shelling out to a third-party tool like ``obs-cmd`` -- see
``recorder.py``'s module docstring. Every failure here is meant to be
caught by the caller and turned into a warning; nothing in this module
should be relied on to keep the process alive if OBS is unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import socket
import struct
from typing import Optional
from urllib.parse import urlparse

# --- Layer 1: RFC 6455 WebSocket client -------------------------------------

_HANDSHAKE_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_DEFAULT_TIMEOUT = 5.0  # seconds; a hung/unreachable OBS must not hang the recorder


class WebSocketError(Exception):
    """Any handshake, framing, or connection failure in :class:`WebSocketClient`."""


class WebSocketClient:
    """A bare-bones RFC 6455 client: handshake, text-frame send/recv."""

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()

    def connect(self, host: str, port: int, path: str = "/") -> None:
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise WebSocketError(f"tcp connect to {host}:{port} failed: {exc}") from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        try:
            self._handshake(host, port, path)
        except Exception:
            self.close()
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))

        raw_headers = self._read_until_headers_end()
        lines = raw_headers.split("\r\n")
        status_line = lines[0] if lines else ""
        if " 101 " not in f" {status_line} ":
            raise WebSocketError(f"handshake failed: unexpected status line {status_line!r}")

        headers = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + _HANDSHAKE_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise WebSocketError("handshake failed: Sec-WebSocket-Accept did not match")

    def _read_until_headers_end(self) -> str:
        while b"\r\n\r\n" not in self._buf:
            chunk = self._recv_raw(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._buf.extend(chunk)
        idx = self._buf.index(b"\r\n\r\n")
        header_bytes = bytes(self._buf[:idx])
        del self._buf[: idx + 4]
        return header_bytes.decode("iso-8859-1")

    def _recv_raw(self, n: int) -> bytes:
        try:
            return self._sock.recv(n)
        except socket.timeout as exc:
            raise WebSocketError("socket timeout") from exc
        except OSError as exc:
            raise WebSocketError(f"socket error: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._recv_raw(4096)
            if not chunk:
                raise WebSocketError("connection closed unexpectedly")
            self._buf.extend(chunk)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("not connected")
        length = len(payload)
        header = bytes([0x80 | (opcode & 0x0F)])  # FIN=1, no fragmentation on send
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        # Client-to-server frames MUST be masked (RFC 6455 5.1/5.2).
        mask_key = os.urandom(4)
        masked = bytearray(payload)
        for i in range(length):
            masked[i] ^= mask_key[i % 4]
        try:
            self._sock.sendall(header + mask_key + bytes(masked))
        except OSError as exc:
            raise WebSocketError(f"send failed: {exc}") from exc

    def recv_text(self) -> str:
        """Block until one full text message arrives.

        Pings are answered with a pong carrying the same payload; pongs
        are ignored. This assumes the peer never fragments a data frame
        (FIN=1 on every TEXT/BINARY frame it sends) -- obs-websocket's
        JSON messages are small and never fragmented in practice, so a
        continuation frame is treated as a protocol error rather than
        reassembled.
        """
        while True:
            opcode, payload = self._recv_frame()
            if opcode == _OP_TEXT:
                return payload.decode("utf-8")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                raise WebSocketError("connection closed by peer")
            raise WebSocketError(f"unsupported/unfragmented opcode {opcode:#x}")

    def _recv_frame(self):
        b0, b1 = self._recv_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._recv_exact(8))
        mask_key = self._recv_exact(4) if masked else None
        payload = bytearray(self._recv_exact(length)) if length else bytearray()
        if masked and mask_key:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]
        if not fin:
            raise WebSocketError("fragmented (FIN=0) messages are not supported")
        return opcode, bytes(payload)

    def close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_frame(_OP_CLOSE, b"")
        except Exception:
            pass  # best-effort; we're closing regardless
        self._sock = None
        try:
            sock.close()
        except OSError:
            pass


# --- Layer 2: obs-websocket v5 protocol -------------------------------------

_OP_HELLO = 0
_OP_IDENTIFY = 1
_OP_IDENTIFIED = 2
_OP_REQUEST = 6
_OP_REQUEST_RESPONSE = 7


class OBSError(Exception):
    """Any obs-websocket connect/auth/request failure in :class:`OBSClient`."""


def compute_auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 authentication string (protocol spec, verbatim):

        secret = base64(sha256(password + salt))
        authentication = base64(sha256(secret + challenge))

    Note the second round concatenates ``secret``'s own base64 *text*
    with ``challenge``'s text -- not raw bytes. See the module docstring
    of the test file for how the expected test vector was independently
    hand-verified.
    """
    secret_digest = hashlib.sha256((password + salt).encode("utf-8")).digest()
    secret = base64.b64encode(secret_digest).decode("ascii")
    auth_digest = hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    return base64.b64encode(auth_digest).decode("ascii")


class OBSClient:
    """obs-websocket v5 client: connection/auth handshake + a few requests.

    Usage::

        client = OBSClient("ws://127.0.0.1:4455", password="hunter2")
        client.connect()
        client.start_record()
        ...
        path = client.stop_record()
        client.close()

    Callers don't need to know anything about WebSocket framing or the
    auth challenge/response -- that's all handled inside :meth:`connect`.
    """

    def __init__(self, url: str, password: Optional[str] = None,
                 timeout: float = _DEFAULT_TIMEOUT):
        self.url = url
        self.password = password
        self.timeout = timeout
        self._ws: Optional[WebSocketClient] = None
        self._request_ids = itertools.count(1)

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", ""):
            raise OBSError(
                f"unsupported OBS WebSocket URL {self.url!r} "
                f"(only ws:// is supported -- OBS's default is unencrypted)"
            )
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 4455
        path = parsed.path or "/"

        self._ws = WebSocketClient(timeout=self.timeout)
        try:
            self._ws.connect(host, port, path)
            self._identify()
        except Exception as exc:
            self.close()
            if isinstance(exc, (OBSError, WebSocketError)):
                raise
            raise OBSError(f"obs-websocket connect failed: {exc}") from exc

    def _identify(self) -> None:
        hello = self._recv_json()
        if hello.get("op") != _OP_HELLO:
            raise OBSError(f"expected Hello (op 0) from OBS, got op {hello.get('op')}")
        d = hello.get("d") or {}
        rpc_version = d.get("rpcVersion", 1)

        identify_d = {"rpcVersion": rpc_version}
        auth = d.get("authentication")
        if auth:
            if not self.password:
                raise OBSError(
                    "OBS requires a password but none was configured "
                    "(--obs-password)"
                )
            identify_d["authentication"] = compute_auth_string(
                self.password, auth.get("salt", ""), auth.get("challenge", ""),
            )

        self._send_json({"op": _OP_IDENTIFY, "d": identify_d})
        try:
            response = self._recv_json()
        except WebSocketError as exc:
            raise OBSError(f"authentication failed: {exc}") from exc
        if response.get("op") != _OP_IDENTIFIED:
            raise OBSError(
                f"authentication failed (expected Identified/op 2, "
                f"got op {response.get('op')})"
            )

    def _send_json(self, obj: dict) -> None:
        self._ws.send_text(json.dumps(obj))

    def _recv_json(self) -> dict:
        return json.loads(self._ws.recv_text())

    def _request(self, request_type: str, request_data: Optional[dict] = None) -> Optional[dict]:
        if self._ws is None:
            raise OBSError("not connected")
        request_id = str(next(self._request_ids))
        d = {"requestType": request_type, "requestId": request_id}
        if request_data is not None:
            d["requestData"] = request_data
        self._send_json({"op": _OP_REQUEST, "d": d})

        while True:
            msg = self._recv_json()
            if msg.get("op") != _OP_REQUEST_RESPONSE:
                continue  # ignore events / anything else while we wait
            rd = msg.get("d") or {}
            if rd.get("requestId") != request_id:
                continue  # stray/late response for an earlier request
            status = rd.get("requestStatus") or {}
            if not status.get("result"):
                raise OBSError(
                    f"{request_type} failed: "
                    f"{status.get('comment') or status.get('code')}"
                )
            return rd.get("responseData")

    def start_record(self) -> None:
        self._request("StartRecord")

    def stop_record(self) -> Optional[str]:
        data = self._request("StopRecord")
        if not data:
            return None
        return data.get("outputPath")

    def save_replay_buffer(self) -> None:
        self._request("SaveReplayBuffer")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
