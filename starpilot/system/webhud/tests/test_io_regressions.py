"""Regression coverage for bounded inputs and interrupted WebHUD I/O."""
import io
import math
import os
import tempfile
from types import SimpleNamespace

import pytest

from openpilot.starpilot.system.webhud import server, websocket
from openpilot.starpilot.system.webhud.maptiles import MapFeatures, MapPrefetch, MapTiles
from openpilot.starpilot.system.webhud.websocket import WebSocketClosed, encode_frame, read_frame


class ShortReader(io.BytesIO):
  def read(self, n=-1):
    return super().read(min(n, 1))


class TestIORegressions:
  def test_websocket_short_reads(self):
    payload = b"payload" * 100
    frame = encode_frame(payload, mask=b"abcd")
    assert read_frame(ShortReader(frame))[2] == payload
    with pytest.raises(WebSocketClosed):
      read_frame(ShortReader(frame[:-1]))

  def test_fragmented_websocket_message_limit(self, mocker, monkeypatch):
    first = bytearray(encode_frame(b"12345", mask=b"abcd"))
    first[0] &= 0x7F
    last = encode_frame(b"6789", opcode=websocket.OP_CONT, mask=b"abcd")
    sock = mocker.Mock()
    ws = websocket.WebSocket(sock, io.BytesIO(first + last))
    with monkeypatch.context() as m:
      m.setattr(websocket, "MAX_MESSAGE", 8)
      assert ws.recv() is None
    assert ws.closed
    sock.shutdown.assert_called_once()

  def test_negative_http_length_rejected_before_read(self, mocker):
    handler = SimpleNamespace(headers={"Content-Length": "-1"}, rfile=mocker.Mock())
    with pytest.raises(ValueError):
      server.HudHandler._body(handler)
    handler.rfile.read.assert_not_called()

  def test_upload_collision_and_truncation(self, mocker, monkeypatch):
    with tempfile.TemporaryDirectory() as root:
      monkeypatch.setattr(server, "data_roots", lambda: (root, root))
      def upload(data, length):
        handler = SimpleNamespace(headers={"Content-Length": str(length)}, rfile=io.BytesIO(data),
                                  server=SimpleNamespace(engine=mocker.Mock()), _json=mocker.Mock())
        server.api_upload(handler, {})
        return handler
      with monkeypatch.context() as m:
        m.setattr(server.time, "strftime", lambda *args: "fixed-second")
        first, second = upload(b"first", 5), upload(b"second", 6)
      routes = [h._json.call_args.args[0]["route"] for h in (first, second)]
      assert routes[0] != routes[1]
      for route, expected in zip(routes, (b"first", b"second"), strict=True):
        with open(os.path.join(root, route + "--0", "rlog"), "rb") as f:
          assert f.read() == expected
      before = set(os.listdir(root))
      with pytest.raises(ValueError, match="incomplete upload"):
        upload(b"partial", 100)
      assert set(os.listdir(root)) == before

  def test_tile_name_rejects_trailing_newline(self):
    with tempfile.TemporaryDirectory() as root:
      tiles = MapTiles(root, download=False)
      name = "50.000000_0.000000_50.250000_0.250000"
      assert tiles.tile_path(50, 0, name) is None
      with pytest.raises(ValueError):
        tiles.tile_path(50, 0, name + "\n")

  def test_nonfinite_prefetch_radius_does_not_start_worker(self):
    with tempfile.TemporaryDirectory() as root:
      prefetch = MapPrefetch(MapTiles(root, download=False), MapFeatures(root, download=False))
      for radius in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
          prefetch.request(51.5, -0.1, radius)
      assert prefetch._thread is None
