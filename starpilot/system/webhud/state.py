"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

StreamBuilder collects what the sources read since the last tick -- the CAN frames the HUD decodes and
the openpilot service extracts -- for the bridge to stream to the page, which does the decoding and the
world-model fusion itself (static/js/world/). Live and replay sources feed it identically; only the
clock differs. It also keeps the latest frame of each message, so a page that connects mid-drive
starts from the current state and the CAN overrides page can show what the ICC is sending now.

Tick shape (JSON, the page's worker takes it apart):
  {now, reset?, brand, can: [[t, [[addr, bus, hex], ...]], ...], op: [[service, t, extract], ...]}
"""
import os

from openpilot.starpilot.system.webhud.dbc import DBC
from openpilot.starpilot.system.webhud.extract import EXTRACTORS, OP_NAMES
from openpilot.starpilot.system.webhud.paths import DBC_PATH, RADAR_DBC_PATH

BUS_PT = 0       # vehicle side: gateway-mirrored body/HMI/chassis
BUS_RADAR = 1    # the mid-range radar's private CAN
BUS_CAM = 2      # OEM ADAS module side
RADAR_SLOTS = frozenset(range(0x310, 0x330))   # radar object slots; an unused one carries ID 0 in bytes 3-4
MERGE_LIMIT = 60   # ticks a slow client may fall behind before it's started over


class StreamBuilder:
  def __init__(self, dbc: DBC | None = None, radar_dbc: DBC | None = None):
    self.dbc = dbc or DBC(DBC_PATH)
    if radar_dbc is None and os.path.isfile(RADAR_DBC_PATH):
      radar_dbc = DBC(RADAR_DBC_PATH)
    self.radar_dbc = radar_dbc
    # which frames the page decodes: the ADASBUS subset on the vehicle and cam buses, the radar's on bus 1
    self.wanted = frozenset(self.dbc.messages)
    self.radar_wanted = frozenset(radar_dbc.messages) if radar_dbc is not None else frozenset()
    self.native_bus = {addr: (BUS_CAM if m.transmitter == "ADAS" else BUS_PT) for addr, m in self.dbc.messages.items()}
    self.services: dict[str, dict] = {}
    self.service_t: dict[str, float] = {}
    self.latest: dict[tuple[int, int], tuple[bytes, float]] = {}   # (addr, bus) -> (data, t)
    self.brand: str | None = None
    self.t = 0.0
    self.can_batches: list = []
    self.op_updates: list = []
    self.pending_reset = False

  def reset(self) -> None:
    self.services.clear()
    self.service_t.clear()
    self.latest.clear()
    self.can_batches = []
    self.op_updates = []
    self.pending_reset = True
    self.t = 0.0

  @property
  def fisker(self) -> bool:
    # an unknown brand still parses, so logs/benches without carParams work
    return self.brand in (None, "fisker")

  def _keep(self, addr: int, data: bytes, src: int) -> bool:
    if src in (BUS_PT, BUS_CAM):
      return addr in self.wanted
    if src == BUS_RADAR and addr in self.radar_wanted:
      # an unused radar slot (ID 0) is nothing to decode: most of the 32 are, every cycle
      return addr not in RADAR_SLOTS or bool(data[3] or data[4])
    return False   # 128+ are TX echoes / blocked frames

  def feed_can(self, frames, t: float) -> None:
    """frames: iterable of (address, data, src) as produced by can_capnp_to_list."""
    self.t = max(self.t, t)
    if not self.fisker:
      return
    kept = []
    latest = self.latest
    for addr, data, src in frames:
      if self._keep(addr, data, src):
        kept.append([addr, src, data.hex()])
        latest[(addr, src)] = (bytes(data), t)
    if kept:
      self.can_batches.append([round(t, 6), kept])

  def feed_service(self, which: str, msg, t: float) -> None:
    extractor = EXTRACTORS.get(which)
    if extractor is not None:
      self.feed_extract(OP_NAMES.get(which, which), extractor(msg), t)

  def feed_extract(self, which: str, data: dict, t: float) -> None:
    """A service already in its compact form (the demo drive makes them directly)."""
    self.services[which] = data
    self.service_t[which] = t
    self.t = max(self.t, t)
    self.op_updates.append([which, round(t, 6), data])
    if which == "carParams":
      self.brand = data.get("brand") or self.brand

  def set_brand(self, brand: str | None) -> None:
    self.brand = brand or None

  def decoded(self, addr: int) -> dict[str, float] | None:
    """The latest frame of an ADASBUS message, decoded (the native bus first), for the overrides page."""
    nb = self.native_bus.get(addr)
    if nb is None:
      return None
    entry = self.latest.get((addr, nb)) or self.latest.get((addr, BUS_PT if nb == BUS_CAM else BUS_CAM))
    return self.dbc.messages[addr].decode(entry[0]) if entry is not None else None

  def take_tick(self, now: float) -> dict:
    """Everything fed since the last tick, and clear it."""
    tick = {"now": round(now, 6), "brand": self.brand, "can": self.can_batches, "op": self.op_updates}
    if self.pending_reset:
      tick["reset"] = True
    self.can_batches, self.op_updates, self.pending_reset = [], [], False
    return tick

  def snapshot_tick(self, now: float) -> dict:
    """The current state for a client that just connected: a reset, then the latest frame of every message
    and every service, so its page starts where the drive is instead of waiting for each message to repeat."""
    by_t: dict[float, list] = {}
    for (addr, src), (data, t) in self.latest.items():
      by_t.setdefault(round(t, 6), []).append([addr, src, data.hex()])
    return {
      "now": round(now, 6), "reset": True, "brand": self.brand,
      "can": [[t, frames] for t, frames in sorted(by_t.items())],
      "op": [[which, round(self.service_t[which], 6), data] for which, data in self.services.items()],
    }


def merge_ticks(older: dict, newer: dict) -> dict:
  """Two ticks a slow client hasn't taken yet, as one: nothing is lost, and the page sees the newer clock.
  A reset in the newer one, or a backlog past MERGE_LIMIT, drops the older frames (the page starts over)."""
  if newer.get("reset") or older.get("_ticks", 1) >= MERGE_LIMIT:
    merged = dict(newer)
    merged["reset"] = True
    merged.pop("_ticks", None)
    return merged
  merged = dict(newer)
  merged["can"] = older["can"] + newer["can"]
  merged["op"] = older["op"] + newer["op"]
  if older.get("reset"):
    merged["reset"] = True
  merged["_ticks"] = older.get("_ticks", 1) + 1
  return merged
