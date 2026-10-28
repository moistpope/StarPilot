"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

The OSM road tiles the page's map matcher reads (static/js/world/osmtile.js, mapdata.js): the same files
StarPilot's mapd keeps under <mapd root>/offline/<lat>/<lon>/<minLat>_<minLon>_<maxLat>_<maxLon>, one
packed Cap'n Proto message per 0.25 deg tile, in 2 x 2 deg cells named by their south-west corner on the
even grid. The page asks for GET /map/tile/<lat>/<lon>/<name>; a tile that isn't on disk has its cell
downloaded from pfeiferj's server (what mapd does too) and unpacked next to mapd's own, so the two share.

The tiles hold roads only. The point features along them the page draws (traffic signals, stop and
give-way signs, crossings, level crossings, traffic calming; mapfeatures.js) come from the Overpass API
in cells of FEAT_DEG square, GET /map/features/<klat>/<klon> (the cell's south-west corner in units of
FEAT_DEG), fetched once per cell and kept under <mapd root>/features/ as compact JSON.
"""
import json
import math
import os
import re
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware import PC
from openpilot.system.hardware.hw import Paths

CELL_URL = "https://map-data.pfeifer.dev/offline/{lat}/{lon}.tar.gz"
NAME_RE = re.compile(r"^-?\d{1,3}\.\d{6}_-?\d{1,3}\.\d{6}_-?\d{1,3}\.\d{6}_-?\d{1,3}\.\d{6}$")
RETRY_S = 60.0       # after a failed download of a cell
TIMEOUT_S = 120.0


def mapd_root() -> str:
  """Where StarPilot's mapd keeps its OSM data (its offline tiles are starpilot_variables.MAPS_PATH)."""
  return os.path.join(Paths.comma_home(), "media", "0", "osm") if PC else "/data/media/0/osm"


class MapTiles:
  def __init__(self, root: str | None = None, download: bool = True):
    self.root = root or os.path.join(mapd_root(), "offline")
    self.download = download
    self._locks: dict[tuple[int, int], threading.Lock] = {}
    self._failed: dict[tuple[int, int], float] = {}
    self._guard = threading.Lock()

  def tile_path(self, lat: int, lon: int, name: str) -> str | None:
    """The tile's file, downloading its cell first if needed; None when it can't be had."""
    if not NAME_RE.fullmatch(name) or not (-90 <= lat < 90 and -180 <= lon < 180) or lat % 2 or lon % 2:
      raise ValueError("not a tile name")
    path = os.path.join(self.root, str(lat), str(lon), name)
    if os.path.isfile(path):
      return path
    if not self.download:
      return None
    with self._guard:
      lock = self._locks.setdefault((lat, lon), threading.Lock())
    with lock:
      if os.path.isfile(path):   # another request fetched it meanwhile
        return path
      if time.monotonic() - self._failed.get((lat, lon), -1e9) < RETRY_S:
        return None
      try:
        self._fetch_cell(lat, lon)
      except Exception:
        cloudlog.exception(f"webhud: map cell {lat}/{lon} download failed")
        self._failed[(lat, lon)] = time.monotonic()
        return None
    return path if os.path.isfile(path) else None

  def _fetch_cell(self, lat: int, lon: int) -> None:
    dest = os.path.join(self.root, str(lat), str(lon))
    os.makedirs(dest, exist_ok=True)
    url = CELL_URL.format(lat=lat, lon=lon)
    cloudlog.info(f"webhud: downloading map cell {url}")
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=self.root)
    try:
      with os.fdopen(fd, "wb") as f, urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
        while True:
          chunk = r.read(1 << 20)
          if not chunk:
            break
          f.write(chunk)
      prefix = f"offline/{lat}/{lon}/"
      with tarfile.open(tmp, "r:gz") as tar:
        for m in tar.getmembers():
          if not m.isfile() or not m.name.startswith(prefix):
            continue
          name = m.name[len(prefix):]
          if not NAME_RE.fullmatch(name):
            continue
          src = tar.extractfile(m)
          if src is None:
            continue
          out = os.path.join(dest, name + ".part")
          with src, open(out, "wb") as f:
            while True:
              chunk = src.read(1 << 20)
              if not chunk:
                break
              f.write(chunk)
          os.replace(out, os.path.join(dest, name))
      cloudlog.info(f"webhud: map cell {lat}/{lon} ready in {dest}")
    finally:
      try:
        os.remove(tmp)
      except OSError:
        pass


def parse_tile_path(path: str) -> tuple[int, int, str]:
  """'/map/tile/<lat>/<lon>/<name>' -> (lat, lon, name); ValueError when it isn't one."""
  parts = path.split("/")
  if len(parts) != 6 or parts[1] != "map" or parts[2] != "tile":
    raise ValueError("not a tile path")
  return int(parts[3]), int(parts[4]), parts[5]


FEAT_DEG = 0.05            # the cells the page asks for (GET /map/features/<klat>/<klon>)
FEAT_TILE_DEG = 0.25       # ...fetched from Overpass a whole road-tile's worth at a time (one request, 25 cells, kept split)
CELLS_PER_TILE = 5
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 150.0
OVERPASS_TRIES = 3
OVERPASS_RETRY_S = 3.0
FEATURE_QUERY = """[out:json][timeout:120];
(
  node["highway"~"^(traffic_signals|stop|give_way|crossing|mini_roundabout)$"]({s},{w},{n},{e});
  node["railway"="level_crossing"]({s},{w},{n},{e});
  node["traffic_calming"]({s},{w},{n},{e});
);
out body;"""
KEEP_TAGS = ("highway", "railway", "direction", "traffic_signals:direction", "traffic_signals", "crossing", "crossing:markings",
             "stop", "traffic_calming")
USER_AGENT = "starpilot-webhud/1.0 (map features; github.com/moistpope/StarPilot)"


def tile_name(lat: float, lon: float) -> tuple[int, int, str]:
  """The road tile under a point as (cell lat, cell lon, file name), as the page's osmtile.js tileFor names it."""
  t_lat, t_lon = math.floor(lat * 4) / 4, math.floor(lon * 4) / 4
  return math.floor(lat / 2) * 2, math.floor(lon / 2) * 2, f"{t_lat:.6f}_{t_lon:.6f}_{t_lat + 0.25:.6f}_{t_lon + 0.25:.6f}"


class MapFeatures:
  """The point features of FEAT_DEG cells: from Overpass once, a FEAT_TILE_DEG tile at a time, then from disk forever."""

  def __init__(self, root: str | None = None, download: bool = True):
    self.root = root or os.path.join(mapd_root(), "features")
    self.download = download
    self._lock = threading.Lock()
    self._failed: dict[tuple[int, int], float] = {}

  def cell_path(self, klat: int, klon: int) -> str | None:
    """The cell's JSON file, fetching its tile first if needed; None when it can't be had."""
    if not (-90 / FEAT_DEG <= klat < 90 / FEAT_DEG and -180 / FEAT_DEG <= klon < 180 / FEAT_DEG):
      raise ValueError("not a feature cell")
    path = os.path.join(self.root, f"{klat}_{klon}.json")
    if os.path.isfile(path):
      return path
    if not self.download or not self.ensure_tile(klat // CELLS_PER_TILE, klon // CELLS_PER_TILE):
      return None
    return path if os.path.isfile(path) else None

  def _marker(self, tlat: int, tlon: int) -> str:
    return os.path.join(self.root, f"tile_{tlat}_{tlon}.done")

  def have_tile(self, tlat: int, tlon: int) -> bool:
    return os.path.isfile(self._marker(tlat, tlon))

  def ensure_tile(self, tlat: int, tlon: int) -> bool:
    """Fetch the tile's features (all its cells) unless already on disk. True when they are."""
    if self.have_tile(tlat, tlon):
      return True
    if not self.download:
      return False
    with self._lock:
      if self.have_tile(tlat, tlon):
        return True
      if time.monotonic() - self._failed.get((tlat, tlon), -1e9) < RETRY_S:
        return False
      try:
        self._fetch_tile(tlat, tlon)
      except Exception:
        cloudlog.exception(f"webhud: map features tile {tlat}/{tlon} failed")
        self._failed[(tlat, tlon)] = time.monotonic()
        return False
    return True

  def _fetch_tile(self, tlat: int, tlon: int) -> None:
    s, w = tlat * FEAT_TILE_DEG, tlon * FEAT_TILE_DEG
    bbox = {"s": f"{s:.4f}", "w": f"{w:.4f}", "n": f"{s + FEAT_TILE_DEG:.4f}", "e": f"{w + FEAT_TILE_DEG:.4f}"}
    query = FEATURE_QUERY.format(**bbox)
    cloudlog.info(f"webhud: fetching map features {tlat}/{tlon} ({bbox['s']},{bbox['w']})")
    req = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    # a busy Overpass answers 504 (or 429) for a while; a couple of short retries usually get through
    for attempt in range(OVERPASS_TRIES):
      try:
        with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_S) as r:
          data = json.loads(r.read().decode("utf-8"))
        break
      except urllib.error.HTTPError as e:
        if e.code not in (429, 504) or attempt == OVERPASS_TRIES - 1:
          raise
        time.sleep(OVERPASS_RETRY_S)
    # split into the page's cells (a node on the tile's far edge stays in this tile)
    cells: dict[tuple[int, int], list] = {
      (tlat * CELLS_PER_TILE + i, tlon * CELLS_PER_TILE + j): []
      for i in range(CELLS_PER_TILE) for j in range(CELLS_PER_TILE)
    }
    total = 0
    for el in data.get("elements", []):
      if el.get("type") != "node":
        continue
      klat = min(max(math.floor(el["lat"] / FEAT_DEG), tlat * CELLS_PER_TILE), tlat * CELLS_PER_TILE + CELLS_PER_TILE - 1)
      klon = min(max(math.floor(el["lon"] / FEAT_DEG), tlon * CELLS_PER_TILE), tlon * CELLS_PER_TILE + CELLS_PER_TILE - 1)
      tags = {k: v for k, v in (el.get("tags") or {}).items() if k in KEEP_TAGS}
      cells[(klat, klon)].append([el["id"], el["lat"], el["lon"], tags])
      total += 1
    os.makedirs(self.root, exist_ok=True)
    now = int(time.time())  # noqa: TID251 - persisted Unix timestamp, not an elapsed interval
    for (klat, klon), nodes in cells.items():
      cs, cw = klat * FEAT_DEG, klon * FEAT_DEG
      out = {"cell": [klat, klon], "bbox": [cs, cw, cs + FEAT_DEG, cw + FEAT_DEG], "at": now, "nodes": nodes}
      path = os.path.join(self.root, f"{klat}_{klon}.json")
      with open(path + ".part", "w") as f:
        json.dump(out, f, separators=(",", ":"))
      os.replace(path + ".part", path)
    with open(self._marker(tlat, tlon), "w") as f:
      f.write(str(now))
    cloudlog.info(f"webhud: map features tile {tlat}/{tlon}: {total} nodes in {len(cells)} cells")


def parse_features_path(path: str) -> tuple[int, int]:
  """'/map/features/<klat>/<klon>' -> (klat, klon); ValueError when it isn't one."""
  parts = path.split("/")
  if len(parts) != 5 or parts[1] != "map" or parts[2] != "features":
    raise ValueError("not a features path")
  return int(parts[3]), int(parts[4])


# ---- prefetch ---------------------------------------------------------------------------------------------

PREFETCH_MAX_KM = 200.0
PREFETCH_PAUSE_S = 1.0     # between Overpass fetches: polite


def tiles_within(lat: float, lon: float, radius_m: float) -> list[tuple[int, int]]:
  """The FEAT_TILE_DEG (= road tile) indices (tlat, tlon) whose box comes within radius_m of the point, nearest first."""
  m_lat = 111_320.0
  m_lon = m_lat * max(0.05, math.cos(math.radians(lat)))
  d_lat, d_lon = radius_m / m_lat, radius_m / m_lon
  out = []
  for tlat in range(math.floor((lat - d_lat) / FEAT_TILE_DEG), math.floor((lat + d_lat) / FEAT_TILE_DEG) + 1):
    for tlon in range(math.floor((lon - d_lon) / FEAT_TILE_DEG), math.floor((lon + d_lon) / FEAT_TILE_DEG) + 1):
      if not (-90 <= tlat * FEAT_TILE_DEG < 90 and -180 <= tlon * FEAT_TILE_DEG < 180):
        continue
      s, w = tlat * FEAT_TILE_DEG, tlon * FEAT_TILE_DEG
      # the nearest point of the box, in meters
      dy = (max(s, min(lat, s + FEAT_TILE_DEG)) - lat) * m_lat
      dx = (max(w, min(lon, w + FEAT_TILE_DEG)) - lon) * m_lon
      d = math.hypot(dx, dy)
      if d <= radius_m:
        out.append((d, tlat, tlon))
  out.sort()
  return [(tlat, tlon) for _, tlat, tlon in out]


class MapPrefetch:
  """Downloads every road tile and feature tile within a radius of a point, in the background, once asked
  (POST /map/prefetch from the page as the car moves). Nothing fetched is ever dropped: the next drive
  through the area needs no network."""

  def __init__(self, tiles: MapTiles, features: MapFeatures):
    self.tiles = tiles
    self.features = features
    self._lock = threading.Lock()
    self._wake = threading.Event()
    self._want: tuple[float, float, float] | None = None
    self._status: dict = {"center": None, "radius_km": None, "tiles": 0, "roads_ready": 0, "features_ready": 0,
                          "busy": False, "errors": 0, "updated": 0}
    self._thread: threading.Thread | None = None

  def request(self, lat: float, lon: float, radius_km: float) -> dict:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
      raise ValueError("bad position")
    radius_km = float(radius_km)
    if not math.isfinite(radius_km):
      raise ValueError("bad radius")
    radius_km = max(0.0, min(PREFETCH_MAX_KM, radius_km))
    with self._lock:
      self._want = (lat, lon, radius_km)
      self._status.update(center=[round(lat, 5), round(lon, 5)], radius_km=radius_km)
      if self._thread is None:
        self._thread = threading.Thread(target=self._run, name="webhud-map-prefetch", daemon=True)
        self._thread.start()
    self._wake.set()
    return self.status()

  def status(self) -> dict:
    with self._lock:
      st = dict(self._status)
      want = self._want
    if want is not None:
      tiles = tiles_within(want[0], want[1], want[2] * 1000.0)
      st["tiles"] = len(tiles)
      st["roads_ready"] = sum(1 for t in tiles if os.path.isfile(os.path.join(self.tiles.root, *self._road(t))))
      st["features_ready"] = sum(1 for t in tiles if self.features.have_tile(*t))
    return st

  @staticmethod
  def _road(tile: tuple[int, int]) -> tuple[str, str, str]:
    lat, lon = tile[0] * FEAT_TILE_DEG + 0.01, tile[1] * FEAT_TILE_DEG + 0.01
    c_lat, c_lon, name = tile_name(lat, lon)
    return str(c_lat), str(c_lon), name

  def _run(self) -> None:
    while True:
      self._wake.wait()
      self._wake.clear()
      with self._lock:
        want = self._want
        self._status["busy"] = True
      if want is None:
        continue
      lat, lon, radius_km = want
      try:
        for tile in tiles_within(lat, lon, radius_km * 1000.0):
          if self._want != want:   # a newer position: start over from there
            break
          c_lat, c_lon, name = self._road(tile)
          try:
            self.tiles.tile_path(int(c_lat), int(c_lon), name)   # downloads the 2 deg cell when missing
          except Exception:
            cloudlog.exception(f"webhud: prefetch road tile {name}")
            with self._lock:
              self._status["errors"] += 1
          if not self.features.have_tile(*tile):
            if not self.features.ensure_tile(*tile):
              with self._lock:
                self._status["errors"] += 1
            time.sleep(PREFETCH_PAUSE_S)
      finally:
        with self._lock:
          self._status["busy"] = False
          self._status["updated"] = int(time.time())  # noqa: TID251 - Unix timestamp displayed by the client
      if self._want != want:
        self._wake.set()
