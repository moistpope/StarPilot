"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Data sources for the web HUD: the live cereal bus, or a recorded route replayed from rlog/qlog files.
Both feed a StreamBuilder through the same calls, so everything downstream is identical.
"""
import bisect
import bz2
import os
import re
import threading
import time
from dataclasses import dataclass, field

from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.system.webhud.extract import SERVICES
from openpilot.starpilot.system.webhud.state import StreamBuilder

SEGMENT_S = 60.0
SEGMENT_DIR_RE = re.compile(r"^(?P<route>.+)--(?P<seg>\d+)$")
LOG_NAMES = ("rlog.zst", "rlog.bz2", "rlog", "qlog.zst", "qlog.bz2", "qlog")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
REPLAY_SERVICES = frozenset(SERVICES) | {"can"}


# ---- route discovery --------------------------------------------------------------------------

def find_log(seg_dir: str) -> str | None:
  for name in LOG_NAMES:
    p = os.path.join(seg_dir, name)
    if os.path.isfile(p):
      return p
  return None


def list_routes(roots: list[str]) -> list[dict]:
  """Routes found under the given realdata-style roots, newest first."""
  routes: dict[str, dict] = {}
  for root in roots:
    try:
      entries = list(os.scandir(root))
    except OSError:
      continue
    for entry in entries:
      m = SEGMENT_DIR_RE.match(entry.name)
      if not m or not entry.is_dir():
        continue
      log_path = find_log(entry.path)
      if log_path is None:
        continue
      st = os.stat(log_path)
      route = routes.setdefault(m.group("route"), {"name": m.group("route"), "segments": [], "mtime": 0.0, "size": 0})
      route["segments"].append({
        "n": int(m.group("seg")), "path": log_path, "kind": os.path.basename(log_path).split(".")[0], "size": st.st_size,
      })
      route["mtime"] = max(route["mtime"], st.st_mtime)
      route["size"] += st.st_size
  out = []
  for r in routes.values():
    r["segments"].sort(key=lambda s: s["n"])
    out.append(r)
  out.sort(key=lambda r: r["mtime"], reverse=True)
  return out


def read_log_bytes(path: str) -> bytes:
  with open(path, "rb") as f:
    data = f.read()
  if data[:4] == ZSTD_MAGIC:
    import zstandard
    with zstandard.ZstdDecompressor().stream_reader(data) as reader:
      return reader.read()
  if data[:3] == b"BZh":
    return bz2.decompress(data)
  return data


# ---- replay -------------------------------------------------------------------------------------

@dataclass
class Segment:
  n: int
  path: str
  times: list[float] = field(default_factory=list)   # seconds, log monotonic clock
  kinds: list[str] = field(default_factory=list)
  events: list = field(default_factory=list)
  brand: str | None = None

  @property
  def t0(self) -> float:
    return self.times[0] if self.times else 0.0

  @property
  def duration(self) -> float:
    return (self.times[-1] - self.times[0]) if self.times else 0.0


def load_segment(n: int, path: str) -> Segment:
  from cereal import log
  data = read_log_bytes(path)
  seg = Segment(n, path)
  # readers reference `data`; keeping them keeps the decompressed buffer alive for this segment
  for evt in log.Event.read_multiple_bytes(data):
    try:
      which = evt.which()
    except Exception:  # union member unknown to this schema version
      continue
    if which not in REPLAY_SERVICES:
      continue
    seg.times.append(evt.logMonoTime * 1e-9)
    seg.kinds.append(which)
    seg.events.append(evt)
    if which == "carParams" and seg.brand is None:
      seg.brand = evt.carParams.brand or None
  return seg


class ReplaySource:
  """Plays a route at `speed`, one segment in memory at a time (plus a prefetched next one)."""
  WARMUP_S = 3.0      # history replayed after a seek so slow messages are populated
  PREFETCH_S = 15.0
  CACHE_SEGMENTS = 3

  def __init__(self, builder: StreamBuilder, name: str, segments: list[dict]):
    if not segments:
      raise ValueError("route has no segments")
    self.builder = builder
    self.name = name
    self.segments = {s["n"]: s["path"] for s in segments}
    self.order = sorted(self.segments)
    self.first = self.order[0]
    self.cache: dict[int, Segment] = {}
    self.loading: set[int] = set()
    self.errors: dict[int, str] = {}
    self.lock = threading.Lock()
    self.brand: str | None = None
    self.t = 0.0
    self.speed = 1.0
    self.playing = False
    self.cur: int | None = None
    self.idx = 0
    self.pending_seek: float | None = 0.0

  # timeline -----------------------------------------------------------------
  def offset(self, n: int) -> float:
    return (n - self.first) * SEGMENT_S

  @property
  def duration(self) -> float:
    last = self.order[-1]
    seg = self.cache.get(last)
    return self.offset(last) + (seg.duration if seg else SEGMENT_S)

  def seg_for_time(self, t: float) -> int:
    n = self.first + int(max(t, 0.0) // SEGMENT_S)
    i = bisect.bisect_right(self.order, n) - 1
    return self.order[max(i, 0)]

  # loading ------------------------------------------------------------------
  def _ensure(self, n: int) -> Segment | None:
    with self.lock:
      seg = self.cache.get(n)
      if seg is not None or n in self.loading or n in self.errors:
        return seg
      self.loading.add(n)
    threading.Thread(target=self._load, args=(n,), name=f"webhud-load-{n}", daemon=True).start()
    return None

  def _load(self, n: int) -> None:
    try:
      t0 = time.monotonic()
      seg = load_segment(n, self.segments[n])
      cloudlog.info(f"webhud replay: loaded {self.name}--{n} ({len(seg.events)} events) in {time.monotonic() - t0:.1f}s")
      with self.lock:
        self.cache[n] = seg
        if seg.brand and self.brand is None:
          self.brand = seg.brand
        # keep the current, next and most recently used segments
        while len(self.cache) > self.CACHE_SEGMENTS:
          victim = next((k for k in self.cache if k not in (n, self.cur)), None)
          if victim is None:
            break
          del self.cache[victim]
    except Exception as e:
      cloudlog.exception(f"webhud replay: failed to load segment {n}")
      with self.lock:
        self.errors[n] = f"{type(e).__name__}: {e}"
    finally:
      with self.lock:
        self.loading.discard(n)

  # control ------------------------------------------------------------------
  def seek(self, t: float) -> None:
    self.pending_seek = min(max(t, 0.0), self.duration)

  def set_speed(self, speed: float) -> None:
    self.speed = min(max(speed, 0.1), 16.0)

  def _do_seek(self, t: float) -> bool:
    n = self.seg_for_time(t)
    seg = self._ensure(n)
    if seg is None:
      return False
    rel = t - self.offset(n)
    self.builder.reset()
    self.builder.set_brand(self.brand or seg.brand)
    i0 = bisect.bisect_left(seg.times, seg.t0 + rel - self.WARMUP_S)
    i1 = bisect.bisect_right(seg.times, seg.t0 + rel)
    self._process(seg, i0, i1)
    self.cur, self.idx, self.t = n, i1, t
    return True

  def _process(self, seg: Segment, i0: int, i1: int) -> None:
    latest: dict[str, int] = {}
    feed_can = self.builder.feed_can
    for i in range(i0, i1):
      kind = seg.kinds[i]
      if kind == "can":
        feed_can([(m.address, m.dat, m.src) for m in seg.events[i].can], seg.times[i])
      else:
        latest[kind] = i  # only the newest of each service matters for display
    for kind, i in latest.items():
      self.builder.feed_service(kind, getattr(seg.events[i], kind), seg.times[i])

  def tick(self, dt: float) -> None:
    if self.pending_seek is not None:
      if not self._do_seek(self.pending_seek):
        return
      self.pending_seek = None
    if self.cur is None:
      return
    seg = self.cache.get(self.cur)
    if seg is None:
      self.pending_seek = self.t
      return

    if self.playing:
      target = min(self.t + dt * self.speed, self.duration)
      rel_end = seg.t0 + (target - self.offset(self.cur))
      i1 = bisect.bisect_right(seg.times, rel_end)
      self._process(seg, self.idx, i1)
      self.idx = i1
      self.t = target
      if i1 >= len(seg.times):
        nxt = next((k for k in self.order if k > self.cur), None)
        if nxt is None:
          self.playing = False
        else:
          nseg = self._ensure(nxt)
          if nseg is not None:
            self.cur, self.idx = nxt, 0
            self.t = max(self.t, self.offset(nxt))
          # else: hold at the end of this segment until the next one is loaded

    # prefetch the next segment ahead of time
    if self.offset(self.cur) + seg.duration - self.t < self.PREFETCH_S:
      nxt = next((k for k in self.order if k > self.cur), None)
      if nxt is not None:
        self._ensure(nxt)

  @property
  def now(self) -> float:
    """Current position on the log's monotonic clock (what StreamBuilder timestamps use)."""
    seg = self.cache.get(self.cur) if self.cur is not None else None
    if seg is None:
      return self.builder.t
    return seg.t0 + (self.t - self.offset(seg.n))

  def status(self) -> dict:
    return {
      "route": self.name, "segments": self.order, "segment": self.cur, "t": round(self.t, 2),
      "duration": round(self.duration, 2), "playing": self.playing, "speed": self.speed,
      "loading": bool(self.loading) or self.pending_seek is not None, "errors": self.errors,
    }


# ---- live ---------------------------------------------------------------------------------------

class LiveSource:
  def __init__(self, builder: StreamBuilder):
    self.builder = builder
    self.sm = None
    self.can_sock = None

  def start(self) -> None:
    import cereal.messaging as messaging
    self.sm = messaging.SubMaster(SERVICES)
    self.can_sock = messaging.sub_sock("can", timeout=0)
    self.builder.reset()
    self.builder.set_brand(_brand_from_params())

  def stop(self) -> None:
    self.sm = None
    self.can_sock = None

  @property
  def running(self) -> bool:
    return self.sm is not None

  def tick(self) -> None:
    import cereal.messaging as messaging
    from openpilot.selfdrive.pandad import can_capnp_to_list
    assert self.sm is not None
    raw = messaging.drain_sock_raw(self.can_sock, wait_for_one=False)
    if raw:
      for nanos, frames in can_capnp_to_list(raw):
        self.builder.feed_can(frames, nanos * 1e-9)
    self.sm.update(0)
    for s in SERVICES:
      if self.sm.updated[s]:
        self.builder.feed_service(s, self.sm[s], self.sm.logMonoTime[s] * 1e-9)

  @property
  def now(self) -> float:
    return time.monotonic()


def _brand_from_params() -> str | None:
  try:
    from opendbc.car.structs import car
    from openpilot.common.params import Params
    raw = Params().get("CarParamsPersistent")
    if raw:
      with car.CarParams.from_bytes(raw) as cp:
        return cp.brand or None
  except Exception:
    cloudlog.exception("webhud: could not read CarParamsPersistent")
  return None
