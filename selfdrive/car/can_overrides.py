"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Runtime-editable CAN signal overrides.

Some ports relay a vehicle frame with a few signals replaced (Fisker: the ICC's feature-settings
frames re-sent to the ADAS module, see opendbc/car/fisker/values.py). The replacement values live in
module-level dicts that the car controller reads every frame. This module lets the web HUD edit them
without a code change: edits are stored in a JSON file (OverrideStore), validated against the DBC, and card
applies them to those same dict objects in place, so carcontroller/fiskercan pick them up on the next frame.

Stored format (JSON): {"<DBC message>": {"<signal>": <physical value>, ...}, ...}. A message present
in the param fully replaces the code defaults for that message (so a default can be removed); a
message missing from the param uses the code defaults.
"""
import importlib
import json
import os
import threading
from dataclasses import dataclass

from openpilot.common.swaglog import cloudlog

PARAM = "FiskerCanOverrides"   # the key the overrides are stored under (see OverrideStore)
PROTECTED_SUFFIXES = ("CheckSum", "AliveCounter")


@dataclass(frozen=True)
class OverrideTable:
  message: str                  # DBC message name
  attr: str                     # dict attribute in the values module
  passthrough_attr: str | None  # frozenset of signals that must never be overridden
  description: str


BRANDS: dict[str, tuple[str, str, tuple[OverrideTable, ...]]] = {
  # brand: (values module, opendbc DBC name, tables)
  "fisker": ("opendbc.car.fisker.values", "fisker_ocean_adas", (
    OverrideTable("ICC_0x52A", "ICC_0x52A_OVERRIDES", "ICC_0x52A_PASSTHROUGH",
                  "ACC and driver-assist settings, re-sent to the ADAS module on every ICC frame. The ACC switch, type, " +
                  "step and trajectory defaults are what enable ACC. Auto speed and terrain follow the port's baseline " +
                  "(auto speed on, terrain off) unless set here."),
    OverrideTable("ICC_0x35B", "ICC_0x35B_OVERRIDES", "ICC_0x35B_PASSTHROUGH",
                  "Surround view, BSD/DOW, park assist and auto high beam settings. With no overrides openpilot leaves " +
                  "this frame alone; camera-view requests always pass through."),
  )),
}

# code defaults, captured the first time a brand is loaded in this process (before any apply)
_DEFAULTS: dict[str, dict[str, dict[str, float]]] = {}


def _dbc_path(dbc_name: str) -> str:
  from opendbc import DBC_PATH
  return os.path.join(DBC_PATH, dbc_name + ".dbc")


class CanOverrides:
  def __init__(self, brand: str):
    module_name, dbc_name, tables = BRANDS[brand]
    self.brand = brand
    self.module = importlib.import_module(module_name)
    self.tables = tables
    self.dbc_name = dbc_name
    self._dbc = None
    if brand not in _DEFAULTS:
      _DEFAULTS[brand] = {t.message: dict(getattr(self.module, t.attr)) for t in tables}
    self.defaults = _DEFAULTS[brand]
    self._last_raw: object = object()  # sentinel: nothing applied yet

  @classmethod
  def for_brand(cls, brand: str | None) -> "CanOverrides | None":
    return cls(brand) if brand in BRANDS else None

  @property
  def dbc(self):
    if self._dbc is None:
      from openpilot.starpilot.system.webhud.dbc import DBC
      self._dbc = DBC(_dbc_path(self.dbc_name))
    return self._dbc

  def table(self, message: str) -> OverrideTable | None:
    return next((t for t in self.tables if t.message == message), None)

  def passthrough(self, table: OverrideTable) -> frozenset[str]:
    return frozenset(getattr(self.module, table.passthrough_attr)) if table.passthrough_attr else frozenset()

  def live(self) -> dict[str, dict[str, float]]:
    """The dicts carcontroller is using right now."""
    return {t.message: dict(getattr(self.module, t.attr)) for t in self.tables}

  def validate(self, overrides: dict) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Drop anything that isn't a settable signal with an in-range value. Returns (clean, errors)."""
    clean: dict[str, dict[str, float]] = {}
    errors: list[str] = []
    if not isinstance(overrides, dict):
      return clean, ["overrides must be an object"]
    for message, signals in overrides.items():
      table = self.table(message)
      msg = self.dbc.by_name.get(message)
      if table is None or msg is None:
        errors.append(f"{message}: not an overridable message")
        continue
      if not isinstance(signals, dict):
        errors.append(f"{message}: expected an object of signal values")
        continue
      blocked = self.passthrough(table)
      out = {}
      for name, value in signals.items():
        sig = msg.signals.get(name)
        if sig is None:
          errors.append(f"{message}.{name}: unknown signal")
        elif name.endswith(PROTECTED_SUFFIXES) or name in blocked:
          errors.append(f"{message}.{name}: always passed through")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
          errors.append(f"{message}.{name}: value must be a number")
        else:
          lo, hi = signal_range(sig)
          raw = (value - sig.offset) / sig.factor
          if not (lo - 1e-9 <= value <= hi + 1e-9) or abs(raw - round(raw)) > 1e-6:
            errors.append(f"{message}.{name}: {value} outside {lo}..{hi} (step {sig.factor:g})")
          else:
            out[name] = int(value) if sig.factor == 1 and sig.offset == 0 else float(value)
      clean[message] = out
    return clean, errors

  def resolve(self, stored: dict | None) -> dict[str, dict[str, float]]:
    """Effective overrides: per message, the stored (validated) dict or the code defaults."""
    stored_clean, _ = self.validate(stored) if stored else ({}, [])
    return {t.message: stored_clean.get(t.message, self.defaults[t.message]) for t in self.tables}

  def apply(self, resolved: dict[str, dict[str, float]]) -> None:
    """Mutate the module dicts in place. Each step is one GIL-atomic dict op, so the controller
    thread never sees a half-copied dict -- at worst one frame with only removals applied."""
    for t in self.tables:
      target = getattr(self.module, t.attr)
      new = resolved.get(t.message, {})
      for k in [k for k in target if k not in new]:
        target.pop(k, None)
      target.update(new)

  def poll(self, params) -> bool:
    """Re-apply when the stored overrides changed. Cheap enough for card's 10 Hz params thread."""
    raw = params.get(PARAM)
    if raw == self._last_raw:
      return False
    self._last_raw = raw
    try:
      resolved = self.resolve(raw)
      self.apply(resolved)
    except Exception:
      cloudlog.exception(f"can overrides: failed to apply {raw}")
      return False
    cloudlog.warning(f"can overrides applied ({self.brand}): {resolved}")
    return True


class OverrideStore:
  """Where the overrides are kept: a JSON file, with the get/put/remove subset of Params that card and the web
  HUD use for PARAM. StarPilot ships prebuilt, and a new params key would need a rebuilt params_pyx.so."""

  def __init__(self, path: str | None = None):
    if path is None:
      from openpilot.system.hardware import PC
      from openpilot.system.hardware.hw import Paths
      path = os.path.join(Paths.comma_home() if PC else "/data", "fisker", "can_overrides.json")
    self.path = path
    self.lock = threading.Lock()

  def get(self, key: str = PARAM, return_default: bool = False):
    assert key == PARAM
    try:
      with open(self.path) as f:
        return json.load(f)
    except (OSError, ValueError):   # none stored yet, or unreadable: the code defaults apply
      return None

  def put(self, key: str, value) -> None:
    assert key == PARAM
    with self.lock:
      os.makedirs(os.path.dirname(self.path), exist_ok=True)
      tmp = f"{self.path}.tmp"
      with open(tmp, "w") as f:
        json.dump(value, f)
      os.replace(tmp, self.path)   # card never reads a half-written file

  def remove(self, key: str) -> None:
    assert key == PARAM
    with self.lock:
      try:
        os.unlink(self.path)
      except FileNotFoundError:
        pass


def signal_range(sig) -> tuple[float, float]:
  """Physical min/max representable by the signal's bits."""
  if sig.signed:
    raw_lo, raw_hi = -(1 << (sig.size - 1)), (1 << (sig.size - 1)) - 1
  else:
    raw_lo, raw_hi = 0, (1 << sig.size) - 1
  a, b = raw_lo * sig.factor + sig.offset, raw_hi * sig.factor + sig.offset
  return (min(a, b), max(a, b))
