#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

StarPilot web HUD bridge: the comma's half of the Fisker Ocean HUD, Oceanfront View
(https://github.com/moistpope/oceanfront-view).

  http://starpilot.local:8088   (also :80 when the process may bind it)

A bridge: it streams what it reads over a WebSocket at 20 Hz -- the ADASBUS and radar frames the page
decodes and the openpilot services, in compact form -- replays recorded routes, and exposes the
editable CAN overrides and params. The decoding and the world model run in the page, which the
Oceanfront View Android app carries onto the car's own screen (relaying /api and /ws here), so this
process stays light. It serves no page of its own: "/" says where the page is, unless WEBHUD_PAGE_DIR
names an Oceanfront View checkout, which it then serves for a plain browser on a PC. Stdlib only
(http.server), like webrtcd: the AGNOS venv has no aiohttp.

Dev on a PC:  python -m openpilot.starpilot.system.webhud.server --replay /path/to/rlog.zst
"""
import argparse
import base64
import binascii
import gzip
import hashlib
import ipaddress
import json
import mimetypes
import os
import queue
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.car.can_overrides import OverrideStore
from openpilot.starpilot.system.webhud.demo import DemoSource
from openpilot.starpilot.system.webhud.mdns import MdnsPublisher, ipv4_interfaces
from openpilot.starpilot.system.webhud.paths import STATIC_DIR, STATIC_FILES, STATIC_ROOTS
from openpilot.starpilot.system.webhud.sources import LiveSource, ReplaySource, list_routes
from openpilot.starpilot.system.webhud.state import StreamBuilder, merge_ticks
from openpilot.starpilot.system.webhud.websocket import WebSocket, accept_key
from openpilot.starpilot.system.webhud.maptiles import MapFeatures, MapPrefetch, MapTiles, parse_features_path, parse_tile_path

VERSION = "1.0"
LANDING = f"""StarPilot web HUD bridge {VERSION}

This process streams the car's buses and openpilot's services to the HUD page. The page itself is the
Oceanfront View app (https://github.com/moistpope/oceanfront-view): on the car's screen run that app; on a
PC run `python3 serve.py --bridge <this host>` from its checkout and open http://localhost:18088, or set
WEBHUD_PAGE_DIR to that checkout and this process serves the page too.

Endpoints: /api/status, /api/routes, /api/replay, /api/upload, /api/params, /api/overrides, /api/ssh, /map/...,
/dbc/fisker_ocean_adas_world.dbc, /dbc/fisker_ocean_mrr.dbc, and the WebSocket at /ws.
"""
DEFAULT_PORT = int(os.getenv("WEBHUD_PORT", "8088"))
DEFAULT_HOSTNAME = os.getenv("WEBHUD_HOSTNAME", "starpilot")
RATE_HZ = 20
IDLE_STOP_S = 15.0          # stop reading the bus this long after the last client leaves
MOVING_SPEED = 1.0          # m/s: above this, settings writes are refused and replay yields to live
MAX_UPLOAD = 256 * 1024 * 1024
MAX_UPLOADS_KEPT = 5
GZIP_TYPES = ("text/", "application/javascript", "application/json", "image/svg+xml", "model/gltf-binary")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/plain", ".dbc")   # text, so the DBCs gzip on their way to the page (the app serves them so too)

# params the UI may read and write: key -> (type, validator)
PARAM_ALLOWLIST: dict[str, tuple[type, Callable[[int], bool] | None]] = {
  "LongitudinalPersonality": (int, lambda v: v in (0, 1, 2)),
  "ExperimentalMode": (bool, None),
  "IsMetric": (bool, None),
  # No FiskerACCAutoSpeed/FiskerACCTerrain: StarPilot ships prebuilt and can't add params. The 0x52A spoof's
  # baseline for those two is FiskerFlagsSP (auto speed on, terrain off); an ICC_0x52A override wins over it.
}


# ---- params (real on device, JSON file on a PC without the native lib) -------------------------

class _DevParams:
  def __init__(self, path: str):
    self.path = path
    self.lock = threading.Lock()

  def _load(self) -> dict:
    try:
      with open(self.path) as f:
        return json.load(f)
    except (OSError, ValueError):
      return {}

  def get(self, key, return_default=False):
    return self._load().get(key)

  def get_bool(self, key):
    return bool(self._load().get(key, False))

  def put(self, key, value):
    with self.lock:
      d = self._load()
      d[key] = value
      os.makedirs(os.path.dirname(self.path), exist_ok=True)
      with open(self.path, "w") as f:
        json.dump(d, f)

  def put_bool(self, key, value):
    self.put(key, bool(value))

  def remove(self, key):
    with self.lock:
      d = self._load()
      d.pop(key, None)
      with open(self.path, "w") as f:
        json.dump(d, f)


def open_params():
  try:
    from openpilot.common.params import Params
    return Params()
  except Exception:
    from openpilot.system.hardware.hw import Paths
    path = os.path.join(Paths.comma_home(), "webhud_dev_params.json")
    cloudlog.warning(f"webhud: native params unavailable, using {path}")
    return _DevParams(path)


def data_roots() -> tuple[list[str], str]:
  from openpilot.system.hardware.hw import Paths
  log_root = Paths.log_root().rstrip("/")
  upload_dir = os.path.join(os.path.dirname(log_root), "webhud_uploads")
  return [log_root, upload_dir], upload_dir


# ---- clients & engine -----------------------------------------------------------------------------

class Client:
  """One WebSocket viewer. A sender thread delivers the ticks, merging the ones a slow tablet hasn't
  taken yet into one, so it never stalls the engine or other viewers and loses nothing."""

  def __init__(self, ws: WebSocket, peer: str):
    self.ws = ws
    self.peer = peer
    self._pending: dict | None = None
    self._extra: queue.SimpleQueue[str] = queue.SimpleQueue()
    self._cv = threading.Condition()
    self.alive = True
    threading.Thread(target=self._sender, name="webhud-send", daemon=True).start()

  def push_tick(self, tick: dict) -> None:
    with self._cv:
      self._pending = tick if self._pending is None else merge_ticks(self._pending, tick)
      self._cv.notify()

  def push(self, text: str) -> None:
    """Messages that must not be coalesced (replies, notices)."""
    self._extra.put(text)
    with self._cv:
      self._cv.notify()

  def _sender(self) -> None:
    while self.alive:
      with self._cv:
        while self.alive and self._pending is None and self._extra.empty():
          self._cv.wait(1.0)
        tick, self._pending = self._pending, None
      try:
        while not self._extra.empty():
          self.ws.send_text(self._extra.get_nowait())
        if tick is not None:
          tick.pop("_ticks", None)
          self.ws.send_text(json.dumps({"type": "tick", "data": tick}, separators=(",", ":")))
      except Exception:
        self.close()

  def close(self) -> None:
    self.alive = False
    with self._cv:
      self._cv.notify()
    self.ws.close()


class Engine:
  def __init__(self, params, replay_path: str | None = None, demo: bool = False):
    self.params = params
    self.builder = StreamBuilder()
    self.live = LiveSource(self.builder)
    self.live_error: str | None = None
    self.replay: ReplaySource | DemoSource | None = None
    self.mode = "live"
    self.notice: str | None = None
    self.clients: set[Client] = set()
    self.clients_lock = threading.Lock()
    self.commands: queue.SimpleQueue = queue.SimpleQueue()
    self.last_client_t = 0.0
    self.monitor = _VehicleMonitor()
    self.stop_event = threading.Event()
    if replay_path:
      self.commands.put(("load_path", replay_path))
    elif demo:
      self.commands.put(("demo",))

  # client registry ----------------------------------------------------------
  def add_client(self, c: Client) -> None:
    with self.clients_lock:
      self.clients.add(c)
    self.last_client_t = time.monotonic()

  def remove_client(self, c: Client) -> None:
    with self.clients_lock:
      self.clients.discard(c)
    self.last_client_t = time.monotonic()

  # commands (from HTTP/WS threads; executed on the engine thread) ------------
  def command(self, *cmd) -> None:
    self.commands.put(cmd)

  def _handle(self, cmd: tuple) -> None:
    kind = cmd[0]
    if kind == "load_route":
      _, name, segments, start = cmd
      if self.monitor.moving:
        self.notice = "Replay is unavailable while the car is moving"
        return
      self.replay = ReplaySource(self.builder, name, segments)
      self.replay.seek(start)
      self.replay.playing = True
      self.mode = "replay"
      self.live.stop()
    elif kind == "load_path":
      path = cmd[1]
      if os.path.isdir(path):
        routes = list_routes([path])
        if routes:
          self._handle(("load_route", routes[0]["name"], routes[0]["segments"], 0.0))
      elif os.path.isfile(path):
        self._handle(("load_route", os.path.basename(path), [{"n": 0, "path": path}], 0.0))
    elif kind == "demo":
      self.replay = DemoSource(self.builder)
      self.mode = "replay"
      self.live.stop()
    elif kind == "live":
      self.replay = None
      self.mode = "live"
      self.builder.reset()
    elif kind == "snapshot":
      # a viewer that just connected starts from the current state (on the engine thread: the builder's)
      tick = self.builder.snapshot_tick(self._clock())
      self._decorate(tick, 1)
      cmd[1].push(json.dumps({"type": "tick", "data": tick}, separators=(",", ":")))
    elif self.replay is not None:
      if kind == "play":
        self.replay.playing = True
      elif kind == "pause":
        self.replay.playing = False
      elif kind == "toggle":
        self.replay.playing = not self.replay.playing
      elif kind == "seek":
        self.replay.seek(float(cmd[1]))
      elif kind == "step":
        self.replay.playing = False
        self.replay.seek(self.replay.t + float(cmd[1]))
      elif kind == "speed":
        self.replay.set_speed(float(cmd[1]))

  def _clock(self) -> float:
    """The data clock the ticks are stamped with: the log's while replaying, else the device's monotonic."""
    if self.mode == "replay" and self.replay is not None:
      return self.replay.now
    return self.live.now

  def _decorate(self, tick: dict, clients: int) -> None:
    tick["mode"] = self.mode
    tick["replay"] = self.replay.status() if self.replay is not None else None
    tick["server"] = {"liveError": self.live_error, "notice": self.notice, "moving": self.monitor.moving,
                      "onroad": self.monitor.onroad, "clients": clients}

  # main loop ----------------------------------------------------------------
  def run(self) -> None:
    period = 1.0 / RATE_HZ
    last = time.monotonic()
    while not self.stop_event.is_set():
      now_wall = time.monotonic()
      dt = min(now_wall - last, 0.5)
      last = now_wall

      while not self.commands.empty():
        try:
          self._handle(self.commands.get_nowait())
        except Exception:
          cloudlog.exception("webhud: command failed")

      with self.clients_lock:
        clients = [c for c in self.clients if c.alive]
      self.monitor.tick()

      if not clients:
        if self.live.running and now_wall - self.last_client_t > IDLE_STOP_S:
          self.live.stop()
        time.sleep(0.25)
        continue

      try:
        if self.mode == "replay" and self.replay is not None:
          if self.monitor.moving:
            self._handle(("live",))
            self.notice = "Replay stopped: the car is moving"
            continue
          self.replay.tick(dt)
          now = self.replay.now
        else:
          if not self.live.running:
            try:
              self.live.start()
              self.live_error = None
            except Exception as e:
              self.live_error = f"{type(e).__name__}: {e}"
          if self.live.running:
            self.live.tick()
          now = self.live.now
        tick = self.builder.take_tick(now)
      except Exception:
        cloudlog.exception("webhud: engine tick failed")
        time.sleep(0.5)
        continue

      self._decorate(tick, len(clients))
      for c in clients:
        c.push_tick(tick)

      time.sleep(max(0.0, period - (time.monotonic() - now_wall)))


class _VehicleMonitor:
  """Watches the real car even while replaying, to gate replay and settings writes."""

  def __init__(self):
    self.sm = None
    self.moving = False
    self.onroad = False
    self._next_try = 0.0

  def tick(self) -> None:
    if self.sm is None:
      if time.monotonic() < self._next_try:
        return
      self._next_try = time.monotonic() + 30.0
      try:
        import cereal.messaging as messaging
        self.sm = messaging.SubMaster(["carState", "deviceState"])
      except Exception:
        return
    self.sm.update(0)
    self.onroad = bool(self.sm.alive["deviceState"] and self.sm["deviceState"].started)
    self.moving = bool(self.sm.alive["carState"] and self.sm["carState"].vEgo > MOVING_SPEED)


# ---- HTTP -------------------------------------------------------------------------------------------

class HudServer(ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True
  engine: Engine
  params: object
  overrides_api: "OverridesApi"
  hostname: str
  port: int
  mdns: MdnsPublisher | None


MAP_TILES = MapTiles()
MAP_FEATURES = MapFeatures()
MAP_PREFETCH = MapPrefetch(MAP_TILES, MAP_FEATURES)


class HudHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  server: HudServer
  _gzip_cache: dict[str, tuple[float, bytes, bytes]] = {}

  def log_message(self, format, *args):  # noqa: A002
    pass

  # helpers --------------------------------------------------------------------
  def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
      self.send_header(k, v)
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)

  def _json(self, obj, status: int = 200) -> None:
    self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json", {"Cache-Control": "no-store"})

  def _error(self, status: int, message: str) -> None:
    self._json({"error": message}, status)

  def _body(self, limit: int = 1 << 20) -> bytes:
    n = int(self.headers.get("Content-Length", 0) or 0)
    if not 0 <= n <= limit:
      raise ValueError("invalid body length")
    return self.rfile.read(n) if n else b""

  def _json_body(self):
    return json.loads(self._body() or b"{}")

  def _write_allowed(self) -> bool:
    """Writes come from the local network only, and never cross-origin (CSRF from a web page)."""
    try:
      if not ipaddress.ip_address(self.client_address[0]).is_private:
        self._error(403, "writes are only accepted from the local network")
        return False
    except ValueError:
      pass
    origin = self.headers.get("Origin")
    if origin and urlparse(origin).netloc != self.headers.get("Host"):
      self._error(403, "cross-origin request")
      return False
    return True

  # dispatch ---------------------------------------------------------------------
  def do_GET(self):
    self._dispatch("GET")

  def do_HEAD(self):
    self._dispatch("GET")

  def do_POST(self):
    self._dispatch("POST")

  def do_PUT(self):
    self._dispatch("PUT")

  def do_DELETE(self):
    self._dispatch("DELETE")

  def _dispatch(self, method: str) -> None:
    url = urlparse(self.path)
    path = url.path
    query = parse_qs(url.query)
    try:
      if method != "GET" and not self._write_allowed():
        return
      if path == "/ws" and method == "GET":
        return self._websocket()
      if path.startswith("/map/tile/") and method == "GET":
        return self._map_tile(path)
      if path.startswith("/map/features/") and method == "GET":
        return self._map_features(path)
      if path == "/map/prefetch" and method == "POST":   # the page's position: download the map around it (maptiles.py MapPrefetch)
        body = self._json_body()
        return self._json(MAP_PREFETCH.request(float(body["lat"]), float(body["lon"]), float(body.get("radius_km", 25))))
      if path == "/map/status" and method == "GET":
        return self._json(MAP_PREFETCH.status())
      route = ROUTES.get((method, path))
      if route is not None:
        return route(self, query)
      if method == "GET":
        return self._static(path)
      self._error(404, "not found")
    except (ValueError, KeyError, TypeError) as e:
      self._error(400, f"{type(e).__name__}: {e}")
    except Exception as e:
      cloudlog.exception(f"webhud: {method} {path} failed")
      self._error(500, f"{type(e).__name__}: {e}")

  # the OSM road tiles the page's map matcher reads (maptiles.py): long-lived, so cached hard
  def _map_tile(self, path: str) -> None:
    lat, lon, name = parse_tile_path(unquote(path))
    full = MAP_TILES.tile_path(lat, lon, name)
    if full is None:
      return self._error(404, "no such map tile")
    st = os.stat(full)
    etag = f'"{st.st_mtime_ns // 1_000_000:x}-{st.st_size:x}"'
    headers = {"Cache-Control": "max-age=604800", "ETag": etag, "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
    if self._not_modified(int(st.st_mtime), (etag,)):
      self.send_response(304)
      for k, v in headers.items():
        self.send_header(k, v)
      self.end_headers()
      return
    with open(full, "rb") as f:
      body = f.read()
    self.send_response(200)
    self.send_header("Content-Type", "application/octet-stream")
    self.send_header("Content-Length", str(len(body)))
    for k, v in headers.items():
      self.send_header(k, v)
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)

  # static -----------------------------------------------------------------------
  # the point features of a map cell (maptiles.py MapFeatures): from Overpass once, then from disk
  def _map_features(self, path: str) -> None:
    klat, klon = parse_features_path(unquote(path))
    full = MAP_FEATURES.cell_path(klat, klon)
    if full is None:
      return self._error(404, "no features for this cell")
    with open(full, "rb") as f:
      body = f.read()
    self._send(200, body, "application/json", {"Cache-Control": "max-age=86400"})

  def _not_modified(self, mtime: int, etags: tuple[str, ...]) -> bool:
    """The request's If-None-Match (or, without one, If-Modified-Since) says its copy is current."""
    inm = self.headers.get("If-None-Match")
    if inm is not None:
      return inm.strip() == "*" or any(t.strip().removeprefix("W/") in etags for t in inm.split(","))
    ims = self.headers.get("If-Modified-Since")
    if ims:
      try:
        return mtime <= parsedate_to_datetime(ims).timestamp()
      except (TypeError, ValueError):
        return False
    return False

  def _landing(self, rel: str) -> None:
    # the page isn't here (it is the Oceanfront View app): "/" says so, anything else is a 404
    if rel != "index.html":
      return self._error(404, "not found")
    self._send(200, LANDING.encode(), "text/plain; charset=utf-8", {"Cache-Control": "no-cache"})

  def _static(self, path: str) -> None:
    rel = unquote(path).lstrip("/") or "index.html"
    prefix = next((p for p in STATIC_ROOTS if rel.startswith(p)), None)
    if rel in STATIC_FILES:
      full = STATIC_FILES[rel]
    elif prefix is None:
      return self._landing(rel)
    else:
      root = os.path.realpath(STATIC_ROOTS[prefix])
      full = os.path.realpath(os.path.join(root, rel[len(prefix):]))
      if not full.startswith(root + os.sep) or not os.path.isfile(full):
        if STATIC_DIR is None:
          return self._landing(rel)
        # unknown paths fall back to the app shell so deep links work
        full, prefix = os.path.join(STATIC_DIR, "index.html"), ""
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    st = os.stat(full)
    mtime = st.st_mtime
    gzip_ok = ctype.startswith(GZIP_TYPES)
    # Validators, so the page's service worker (and browser caches) can check a file with a tiny 304
    # instead of pulling it again -- the car model is ~25 MB. The gzip encoding is its own entity.
    etag = f'"{st.st_mtime_ns // 1_000_000:x}-{st.st_size:x}"'
    etag_gz = etag[:-1] + '-gz"'
    use_gz = gzip_ok and "gzip" in self.headers.get("Accept-Encoding", "")
    # third-party libs and the car model rarely change; the app's own files always revalidate
    headers = {"Cache-Control": "max-age=86400" if prefix else "no-cache", "ETag": etag_gz if use_gz else etag,
               "Last-Modified": formatdate(mtime, usegmt=True)}
    if gzip_ok:
      headers["Vary"] = "Accept-Encoding"
    if self._not_modified(int(mtime), (etag, etag_gz)):
      self.send_response(304)
      for k, v in headers.items():
        self.send_header(k, v)
      self.end_headers()
      return
    cached = self._gzip_cache.get(full)
    if cached is None or cached[0] != mtime:
      with open(full, "rb") as f:
        raw = f.read()
      gz = gzip.compress(raw, 6) if ctype.startswith(GZIP_TYPES) else b""
      cached = (mtime, raw, gz)
      self._gzip_cache[full] = cached
    _, raw, gz = cached
    if gz and use_gz:
      headers["Content-Encoding"] = "gzip"
      self._send(200, gz, ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""), headers)
    else:
      self._send(200, raw, ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""), headers)

  # websocket --------------------------------------------------------------------
  def _websocket(self) -> None:
    key = self.headers.get("Sec-WebSocket-Key")
    if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
      return self._error(400, "expected a websocket upgrade")
    self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
    self.send_header("Upgrade", "websocket")
    self.send_header("Connection", "Upgrade")
    self.send_header("Sec-WebSocket-Accept", accept_key(key))
    self.end_headers()
    self.wfile.flush()
    self.close_connection = True

    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ws = WebSocket(self.connection, self.rfile)
    client = Client(ws, self.client_address[0])
    engine = self.server.engine
    engine.add_client(client)
    client.push(json.dumps({"type": "hello", "data": status_payload(self.server)}))
    engine.command("snapshot", client)
    try:
      while True:
        msg = ws.recv()
        if msg is None:
          break
        self._ws_message(client, msg)
    finally:
      engine.remove_client(client)
      client.close()

  def _ws_message(self, client: Client, msg: str) -> None:
    try:
      data = json.loads(msg)
    except ValueError:
      return
    kind = data.get("type")
    if kind == "replay":
      replay_command(self.server.engine, data)
    elif kind == "ping":
      client.push(json.dumps({"type": "pong", "t": data.get("t")}))


def status_payload(server: HudServer) -> dict:
  engine = server.engine
  ips = [ip for _, ip in ipv4_interfaces().values()]
  host = server.hostname if server.hostname.endswith(".local") else f"{server.hostname}.local"
  ports = getattr(server, "all_ports", [server.port])
  return {
    "version": VERSION,
    "hostname": host,
    "mdns": server.mdns.backend if server.mdns else "disabled",
    "urls": [f"http://{h}{'' if p == 80 else f':{p}'}" for p in ports for h in [host, *ips]],
    "mode": engine.mode,
    "brand": engine.builder.brand,
    "liveError": engine.live_error,
    "onroad": engine.monitor.onroad,
    "moving": engine.monitor.moving,
    "overrides": server.overrides_api.supported,
  }


def replay_command(engine: Engine, data: dict) -> None:
  action = data.get("action")
  if action == "load":
    roots, _ = data_roots()
    route = next((r for r in list_routes(roots) if r["name"] == data.get("route")), None)
    if route is None:
      raise KeyError(f"route {data.get('route')!r} not found")
    segs = route["segments"]
    start = float(data.get("t", 0.0))
    if data.get("segment") is not None:
      start = (int(data["segment"]) - segs[0]["n"]) * 60.0
    engine.command("load_route", route["name"], segs, start)
  elif action in ("play", "pause", "toggle", "live", "demo"):
    engine.command(action)
  elif action in ("seek", "step", "speed"):
    engine.command(action, float(data["value"]))
  else:
    raise ValueError(f"unknown replay action {action!r}")


# ---- REST endpoints -----------------------------------------------------------------------------

def api_status(h: HudHandler, q) -> None:
  h._json(status_payload(h.server))


def api_routes(h: HudHandler, q) -> None:
  roots, _ = data_roots()
  routes = list_routes(roots)
  for r in routes:
    for s in r["segments"]:
      s.pop("path", None)
  h._json({"routes": routes})


def api_replay(h: HudHandler, q) -> None:
  replay_command(h.server.engine, h._json_body())
  h._json({"ok": True})


def api_upload(h: HudHandler, q) -> None:
  """PUT raw rlog/qlog bytes (zst, bz2 or plain). Stored as its own one-segment route."""
  n = int(h.headers.get("Content-Length", 0) or 0)
  if n <= 0 or n > MAX_UPLOAD:
    return h._error(413, f"upload must be 1..{MAX_UPLOAD} bytes")
  name = os.path.basename(q.get("name", ["upload"])[0])
  _, upload_dir = data_roots()
  head = h.rfile.read(min(n, 4))
  ext = ".zst" if head[:4] == b"\x28\xb5\x2f\xfd" else (".bz2" if head[:3] == b"BZh" else "")
  kind = "qlog" if "qlog" in name else "rlog"
  os.makedirs(upload_dir, exist_ok=True)
  seg_dir = tempfile.mkdtemp(prefix=f"upload-{time.strftime('%Y%m%d-%H%M%S')}-", suffix="--0", dir=upload_dir)
  route = os.path.basename(seg_dir)[:-3]
  dest = os.path.join(seg_dir, kind + ext)
  try:
    with open(dest, "wb") as f:
      f.write(head)
      remaining = n - len(head)
      while remaining > 0:
        chunk = h.rfile.read(min(remaining, 1 << 20))
        if not chunk:
          raise ValueError("incomplete upload")
        f.write(chunk)
        remaining -= len(chunk)
  except Exception:
    shutil.rmtree(seg_dir, ignore_errors=True)
    raise
  # keep only the newest uploads
  dirs = sorted((d for d in os.scandir(upload_dir) if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
  for old in dirs[MAX_UPLOADS_KEPT:]:
    shutil.rmtree(old.path, ignore_errors=True)
  cloudlog.info(f"webhud: uploaded {name} -> {dest}")
  h.server.engine.command("load_route", route, [{"n": 0, "path": dest}], 0.0)
  h._json({"ok": True, "route": route})


def api_params_get(h: HudHandler, q) -> None:
  out = {}
  for key, (typ, _) in PARAM_ALLOWLIST.items():
    try:
      out[key] = h.server.params.get_bool(key) if typ is bool else h.server.params.get(key, return_default=True)
    except Exception:
      out[key] = None
  h._json(out)


def api_params_put(h: HudHandler, q) -> None:
  body = h._json_body()
  for key, value in body.items():
    if key not in PARAM_ALLOWLIST:
      return h._error(400, f"{key} is not editable")
    typ, check = PARAM_ALLOWLIST[key]
    if typ is bool:
      h.server.params.put_bool(key, bool(value))
    else:
      value = typ(value)
      if check is not None and not check(value):
        return h._error(400, f"invalid value for {key}")
      h.server.params.put(key, value)
  api_params_get(h, q)


# ---- SSH keys: the Oceanfront View app's key, for managing the comma over SSH -----------------------------
# The app makes its own key pair (in the head unit's keystore) and asks for its public half to be let in. It goes
# into GithubSshKeys beside whatever is there (the comma's sshd reads that param for `comma`), and SSH is turned on.
# Like every write here: from the local network only, never cross-origin, and not while the car is moving.

SSH_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
SSH_COMMENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")


def parse_ssh_key(text) -> tuple[str, bytes, str]:
  """(type, key blob, comment) of one OpenSSH public key line; ValueError for anything else."""
  if not isinstance(text, str) or "\n" in text.strip() or len(text) > 4096:
    raise ValueError("one public key line expected")
  parts = text.strip().split()
  if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
    raise ValueError(f"key type must be one of {', '.join(SSH_KEY_TYPES)}")
  try:
    blob = base64.b64decode(parts[1], validate=True)
  except (binascii.Error, ValueError):
    raise ValueError("the key isn't base64") from None
  name = parts[0].encode()
  if blob[:4] != len(name).to_bytes(4, "big") or blob[4:4 + len(name)] != name:
    raise ValueError("the key blob doesn't match its type")
  comment = "".join(c for c in " ".join(parts[2:]) if c in SSH_COMMENT_OK)[:64]
  return parts[0], blob, comment


def ssh_fingerprint(blob: bytes) -> str:
  return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _ssh_keys(params) -> list[str]:
  raw = params.get("GithubSshKeys")
  if isinstance(raw, bytes):
    raw = raw.decode("utf-8", "replace")
  return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _ssh_blob(line: str) -> bytes | None:
  try:
    return parse_ssh_key(line)[1]
  except ValueError:
    return None


def api_ssh_get(h: "HudHandler", q) -> None:
  keys = []
  for line in _ssh_keys(h.server.params):
    try:
      kind, blob, comment = parse_ssh_key(line)
      keys.append({"type": kind, "fingerprint": ssh_fingerprint(blob), "comment": comment})
    except ValueError:
      keys.append({"type": "unknown", "fingerprint": None, "comment": ""})
  try:
    enabled = bool(h.server.params.get_bool("SshEnabled"))
  except Exception:
    enabled = None
  h._json({"enabled": enabled, "user": "comma", "port": 22, "keys": keys})


def api_ssh_authorize(h: "HudHandler", q) -> None:
  if h.server.engine.monitor.moving:
    return h._error(409, "not while the car is moving")
  kind, blob, comment = parse_ssh_key(h._json_body().get("publicKey"))
  lines = _ssh_keys(h.server.params)
  if not any(_ssh_blob(line) == blob for line in lines):
    lines.append(f"{kind} {base64.b64encode(blob).decode()} {comment or 'oceanfront-view'}")
    h.server.params.put("GithubSshKeys", "\n".join(lines))
    cloudlog.warning(f"webhud: SSH key {ssh_fingerprint(blob)} ({comment}) authorized by {h.client_address[0]}")
  h.server.params.put_bool("SshEnabled", True)
  api_ssh_get(h, q)


def api_ssh_revoke(h: "HudHandler", q) -> None:
  _, blob, _ = parse_ssh_key(h._json_body().get("publicKey"))
  lines = _ssh_keys(h.server.params)
  keep = [line for line in lines if _ssh_blob(line) != blob]
  if len(keep) != len(lines):
    if keep:
      h.server.params.put("GithubSshKeys", "\n".join(keep))
    else:
      h.server.params.remove("GithubSshKeys")
    cloudlog.warning(f"webhud: SSH key {ssh_fingerprint(blob)} revoked by {h.client_address[0]}")
  api_ssh_get(h, q)


class OverridesApi:
  def __init__(self, params, builder: StreamBuilder, monitor: _VehicleMonitor):
    from openpilot.selfdrive.car.can_overrides import CanOverrides
    self.params = params
    self.builder = builder
    self.monitor = monitor
    try:
      self.ov = CanOverrides.for_brand("fisker")
    except Exception:
      cloudlog.exception("webhud: CAN overrides unavailable")
      self.ov = None

  @property
  def supported(self) -> bool:
    return self.ov is not None

  def stored(self) -> dict:
    from openpilot.selfdrive.car.can_overrides import PARAM
    val = self.params.get(PARAM)
    return val if isinstance(val, dict) else {}

  def describe(self) -> dict:
    from openpilot.selfdrive.car.can_overrides import PROTECTED_SUFFIXES, signal_range
    assert self.ov is not None
    stored = self.stored()
    effective = self.ov.resolve(stored)
    tables = []
    for t in self.ov.tables:
      msg = self.ov.dbc.by_name[t.message]
      live = self.builder.decoded(msg.address) or {}
      blocked = self.ov.passthrough(t)
      sigs = []
      for name, sig in msg.signals.items():
        lo, hi = signal_range(sig)
        sigs.append({
          "name": name, "comment": sig.comment, "unit": sig.unit, "factor": sig.factor, "min": lo, "max": hi,
          "values": {str(k): v for k, v in sig.values.items()},
          "default": self.ov.defaults[t.message].get(name),
          "override": effective[t.message].get(name),
          "current": live.get(name),   # what the ICC itself is sending right now
          "locked": name.endswith(PROTECTED_SUFFIXES) or name in blocked,
        })
      tables.append({"message": t.message, "address": msg.address, "description": t.description,
                     "customized": t.message in stored, "signals": sigs})
    return {"supported": True, "tables": tables, "editable": not self.monitor.moving}

  def save(self, body: dict) -> tuple[int, dict]:
    from openpilot.selfdrive.car.can_overrides import PARAM
    assert self.ov is not None
    if self.monitor.moving:
      return 409, {"error": "CAN overrides can't be changed while the car is moving"}
    clean, errors = self.ov.validate(body)
    if errors:
      return 400, {"error": "; ".join(errors)}
    stored = self.stored()
    stored.update(clean)
    self.params.put(PARAM, stored)
    return 200, self.describe()

  def reset(self, message: str | None) -> tuple[int, dict]:
    from openpilot.selfdrive.car.can_overrides import PARAM
    if self.monitor.moving:
      return 409, {"error": "CAN overrides can't be changed while the car is moving"}
    stored = self.stored()
    if message:
      stored.pop(message, None)
    if message and stored:
      self.params.put(PARAM, stored)
    else:
      self.params.remove(PARAM)
    return 200, self.describe()


def api_overrides_get(h: HudHandler, q) -> None:
  api = h.server.overrides_api
  h._json(api.describe() if api.supported else {"supported": False, "tables": []})


def api_overrides_put(h: HudHandler, q) -> None:
  api = h.server.overrides_api
  if not api.supported:
    return h._error(404, "no CAN overrides for this car")
  status, body = api.save(h._json_body())
  h._json(body, status)


def api_overrides_delete(h: HudHandler, q) -> None:
  api = h.server.overrides_api
  if not api.supported:
    return h._error(404, "no CAN overrides for this car")
  status, body = api.reset(q.get("message", [None])[0])
  h._json(body, status)


ROUTES = {
  ("GET", "/api/status"): api_status,
  ("GET", "/api/routes"): api_routes,
  ("POST", "/api/replay"): api_replay,
  ("PUT", "/api/upload"): api_upload,
  ("GET", "/api/params"): api_params_get,
  ("PUT", "/api/params"): api_params_put,
  ("GET", "/api/overrides"): api_overrides_get,
  ("PUT", "/api/overrides"): api_overrides_put,
  ("DELETE", "/api/overrides"): api_overrides_delete,
  ("GET", "/api/ssh"): api_ssh_get,
  ("POST", "/api/ssh/authorize"): api_ssh_authorize,
  ("DELETE", "/api/ssh/authorize"): api_ssh_revoke,
}


# ---- entry point -------------------------------------------------------------------------------

def make_server(host: str, port: int, engine: Engine, params, overrides_api: OverridesApi, hostname: str) -> HudServer:
  server = HudServer((host, port), HudHandler)
  server.engine = engine
  server.params = params
  server.overrides_api = overrides_api
  server.hostname = hostname
  server.port = port
  server.mdns = None
  return server


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description="StarPilot web HUD")
  parser.add_argument("--host", default="0.0.0.0")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--hostname", default=DEFAULT_HOSTNAME, help="mDNS name (<name>.local)")
  parser.add_argument("--no-port80", action="store_true", help="don't also try to serve on port 80")
  parser.add_argument("--no-mdns", action="store_true")
  parser.add_argument("--replay", help="rlog file or segment/route directory to play on start")
  parser.add_argument("--demo", action="store_true", help="start with the synthetic demo drive")
  parser.add_argument("--map-root", default=None, help="directory of OSM tile cells for the page's map matcher (default: mapd's <media>/osm/offline)")
  args = parser.parse_args(argv if argv is not None else [])
  if args.map_root:
    MAP_TILES.root = args.map_root
    MAP_FEATURES.root = os.path.join(os.path.dirname(os.path.abspath(args.map_root)), "features")   # beside the tiles' `offline`

  try:
    os.nice(10)  # never compete with the driving stack
  except OSError:
    pass

  params = open_params()
  engine = Engine(params, args.replay, args.demo)
  overrides_api = OverridesApi(OverrideStore(), engine.builder, engine.monitor)

  servers = [make_server(args.host, args.port, engine, params, overrides_api, args.hostname)]
  if not args.no_port80 and args.port != 80:
    try:
      servers.append(make_server(args.host, 80, engine, params, overrides_api, args.hostname))
    except OSError:
      pass  # needs root or cap_net_bind_service; the main port is enough

  mdns = None
  if not args.no_mdns:
    mdns = MdnsPublisher(args.hostname, args.port)
    mdns.start()
  ports = [s.port for s in servers]
  for s in servers:
    s.mdns = mdns
    s.all_ports = ports  # type: ignore[attr-defined]
    threading.Thread(target=s.serve_forever, name=f"webhud-http-{s.port}", daemon=True).start()
  cloudlog.warning(f"webhud: serving http://{args.hostname}.local:{args.port} on ports {ports}")
  print(f"webhud: http://{args.hostname}.local:{args.port} (ports {ports})", flush=True)

  engine.run()


if __name__ == "__main__":
  import sys
  main(sys.argv[1:])
