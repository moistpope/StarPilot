"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import tempfile
import time

import zstandard

from cereal import log
from openpilot.starpilot.system.webhud.dbc import DBC
from openpilot.starpilot.system.webhud.paths import DBC_PATH
from openpilot.starpilot.system.webhud.sources import ReplaySource, list_routes
from openpilot.starpilot.system.webhud.state import StreamBuilder

SEG_S = 3.0   # short synthetic segments; the replay timeline still spaces them 60 s apart


def make_segment(path: str, seg: int, t0: float) -> None:
  acc = DBC(DBC_PATH).by_name["ADAS_0x31C"]
  events = []
  cp = log.Event.new_message(logMonoTime=int(t0 * 1e9))
  cp.init("carParams").brand = "fisker"
  events.append(cp)
  for i in range(int(SEG_S * 100)):
    t = t0 + i * 0.01
    cs = log.Event.new_message(logMonoTime=int(t * 1e9))
    cs.init("carState").vEgo = seg * 10 + i * 0.01
    events.append(cs)
    can = log.Event.new_message(logMonoTime=int(t * 1e9))
    frames = can.init("can", 1)
    frames[0].address = acc.address
    frames[0].dat = acc.encode({"ADAS_AccTrgSpdDisp": 40 + seg})
    frames[0].src = 2
    events.append(can)
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "wb") as f:
    f.write(zstandard.ZstdCompressor().compress(b"".join(e.to_bytes() for e in events)))


def wait_for(cond, timeout=10.0):
  end = time.monotonic() + timeout
  while time.monotonic() < end:
    if cond():
      return True
    time.sleep(0.02)
  return False


class TestReplay:
  def setup_method(self):
    self.tmp = tempfile.TemporaryDirectory()
    root = self.tmp.name
    for seg in (0, 1):
      make_segment(os.path.join(root, f"0000002a--abcdef1234--{seg}", "rlog.zst"), seg, 100.0 + seg * 60)
    os.makedirs(os.path.join(root, "not-a-route"))
    os.makedirs(os.path.join(root, "0000002b--ffff--0"))   # segment dir without logs
    self.root = root

  def teardown_method(self):
    self.tmp.cleanup()

  def test_list_routes(self):
    routes = list_routes([self.root, os.path.join(self.root, "missing")])
    assert [r["name"] for r in routes] == ["0000002a--abcdef1234"]
    assert [s["n"] for s in routes[0]["segments"]] == [0, 1]
    assert routes[0]["segments"][0]["kind"] == "rlog"

  def test_seek_and_play_across_segments(self):
    route = list_routes([self.root])[0]
    builder = StreamBuilder()
    acc = builder.dbc.by_name["ADAS_0x31C"]

    def set_speed(tick):
      """The ACC set speed in the newest 0x31C frame of a tick (what the page decodes)."""
      frames = [f for _, batch in tick["can"] for f in batch if f[0] == acc.address]
      return acc.decode(bytes.fromhex(frames[-1][2]))["ADAS_AccTrgSpdDisp"]

    replay = ReplaySource(builder, route["name"], route["segments"])
    replay.seek(1.5)
    assert wait_for(lambda: (replay.tick(0.0), replay.pending_seek is None)[1])
    assert builder.brand == "fisker"
    tick = builder.take_tick(replay.now)
    assert tick["reset"] and tick["brand"] == "fisker"
    assert abs(builder.services["carState"]["vEgo"] - 1.5) < 0.02
    assert set_speed(tick) == 40
    # a viewer that connects now gets the same state, from the latest frames
    snap = builder.snapshot_tick(replay.now)
    assert snap["reset"] and set_speed(snap) == 40 and "carState" in [w for w, _, _ in snap["op"]]

    # play past the end of segment 0: the next segment is prefetched and playback continues there
    replay.playing = True
    replay.speed = 4.0
    assert wait_for(lambda: (replay.tick(0.1), replay.cur == 1)[1])
    assert wait_for(lambda: (replay.tick(0.1), replay.t > 60.5)[1])
    tick = builder.take_tick(replay.now)
    assert builder.services["carState"]["vEgo"] >= 10
    assert set_speed(tick) == 41

    # runs to the end and stops
    assert wait_for(lambda: (replay.tick(0.5), not replay.playing)[1])
    assert abs(replay.t - replay.duration) < 0.05

    # seeking back rebuilds state from that point
    replay.seek(0.2)
    assert wait_for(lambda: (replay.tick(0.0), replay.pending_seek is None)[1])
    assert builder.take_tick(replay.now)["reset"] and builder.services["carState"]["vEgo"] < 0.3
