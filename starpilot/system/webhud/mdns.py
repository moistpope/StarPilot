"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Make the web HUD reachable as http://<name>.local without touching the device hostname.

AGNOS runs avahi-daemon, so the preferred path registers an extra A record (an alias for each
interface address) plus an _http._tcp service through avahi's D-Bus API. Where avahi isn't
available (a PC, a stripped image) a tiny built-in responder answers A queries for the name
itself. Both re-publish when the device's addresses change (Wi-Fi/hotspot switches).
"""
import fcntl
import socket
import struct
import threading
import time

from openpilot.common.swaglog import cloudlog

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
TTL = 120
REFRESH_S = 10.0

# avahi constants
AVAHI_IF_UNSPEC = -1
AVAHI_PROTO_INET = 0
AVAHI_PROTO_UNSPEC = -1
AVAHI_PUBLISH_NO_REVERSE = 16   # the device hostname already owns the reverse (PTR) records
SIOCGIFADDR = 0x8915


def ipv4_interfaces() -> dict[str, tuple[int, str]]:
  """{ifname: (ifindex, ipv4)} for every non-loopback interface with an IPv4 address."""
  out = {}
  try:
    names = socket.if_nameindex()
  except OSError:
    return out
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    for idx, name in names:
      if name == "lo":
        continue
      try:
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
        ip = socket.inet_ntoa(res[20:24])
      except OSError:
        continue
      if not ip.startswith("127."):
        out[name] = (idx, ip)
  finally:
    s.close()
  return out


def route_ip(dest: str) -> str | None:
  """Local address the kernel would use to reach `dest` (nothing is sent)."""
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    s.connect((dest, 9))
    return s.getsockname()[0]
  except OSError:
    return None
  finally:
    s.close()


# ---- DNS wire helpers (built-in responder) -----------------------------------------------------

def encode_name(name: str) -> bytes:
  out = b""
  for label in name.rstrip(".").split("."):
    raw = label.encode()
    out += bytes([len(raw)]) + raw
  return out + b"\x00"


def decode_name(data: bytes, off: int) -> tuple[str, int]:
  labels = []
  jumped = False
  end = off
  for _ in range(128):  # bound pointer loops
    n = data[off]
    if n == 0:
      off += 1
      break
    if n & 0xC0 == 0xC0:
      ptr = ((n & 0x3F) << 8) | data[off + 1]
      if not jumped:
        end = off + 2
      jumped = True
      off = ptr
      continue
    labels.append(data[off + 1:off + 1 + n].decode(errors="replace"))
    off += 1 + n
  else:
    raise ValueError("name too long")
  return ".".join(labels), (end if jumped else off)


def parse_query(data: bytes) -> tuple[int, list[tuple[str, int, int]]] | None:
  """(id, [(name, qtype, qclass)]) for a query packet, None for responses/garbage."""
  if len(data) < 12:
    return None
  qid, flags, qdcount = struct.unpack("!HHH", data[:6])
  if flags & 0x8000:  # a response
    return None
  questions = []
  off = 12
  try:
    for _ in range(qdcount):
      name, off = decode_name(data, off)
      qtype, qclass = struct.unpack("!HH", data[off:off + 4])
      off += 4
      questions.append((name, qtype, qclass))
  except (IndexError, ValueError, struct.error):
    return None
  return qid, questions


def build_a_response(name: str, ip: str, qid: int = 0, question: bool = False, cache_flush: bool = True) -> bytes:
  qd = 1 if question else 0
  out = struct.pack("!HHHHHH", qid, 0x8400, qd, 1, 0, 0)
  if question:
    out += encode_name(name) + struct.pack("!HH", 1, 1)
  rr_class = 0x8001 if cache_flush else 0x0001
  out += encode_name(name) + struct.pack("!HHIH", 1, rr_class, TTL, 4) + socket.inet_aton(ip)
  return out


class _BuiltinResponder:
  """Answers A queries for one .local name. Enough for browsers to resolve the HUD."""

  def __init__(self, fqdn: str):
    self.fqdn = fqdn.lower()
    self.sock: socket.socket | None = None
    self.groups: set[str] = set()

  def start(self) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
      s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.bind(("", MDNS_PORT))
    s.settimeout(1.0)
    self.sock = s

  def healthy(self) -> bool:
    return True

  def publish(self, ifaces: dict[str, tuple[int, str]]) -> None:
    assert self.sock is not None
    for _, ip in ifaces.values():
      if ip in self.groups:
        continue
      try:
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MDNS_ADDR) + socket.inet_aton(ip))
        self.groups.add(ip)
        self.announce(ip)
      except OSError as e:
        cloudlog.warning(f"webhud mdns: join on {ip} failed: {e}")

  def announce(self, ip: str) -> None:
    try:
      assert self.sock is not None
      self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
      self.sock.sendto(build_a_response(self.fqdn, ip), (MDNS_ADDR, MDNS_PORT))
    except OSError:
      pass

  def serve_once(self) -> None:
    assert self.sock is not None
    try:
      data, (src_ip, src_port) = self.sock.recvfrom(9000)
    except (TimeoutError, OSError):
      return
    query = parse_query(data)
    if query is None:
      return
    qid, questions = query
    for name, qtype, qclass in questions:
      if name.lower() != self.fqdn or qtype not in (1, 255):  # A or ANY
        continue
      ip = route_ip(src_ip)
      if ip is None:
        return
      if src_port != MDNS_PORT:
        # legacy unicast resolver (RFC 6762 6.7): echo the id + question, no cache-flush bit
        self.sock.sendto(build_a_response(self.fqdn, ip, qid=qid, question=True, cache_flush=False), (src_ip, src_port))
      elif qclass & 0x8000:  # QU: unicast response requested
        self.sock.sendto(build_a_response(self.fqdn, ip), (src_ip, src_port))
      else:
        self.announce(ip)
      return

  def close(self) -> None:
    if self.sock is not None:
      self.sock.close()


class _AvahiPublisher:
  def __init__(self, fqdn: str, port: int, service_name: str):
    from jeepney import DBusAddress
    from jeepney.io.blocking import open_dbus_connection
    self.fqdn = fqdn
    self.port = port
    self.service_name = service_name
    self.conn = open_dbus_connection(bus="SYSTEM")
    self.server = DBusAddress("/", bus_name="org.freedesktop.Avahi", interface="org.freedesktop.Avahi.Server")
    self.group = None
    # the device's own name is already published by avahi; adding it again would collide
    self.alias_is_hostname = self._call(self.server, "GetHostNameFqdn")[0].lower() == fqdn.lower()

  def _call(self, addr, method: str, signature: str | None = None, body: tuple = ()):
    from jeepney import new_method_call
    from jeepney.low_level import MessageType
    msg = new_method_call(addr, method, signature, body) if signature else new_method_call(addr, method)
    reply = self.conn.send_and_get_reply(msg, timeout=5)
    if reply.header.message_type == MessageType.error:
      raise RuntimeError(f"avahi {method}: {reply.body}")
    return reply.body

  def publish(self, ifaces: dict[str, tuple[int, str]]) -> None:
    from jeepney import DBusAddress
    if self.group is None:
      path = self._call(self.server, "EntryGroupNew")[0]
      self.group = DBusAddress(path, bus_name="org.freedesktop.Avahi", interface="org.freedesktop.Avahi.EntryGroup")
    else:
      self._call(self.group, "Reset")
    if not self.alias_is_hostname:
      for idx, ip in ifaces.values():
        self._call(self.group, "AddAddress", "iiuss", (idx, AVAHI_PROTO_INET, AVAHI_PUBLISH_NO_REVERSE, self.fqdn, ip))
    txt = [b"path=/"]
    self._call(self.group, "AddService", "iiussssqaay",
               (AVAHI_IF_UNSPEC, AVAHI_PROTO_INET, 0, self.service_name, "_http._tcp", "", self.fqdn, self.port, txt))
    self._call(self.group, "Commit")

  def healthy(self) -> bool:
    """False once avahi lost our entry group (daemon restart) or reports a name collision."""
    if self.group is None:
      return False
    try:
      return self._call(self.group, "GetState")[0] in (1, 2)  # registering / established
    except Exception:
      return False

  def close(self) -> None:
    try:
      if self.group is not None:
        self._call(self.group, "Free")
    except Exception:
      pass
    self.conn.close()


class MdnsPublisher:
  def __init__(self, hostname: str, port: int, service_name: str = "sunnypilot HUD"):
    self.fqdn = hostname if hostname.endswith(".local") else f"{hostname}.local"
    self.port = port
    self.service_name = service_name
    self.backend = "none"
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None

  def start(self) -> None:
    self._thread = threading.Thread(target=self._run, name="webhud-mdns", daemon=True)
    self._thread.start()

  def stop(self) -> None:
    self._stop.set()

  def _open_backend(self):
    try:
      backend = _AvahiPublisher(self.fqdn, self.port, self.service_name)
      self.backend = "avahi"
      return backend
    except Exception as e:
      cloudlog.info(f"webhud mdns: avahi unavailable ({e}), using built-in responder")
    try:
      responder = _BuiltinResponder(self.fqdn)
      responder.start()
      self.backend = "builtin"
      return responder
    except OSError as e:
      cloudlog.warning(f"webhud mdns: built-in responder failed: {e}")
    self.backend = "none"
    return None

  def _run(self) -> None:
    backend: _AvahiPublisher | _BuiltinResponder | None = None
    published = None
    next_check = 0.0
    while not self._stop.is_set():
      now = time.monotonic()
      if now >= next_check:
        next_check = now + REFRESH_S
        ifaces = ipv4_interfaces()
        if backend is None:
          backend = self._open_backend()
        if backend is not None and (ifaces != published or not backend.healthy()):
          try:
            backend.publish(ifaces)
            published = ifaces
            cloudlog.info(f"webhud mdns: {self.fqdn} -> {[ip for _, ip in ifaces.values()]} via {self.backend}")
          except Exception as e:
            cloudlog.warning(f"webhud mdns: publish failed: {e}")
            backend.close()
            backend, published = None, None
      if isinstance(backend, _BuiltinResponder):
        backend.serve_once()
      else:
        self._stop.wait(1.0)
    if backend is not None:
      backend.close()
