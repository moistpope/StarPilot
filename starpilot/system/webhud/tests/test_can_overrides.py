"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import os
import tempfile
from dataclasses import replace

from opendbc.car.fisker import carcontroller, fiskercan, values

from openpilot.selfdrive.car.can_overrides import PARAM, CanOverrides, OverrideStore


class FakeParams:
  def __init__(self):
    self.store = {}

  def get(self, key, return_default=False):
    return self.store.get(key)


class TestCanOverrides:
  def setup_method(self):
    self.saved = dict(values.ICC_0x35B_OVERRIDES)
    self.saved_52a = dict(values.ICC_0x52A_OVERRIDES)
    self.ov = CanOverrides.for_brand("fisker")

  def teardown_method(self):
    values.ICC_0x35B_OVERRIDES.clear()
    values.ICC_0x35B_OVERRIDES.update(self.saved)
    values.ICC_0x52A_OVERRIDES.clear()
    values.ICC_0x52A_OVERRIDES.update(self.saved_52a)

  def test_unsupported_brand(self):
    assert CanOverrides.for_brand("toyota") is None
    assert CanOverrides.for_brand(None) is None

  def test_tables(self):
    # 0x52A: the ACC settings the spoof applies (the HUD's Assist settings); 0x35B: park assist, BSD and the rest
    assert [t.message for t in self.ov.tables] == ["ICC_0x52A", "ICC_0x35B"]
    assert self.ov.passthrough(self.ov.tables[0]) == values.ICC_0x52A_PASSTHROUGH
    assert self.ov.passthrough(self.ov.tables[1]) == values.ICC_0x35B_PASSTHROUGH

  def test_defaults_are_the_code_values(self):
    assert self.ov.defaults == {"ICC_0x52A": self.saved_52a, "ICC_0x35B": self.saved}
    assert self.ov.live() == {"ICC_0x52A": self.saved_52a, "ICC_0x35B": self.saved}
    assert self.saved_52a == {"ICCACCSwt": 1, "ICCACCFuncTyp": 2, "ICCACCSpdStepSize": 1, "ICCLaneTrajectorySetting": 1}

  def test_validate(self):
    clean, errors = self.ov.validate({
      "ICC_0x35B": {"ICC_BSDSetting": 2, "ICC_APA_Setting": 99, "ICC_BSD_Sensitivity": 1.5, "ICC_ViewReq": 2,
                    "ICC_DOW_Setting": True, "NotASignal": 1},
      "ICC_0x52A": {"ICCACCSpdLimOffs": 7.5, "ICCACCSpdLimOffsTyp": 2, "ICCACCSwt": 4, "ICCACCTiGapCfm": 1, "ICC_0x52AAliveCounter": 3},
      "ADAS_0x31C": {"ADAS_TiGapSet_ACC": 1},
    })
    assert clean == {"ICC_0x35B": {"ICC_BSDSetting": 2}, "ICC_0x52A": {"ICCACCSpdLimOffs": 7.5, "ICCACCSpdLimOffsTyp": 2}}
    text = " | ".join(errors)
    for needle in ("ICCACCSwt: 4 outside", "ICCACCTiGapCfm: always passed through", "ICC_0x52AAliveCounter: always passed through",
                   "ICC_APA_Setting: 99 outside", "ICC_BSD_Sensitivity: 1.5 outside", "ICC_ViewReq: always passed through",
                   "ICC_DOW_Setting: value must be a number", "NotASignal: unknown signal",
                   "ADAS_0x31C: not an overridable message"):
      assert needle in text, needle

  def test_protected_suffixes_are_locked(self):
    # 0x35B carries no E2E, so graft a counter and a checksum onto this instance's parsed copy of the
    # message: the rule has to hold for the next table that does have them
    msg = self.ov.dbc.by_name["ICC_0x35B"]
    grafted = ("ICC_0x35B_AliveCounter", "ICC_0x35B_CheckSum")
    for name in grafted:
      msg.signals[name] = replace(msg.signals["ICC_BSDSetting"], name=name)
    try:
      clean, errors = self.ov.validate({"ICC_0x35B": {"ICC_0x35B_AliveCounter": 1, "ICC_0x35B_CheckSum": 1, "ICC_BSDSetting": 1}})
    finally:
      for name in grafted:
        msg.signals.pop(name)
    assert clean == {"ICC_0x35B": {"ICC_BSDSetting": 1}}
    text = " | ".join(errors)
    for name in grafted:
      assert f"{name}: always passed through" in text, name

  def test_resolve_replaces_per_message(self):
    acc = self.saved_52a
    # stored message fully replaces defaults
    assert self.ov.resolve({"ICC_0x35B": {"ICC_BSDSetting": 3}}) == {"ICC_0x52A": acc, "ICC_0x35B": {"ICC_BSDSetting": 3}}
    assert self.ov.resolve({"ICC_0x35B": {}}) == {"ICC_0x52A": acc, "ICC_0x35B": {}}                    # ... down to nothing: frame left alone
    assert self.ov.resolve(None) == {"ICC_0x52A": acc, "ICC_0x35B": self.saved}                         # missing message keeps code defaults
    assert self.ov.resolve({"ICC_0x52A": {"ICCACCSwt": 2}}) == {"ICC_0x52A": {"ICCACCSwt": 2}, "ICC_0x35B": self.saved}
    assert self.ov.resolve({"ADAS_0x31C": {"ADAS_TiGapSet_ACC": 1}}) == {"ICC_0x52A": acc, "ICC_0x35B": self.saved}   # unknown message is ignored

  def test_apply_mutates_the_dict_carcontroller_reads(self):
    target = values.ICC_0x35B_OVERRIDES
    assert fiskercan.ICC_0x35B_OVERRIDES is target and carcontroller.ICC_0x35B_OVERRIDES is target
    self.ov.apply({"ICC_0x35B": {"ICC_BSDSetting": 2, "ICC_DOW_Setting": 2}})
    assert fiskercan.ICC_0x35B_OVERRIDES is target
    assert target == {"ICC_BSDSetting": 2, "ICC_DOW_Setting": 2}
    self.ov.apply({"ICC_0x35B": {}})
    assert target == {} and not carcontroller.ICC_0x35B_OVERRIDES   # empty: carcontroller stops relaying 0x35B
    acc = values.ICC_0x52A_OVERRIDES
    assert fiskercan.ICC_0x52A_OVERRIDES is acc
    self.ov.apply({"ICC_0x52A": {"ICCACCSwt": 2}})
    assert fiskercan.ICC_0x52A_OVERRIDES is acc and acc == {"ICCACCSwt": 2}

  def test_poll_applies_only_on_change(self):
    params = FakeParams()
    assert self.ov.poll(params)              # first poll applies defaults
    assert not self.ov.poll(params)
    params.store[PARAM] = {"ICC_0x35B": {"ICC_BSDSetting": 3}}
    assert self.ov.poll(params)
    assert values.ICC_0x35B_OVERRIDES == {"ICC_BSDSetting": 3}
    assert not self.ov.poll(params)
    del params.store[PARAM]
    assert self.ov.poll(params)
    assert values.ICC_0x35B_OVERRIDES == self.saved

  def test_poll_from_the_store_file(self):
    # StarPilot keeps the overrides in a JSON file (no new params in a prebuilt tree): card polls it like a param
    with tempfile.TemporaryDirectory() as d:
      store = OverrideStore(os.path.join(d, "fisker", "can_overrides.json"))
      assert store.get(PARAM) is None
      assert self.ov.poll(store)                # nothing stored: the code defaults
      store.put(PARAM, {"ICC_0x35B": {"ICC_BSDSetting": 3}})
      assert store.get(PARAM) == {"ICC_0x35B": {"ICC_BSDSetting": 3}}
      assert self.ov.poll(store)
      assert values.ICC_0x35B_OVERRIDES == {"ICC_BSDSetting": 3}
      assert not self.ov.poll(store)
      with open(store.path, "w") as f:
        f.write("{not json")                    # unreadable: the code defaults again
      assert self.ov.poll(store)
      assert values.ICC_0x35B_OVERRIDES == self.saved
      store.remove(PARAM)
      store.remove(PARAM)                       # removing what isn't there is fine
      assert not os.path.exists(store.path)

  def test_bad_param_falls_back_to_defaults(self):
    params = FakeParams()
    params.store[PARAM] = {"ICC_0x35B": {"ICC_BSDSetting": 3}}
    self.ov.poll(params)
    params.store[PARAM] = {"ICC_0x35B": "garbage"}   # malformed entry is dropped -> code defaults
    self.ov.poll(params)
    assert values.ICC_0x35B_OVERRIDES == self.saved
    params.store[PARAM] = {"ICC_0x35B": {"ICC_BSDSetting": 3}}
    self.ov.poll(params)
    params.store[PARAM] = ["not", "a", "dict"]
    self.ov.poll(params)
    assert values.ICC_0x35B_OVERRIDES == self.saved

  def test_overridden_frame_packs(self):
    from opendbc.can import CANPacker
    from opendbc.car import Bus
    from opendbc.car.fisker.values import CAR, DBC
    self.ov.apply(self.ov.resolve({"ICC_0x35B": {"ICC_BSDSetting": 3, "ICC_EnbLnChgAsst": 0}}))
    can = fiskercan.FiskerCAN(None, CANPacker(DBC[CAR.FISKER_OCEAN][Bus.pt]))
    msg = self.ov.dbc.by_name["ICC_0x35B"]
    # one decoded ICC frame off bus 0: a live camera-view request, undocumented bits set, our two signals at other values
    icc = dict.fromkeys(msg.signals, 0)
    icc.update({"ICC_0x35B_Undoc_Byte0": 0xA5, "ICC_ViewReq": 4, "ICC_BSDSetting": 0, "ICC_EnbLnChgAsst": 1, "ICC_APA_Setting": 1})
    addr, data, bus = can.create_icc_0x35b(icc)
    assert addr == 0x35B and bus == values.CANBUS.cam == 2 and len(data) == 8
    out = msg.decode(data)
    assert out["ICC_BSDSetting"] == 3 and out["ICC_EnbLnChgAsst"] == 0                                   # overrides win
    assert out["ICC_ViewReq"] == 4 and out["ICC_0x35B_Undoc_Byte0"] == 0xA5 and out["ICC_APA_Setting"] == 1   # the rest is the ICC's

  def test_acc_spoof_packs_overrides(self):
    from opendbc.can import CANPacker
    from opendbc.car import Bus
    from opendbc.car.fisker.values import CAR, DBC
    can = fiskercan.FiskerCAN(None, CANPacker(DBC[CAR.FISKER_OCEAN][Bus.pt]))
    msg = self.ov.dbc.by_name["ICC_0x52A"]
    icc = dict.fromkeys(msg.signals, 0)
    icc.update({"ICC_0x52AAliveCounter": 9, "ICCACCSpdLimOffs": 2.5, "ICCACCSpdLimOffsTyp": 1, "ICCACCTiGapCfm": 1, "ICC_LKASetting": 2})

    out = msg.decode(can.create_icc_spoof_0x52a(icc)[1])     # code defaults: ACC on, the ICC's offset
    assert (out["ICCACCSwt"], out["ICCACCFuncTyp"], out["ICCACCAutoSpdSts"], out["ICCACCTerrainSetting"]) == (1, 2, 1, 0)
    assert (out["ICCACCSpdLimOffs"], out["ICCACCSpdLimOffsTyp"]) == (2.5, 1)

    # what the HUD's Assist settings store: ACC off, auto speed off over the toggle, terrain on, +7.5 fixed
    self.ov.apply(self.ov.resolve({"ICC_0x52A": {**self.saved_52a, "ICCACCSwt": 2, "ICCACCAutoSpdSts": 0, "ICCACCTerrainSetting": 1,
                                                 "ICCACCSpdLimOffsTyp": 2, "ICCACCSpdLimOffs": 7.5}}))
    out = msg.decode(can.create_icc_spoof_0x52a(icc, acc_auto_speed=True, terrain_setting=False)[1])
    assert (out["ICCACCSwt"], out["ICCACCAutoSpdSts"], out["ICCACCTerrainSetting"]) == (2, 0, 1)
    assert (out["ICCACCSpdLimOffs"], out["ICCACCSpdLimOffsTyp"]) == (7.5, 2)
    assert (out["ICC_0x52AAliveCounter"], out["ICCACCTiGapCfm"], out["ICC_LKASetting"]) == (9, 1, 2)   # the rest is the ICC's
