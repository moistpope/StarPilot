"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Small, dependency-free DBC reader + decoder for the web HUD.

opendbc's CANParser is built for the control loop (liveness checks, checksums, every frame).
The HUD only needs "latest value of every signal", decoded at display rate, plus the metadata
opendbc drops (value-table names, comments, units, ranges) to drive the UI and signal browser.
Decoding one frame is a single int.from_bytes() plus a shift/mask per signal.
"""
import re
from dataclasses import dataclass, field

BO_RE = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\w+)")
SG_RE = re.compile(r"^\s*SG_\s+(\w+)\s*(M|m\d+)?\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*\(([^,]+),([^)]+)\)\s*\[([^|]*)\|([^\]]*)\]\s*\"([^\"]*)\"")
VAL_RE = re.compile(r"^VAL_\s+(\d+)\s+(\w+)\s+(.*);\s*$")
VAL_PAIR_RE = re.compile(r"(-?\d+)\s+\"([^\"]*)\"")
CM_SG_RE = re.compile(r"^CM_\s+SG_\s+(\d+)\s+(\w+)\s+\"(.*?)\";", re.S | re.M)
CM_BO_RE = re.compile(r"^CM_\s+BO_\s+(\d+)\s+\"(.*?)\";", re.S | re.M)
CYCLE_RE = re.compile(r"^BA_\s+\"GenMsgCycleTime\"\s+BO_\s+(\d+)\s+(\d+);", re.M)


@dataclass
class Signal:
  name: str
  start_bit: int
  size: int
  little_endian: bool
  signed: bool
  factor: float
  offset: float
  minimum: float
  maximum: float
  unit: str
  values: dict[int, str] = field(default_factory=dict)
  comment: str = ""
  # precomputed extraction for a frame of `msg_size` bytes
  shift: int = 0
  mask: int = 0

  def raw_to_phys(self, raw: int) -> float:
    if self.signed and raw & (1 << (self.size - 1)):
      raw -= 1 << self.size
    return raw * self.factor + self.offset

  def phys_to_raw(self, value: float) -> int:
    if isinstance(value, int) and self.factor == 1 and self.offset == 0:
      raw = value  # exact for wide integer signals (VIN, names) that a float can't hold
    else:
      raw = round((value - self.offset) / self.factor)
    if raw < 0:
      raw += 1 << self.size
    return raw & self.mask

  def describe(self, value: float) -> str | None:
    """Value-table name for a decoded value, if the DBC has one."""
    if not self.values or not self.factor:
      return None
    return self.values.get(round((value - self.offset) / self.factor))

  def to_json(self) -> dict:
    return {
      "name": self.name, "startBit": self.start_bit, "size": self.size, "littleEndian": self.little_endian,
      "signed": self.signed, "factor": self.factor, "offset": self.offset, "min": self.minimum, "max": self.maximum,
      "unit": self.unit, "values": {str(k): v for k, v in self.values.items()}, "comment": self.comment,
    }


@dataclass
class Message:
  address: int
  name: str
  size: int
  transmitter: str
  signals: dict[str, Signal] = field(default_factory=dict)
  comment: str = ""
  cycle_ms: int = 0

  def finalize(self) -> None:
    nbits = self.size * 8
    for sig in self.signals.values():
      sig.mask = (1 << sig.size) - 1
      if sig.little_endian:
        sig.shift = sig.start_bit
      else:
        # Motorola: start_bit is the MSB in DBC "sawtooth" numbering. Convert to a linear
        # big-endian bit index (0 = MSB of byte 0) to find the LSB position.
        msb_lin = (sig.start_bit // 8) * 8 + (7 - sig.start_bit % 8)
        lsb_lin = msb_lin + sig.size - 1
        sig.shift = nbits - 1 - lsb_lin

  def decode(self, data: bytes) -> dict[str, float]:
    """Decode every signal of a frame into physical values."""
    if len(data) < self.size:
      data = bytes(data) + bytes(self.size - len(data))
    elif len(data) > self.size:
      data = data[:self.size]
    be = int.from_bytes(data, "big")
    le = None
    out = {}
    for name, sig in self.signals.items():
      if sig.little_endian:
        if le is None:
          le = int.from_bytes(data, "little")
        raw = (le >> sig.shift) & sig.mask
      else:
        raw = (be >> sig.shift) & sig.mask
      if sig.signed and raw >> (sig.size - 1):
        raw -= 1 << sig.size
      out[name] = raw * sig.factor + sig.offset if (sig.factor != 1 or sig.offset != 0) else raw
    return out

  def encode(self, values: dict[str, float], base: bytes | None = None) -> bytes:
    """Pack physical values into a frame, starting from `base` (unlisted signals keep their bits)."""
    data = bytes(base or b"")[:self.size]
    data += bytes(self.size - len(data))
    for little_endian, byteorder in ((False, "big"), (True, "little")):
      word = int.from_bytes(data, byteorder)
      for name, value in values.items():
        sig = self.signals[name]
        if sig.little_endian == little_endian:
          word = (word & ~(sig.mask << sig.shift)) | (sig.phys_to_raw(value) << sig.shift)
      data = word.to_bytes(self.size, byteorder)
    return data

  def to_json(self) -> dict:
    return {
      "address": self.address, "name": self.name, "size": self.size, "transmitter": self.transmitter,
      "comment": self.comment, "cycleMs": self.cycle_ms, "signals": [s.to_json() for s in self.signals.values()],
    }


def _float(s: str, default: float = 0.0) -> float:
  try:
    return float(s)
  except ValueError:
    return default


class DBC:
  def __init__(self, path: str | None = None, text: str | None = None):
    if text is None:
      assert path is not None
      with open(path, encoding="latin-1") as f:
        text = f.read()
    self.messages: dict[int, Message] = {}
    self.by_name: dict[str, Message] = {}
    self._parse(text.replace("\r\n", "\n"))

  def _parse(self, text: str) -> None:
    cur: Message | None = None
    for line in text.split("\n"):
      if line.startswith("BO_ "):
        m = BO_RE.match(line)
        cur = None
        if m:
          # extended IDs carry bit 31 in DBC files
          addr = int(m.group(1)) & 0x1FFFFFFF
          cur = Message(addr, m.group(2), int(m.group(3)), m.group(4))
          self.messages[addr] = cur
          self.by_name[cur.name] = cur
      elif cur is not None and line.lstrip().startswith("SG_ "):
        m = SG_RE.match(line)
        if m:
          name = m.group(1)
          cur.signals[name] = Signal(
            name=name, start_bit=int(m.group(3)), size=int(m.group(4)), little_endian=m.group(5) == "1",
            signed=m.group(6) == "-", factor=_float(m.group(7), 1.0), offset=_float(m.group(8)),
            minimum=_float(m.group(9)), maximum=_float(m.group(10)), unit=m.group(11),
          )
      elif line.startswith("VAL_ "):
        m = VAL_RE.match(line)
        if m:
          msg = self.messages.get(int(m.group(1)) & 0x1FFFFFFF)
          if msg is not None and m.group(2) in msg.signals:
            msg.signals[m.group(2)].values = {int(k): v for k, v in VAL_PAIR_RE.findall(m.group(3))}

    for m in CM_SG_RE.finditer(text):
      msg = self.messages.get(int(m.group(1)) & 0x1FFFFFFF)
      if msg is not None and m.group(2) in msg.signals:
        msg.signals[m.group(2)].comment = " ".join(m.group(3).split())
    for m in CM_BO_RE.finditer(text):
      msg = self.messages.get(int(m.group(1)) & 0x1FFFFFFF)
      if msg is not None:
        msg.comment = " ".join(m.group(2).split())
    for m in CYCLE_RE.finditer(text):
      msg = self.messages.get(int(m.group(1)) & 0x1FFFFFFF)
      if msg is not None:
        msg.cycle_ms = int(m.group(2))

    for msg in self.messages.values():
      msg.finalize()

  def decode(self, address: int, data: bytes) -> dict[str, float] | None:
    msg = self.messages.get(address)
    return msg.decode(data) if msg is not None else None
