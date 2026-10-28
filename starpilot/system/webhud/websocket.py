"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Minimal server-side WebSocket (RFC 6455) on top of http.server -- the AGNOS venv has no aiohttp or
websockets package, and the HUD only needs text frames, ping/pong and close.
"""
import base64
import hashlib
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1 << 20   # client -> server messages are small JSON commands

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
  pass


def accept_key(key: str) -> str:
  return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bytes | None = None) -> bytes:
  """Single FIN frame. Servers never mask; `mask` exists for tests that play the client."""
  n = len(payload)
  head = bytearray([0x80 | opcode])
  mask_bit = 0x80 if mask else 0
  if n < 126:
    head.append(mask_bit | n)
  elif n < (1 << 16):
    head.append(mask_bit | 126)
    head += struct.pack("!H", n)
  else:
    head.append(mask_bit | 127)
    head += struct.pack("!Q", n)
  if mask:
    head += mask
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
  return bytes(head) + payload


def _read_exact(rfile, n: int) -> bytes:
  buf = bytearray()
  while len(buf) < n:
    chunk = rfile.read(n - len(buf))
    if not chunk:
      raise WebSocketClosed("connection closed")
    buf.extend(chunk)
  return bytes(buf)


def read_frame(rfile) -> tuple[bool, int, bytes]:
  b0, b1 = _read_exact(rfile, 2)
  fin, opcode = bool(b0 & 0x80), b0 & 0x0F
  masked, n = bool(b1 & 0x80), b1 & 0x7F
  if n == 126:
    n = struct.unpack("!H", _read_exact(rfile, 2))[0]
  elif n == 127:
    n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
  if n > MAX_MESSAGE:
    raise WebSocketClosed("frame too large")
  mask = _read_exact(rfile, 4) if masked else None
  payload = _read_exact(rfile, n) if n else b""
  if mask:
    # XOR in one shot via int arithmetic; per-byte Python loops are slow on the device
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    payload = (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")
  return fin, opcode, payload


class WebSocket:
  """Wraps an upgraded http.server connection. send() is thread-safe; recv() is single-reader."""

  def __init__(self, sock: socket.socket, rfile):
    self.sock = sock
    self.rfile = rfile
    self.closed = False
    self._send_lock = threading.Lock()

  def send_text(self, text: str) -> None:
    self._send(encode_frame(text.encode(), OP_TEXT))

  def _send(self, frame: bytes) -> None:
    if self.closed:
      raise WebSocketClosed("closed")
    with self._send_lock:
      try:
        self.sock.sendall(frame)
      except OSError as e:
        self.closed = True
        raise WebSocketClosed(str(e)) from e

  def recv(self) -> str | None:
    """Next text message, or None once the peer closed."""
    parts: list[bytes] = []
    size = 0
    while True:
      try:
        fin, opcode, payload = read_frame(self.rfile)
      except (WebSocketClosed, OSError, ValueError):
        self.closed = True
        return None
      if opcode == OP_PING:
        self._send(encode_frame(payload, OP_PONG))
        continue
      if opcode == OP_PONG:
        continue
      if opcode == OP_CLOSE:
        try:
          self._send(encode_frame(payload[:2], OP_CLOSE))
        except WebSocketClosed:
          pass
        self.closed = True
        return None
      if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
        parts.append(payload)
        size += len(payload)
        if size > MAX_MESSAGE:
          self.close()
          return None
        if fin:
          return b"".join(parts).decode(errors="replace")

  def close(self) -> None:
    if not self.closed:
      try:
        self._send(encode_frame(struct.pack("!H", 1000), OP_CLOSE))
      except WebSocketClosed:
        pass
    self.closed = True
    try:
      self.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
      pass
