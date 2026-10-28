#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Audit a Fisker Ocean drive log for hand-off problems between openpilot and the stock ADAS module.

For each relayed message (0x1D0 steering angle, 0x1C0 lateral activation, 0x121 accel) it rebuilds what
the car's vehicle bus saw: the stock frames panda forwarded (src 128) and openpilot's own frames (the
src-128 echoes of its sendcan), in bus order, and judges them with opendbc/car/fisker/relay_audit.py:
one source at a time, a strictly +1 alive counter, only bounded steps at a hand-off, no long gaps. It
also prints the health signals that went wrong in the drive that faulted the car (ACC states 9/10,
EPS_AdasLatCtrlSts, ESP fault indicators, VCU_ACCRdy), panda's lateral flag against our last frame, and
panda's rejected frames (src 192).

  python -m openpilot.starpilot.system.webhud.tools.fisker_handoff_audit ROUTE_SEGMENT.rlog.zst [...]

Exit status 1 if any invariant is violated.
"""
import argparse
import collections
import os
import sys

from opendbc import DBC_PATH
from opendbc.car.fisker.relay_audit import OURS, STOCK, BusFrame, audit_stream
from openpilot.starpilot.system.webhud.dbc import DBC
from openpilot.starpilot.system.webhud.sources import read_log_bytes

RELAYED = {0x1D0: "steering angle", 0x1C0: "lateral activation", 0x121: "accel"}
RELEASE_FRAME_ADDR = 0x5FE
VEHICLE_ECHO_SRC = 128   # panda's own transmissions on bus 0 (forwarded stock frames and ours) come back as bus + 128
REJECTED_SRC = 192


def load(path: str):
  from cereal import log
  data = read_log_bytes(path)
  t0 = None
  can = []            # (t, addr, src, data)
  sendcan = []        # (t, addr, data)
  panda = []          # (t, controlsAllowed, controlsAllowedLateral)
  for evt in log.Event.read_multiple_bytes(data):
    try:
      which = evt.which()
    except Exception:
      continue
    if t0 is None:
      t0 = evt.logMonoTime
    t = (evt.logMonoTime - t0) * 1e-9
    if which == "can":
      can.extend((t, c.address, c.src, bytes(c.dat)) for c in evt.can)
    elif which == "sendcan":
      sendcan.extend((t, c.address, bytes(c.dat)) for c in evt.sendcan)
    elif which == "pandaStates":
      for p in evt.pandaStates:
        panda.append((t, bool(p.controlsAllowed), bool(p.controlsAllowedLateral)))
  return can, sendcan, panda


def vehicle_bus_streams(can, sendcan) -> dict[int, list[BusFrame]]:
  """Per relayed address, the frames the vehicle bus saw, tagged stock or ours. A src-128 frame is ours if
  its bytes equal one we sent (the SecOC tail / E2E fill differ from the stock module's)."""
  ours_pending: dict[tuple[int, bytes], list[float]] = collections.defaultdict(list)
  for t, addr, data in sendcan:
    if addr in RELAYED:
      ours_pending[(addr, data)].append(t)
  streams: dict[int, list[BusFrame]] = {a: [] for a in RELAYED}
  for t, addr, src, data in can:
    if addr not in RELAYED or src != VEHICLE_ECHO_SRC:
      continue
    sent = ours_pending.get((addr, data))
    is_ours = bool(sent) and sent[0] <= t
    if is_ours:
      sent.pop(0)
    streams[addr].append(BusFrame(t, OURS if is_ours else STOCK, data[1] & 0x0F))
  return streams


def health(can, panda, ours_last: float | None, dbc: DBC) -> list[str]:
  out = []
  eps = dbc.messages[0x1C2]
  esp114 = dbc.messages[0x114]
  vcu = dbc.messages[0x214]
  prev = {}
  for t, addr, src, data in can:
    if addr == 0x313 and src == 2:
      v = data[4] & 0xF
      if prev.get("acc") != v:
        if v in (9, 10) or prev.get("acc") is not None:
          out.append(f"{t:9.3f}s ACC state {prev.get('acc')} -> {v}" + ("   <-- failure state" if v in (9, 10) else ""))
        prev["acc"] = v
    elif addr == 0x1C2 and src == 0:
      v = int(eps.decode(data)["EPS_AdasLatCtrlSts"])
      if prev.get("eps") != v:
        out.append(f"{t:9.3f}s EPS_AdasLatCtrlSts {prev.get('eps')} -> {v}" + ("   <-- EPS reports a lateral fault/timeout" if v == 3 else ""))
        prev["eps"] = v
    elif addr == 0x114 and src == 0:
      d = esp114.decode(data)
      v = tuple(int(d[k]) for k in ("ESP_FltIndcn_ABA", "ESP_FltIndcn_ABP", "ESP_FltIndcn_AEB", "ESP_FltIndcn_AWB"))
      if prev.get("esp") != v:
        if any(v):
          out.append(f"{t:9.3f}s ESP fault indicators (ABA, ABP, AEB, AWB) {prev.get('esp')} -> {v}   <-- ESP fault")
        prev["esp"] = v
    elif addr == 0x214 and src == 0:
      v = int(vcu.decode(data)["VCU_ACCRdy"])
      if prev.get("rdy") != v:
        if prev.get("rdy") is not None:
          out.append(f"{t:9.3f}s VCU_ACCRdy {prev.get('rdy')} -> {v}")
        prev["rdy"] = v
  if ours_last is not None:
    fell = next((t for t, _, lat in panda if t > ours_last and not lat), None)
    if fell is not None and fell - ours_last > 0.5:
      out.append(f"panda controlsAllowedLateral stayed set {fell - ours_last:.2f} s after our last frame (the arbiter must not depend on it)")
  return out


def audit(path: str, verbose: bool) -> bool:
  can, sendcan, panda = load(path)
  dbc = DBC(os.path.join(DBC_PATH, "fisker_ocean_adas.dbc"))
  streams = vehicle_bus_streams(can, sendcan)
  rejected = collections.Counter(hex(a) for _, a, s, _ in can if s == REJECTED_SRC and a in (*RELAYED, RELEASE_FRAME_ADDR))
  print(f"== {os.path.basename(path)}")
  ok = True
  ours_last = None
  for addr, name in RELAYED.items():
    frames = streams[addr]
    n_ours = sum(1 for f in frames if f.source == OURS)
    if not n_ours:
      print(f"  0x{addr:03x} {name}: openpilot never replaced it ({len(frames)} stock frames)")
      continue
    ours_last = max(ours_last or 0.0, max(f.t for f in frames if f.source == OURS))
    res = audit_stream(frames)
    ok &= res.ok
    verdict = "OK" if res.ok else "VIOLATIONS"
    counts = f"{n_ours} of ours, {res.takeovers} takeover(s), {res.releases} release(s)"
    print(f"  0x{addr:03x} {name}: {counts}, max gap {res.max_gap_s * 1e3:.0f} ms (log timestamps are batch-quantized): {verdict}")
    for v in res.violations[:20 if verbose else 6]:
      print(f"      VIOLATION {v.t:9.3f}s {v.kind}: {v.detail}")
    for n in res.notes[:6]:
      print(f"      note      {n.t:9.3f}s {n.kind}: {n.detail}")
  if rejected:
    print(f"  panda-rejected frames (src 192): {dict(rejected)} (release frames are rejected by design)")
  for line in health(can, panda, ours_last, dbc):
    print("  " + line)
  return ok


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("logs", nargs="+")
  ap.add_argument("-v", "--verbose", action="store_true")
  args = ap.parse_args()
  results = [audit(p, args.verbose) for p in args.logs]
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
