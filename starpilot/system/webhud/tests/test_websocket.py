"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import io
import socket
import struct

from openpilot.starpilot.system.webhud.websocket import OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, WebSocket, accept_key, encode_frame, read_frame

MASK = b"\x37\xfa\x21\x3d"


class TestWebSocket:
  def test_accept_key_rfc6455_example(self):
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

  def test_masked_client_frames(self):
    for n in (0, 5, 125, 126, 300, 70000):
      payload = bytes(i % 251 for i in range(n))
      fin, op, out = read_frame(io.BytesIO(encode_frame(payload, OP_TEXT, mask=MASK)))
      assert fin and op == OP_TEXT and out == payload

  def test_rfc_masked_hello(self):
    # RFC 6455 5.7: a single-frame masked text message containing "Hello"
    frame = bytes([0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58])
    assert read_frame(io.BytesIO(frame)) == (True, OP_TEXT, b"Hello")

  def test_server_frame_lengths(self):
    assert encode_frame(b"x" * 10)[:2] == bytes([0x81, 10])
    assert encode_frame(b"x" * 200)[:4] == bytes([0x81, 126]) + struct.pack("!H", 200)
    assert encode_frame(b"x" * 70000)[:10] == bytes([0x81, 127]) + struct.pack("!Q", 70000)

  def test_session_ping_fragments_close(self):
    server_sock, client_sock = socket.socketpair()
    try:
      ws = WebSocket(server_sock, server_sock.makefile("rb"))
      # ping is answered, a fragmented message is reassembled
      client_sock.sendall(encode_frame(b"hi", OP_PING, mask=MASK))
      first = bytes([0x01, 0x80 | 3]) + MASK + bytes(b ^ MASK[i % 4] for i, b in enumerate(b"abc"))
      last = bytes([0x80, 0x80 | 3]) + MASK + bytes(b ^ MASK[i % 4] for i, b in enumerate(b"def"))
      client_sock.sendall(first + last)
      assert ws.recv() == "abcdef"
      client_reader = client_sock.makefile("rb")
      assert read_frame(client_reader) == (True, OP_PONG, b"hi")

      ws.send_text("state")
      assert read_frame(client_reader) == (True, OP_TEXT, b"state")

      client_sock.sendall(encode_frame(struct.pack("!H", 1000), OP_CLOSE, mask=MASK))
      assert ws.recv() is None and ws.closed
      assert read_frame(client_reader)[1] == OP_CLOSE
    finally:
      server_sock.close()
      client_sock.close()
