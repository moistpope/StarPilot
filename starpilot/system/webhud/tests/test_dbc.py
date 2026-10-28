"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import random

from openpilot.starpilot.system.webhud.dbc import DBC
from openpilot.starpilot.system.webhud.paths import DBC_PATH

TEXT = '''
BO_ 291 TEST_0x123: 8 ADAS
 SG_ BE_U8 : 7|8@0+ (1,0) [0|255] ""  GW
 SG_ BE_S12 : 11|12@0- (0.5,-10) [-1034|1013.5] "m"  GW
 SG_ LE_U4 : 32|4@1+ (1,0) [0|15] ""  GW
 SG_ LE_S16 : 40|16@1- (0.01,0) [-327.68|327.67] "deg"  GW

BO_ 2147484160 EXT_MSG: 8 GW
 SG_ X : 0|8@1+ (1,0) [0|255] ""  ADAS

CM_ BO_ 291 "a test message";
CM_ SG_ 291 BE_U8 "first byte,
spanning lines";
BA_ "GenMsgCycleTime" BO_ 291 20;
VAL_ 291 LE_U4 0 "Off" 1 "On_with_Visual" 15 "Fault" ;
'''


class TestDbc:
  def test_parse_metadata(self):
    dbc = DBC(text=TEXT)
    msg = dbc.by_name["TEST_0x123"]
    assert msg.address == 0x123 and msg.size == 8 and msg.transmitter == "ADAS"
    assert msg.comment == "a test message" and msg.cycle_ms == 20
    assert msg.signals["BE_U8"].comment == "first byte, spanning lines"
    assert msg.signals["LE_U4"].values == {0: "Off", 1: "On_with_Visual", 15: "Fault"}
    assert msg.signals["BE_S12"].unit == "m"
    # extended frame flag (bit 31) is stripped from the address
    assert 0x200 in dbc.messages

  def test_decode_known_frame(self):
    dbc = DBC(text=TEXT)
    msg = dbc.by_name["TEST_0x123"]
    # BE_U8 = byte0; BE_S12 = low nibble of byte1 + byte2 (msb first); LE_U4 = low nibble byte4; LE_S16 = bytes5..6 LE
    data = bytes([0xAB, 0x0F, 0xFE, 0x00, 0x05, 0x18, 0xFC, 0x00])
    out = msg.decode(data)
    assert out["BE_U8"] == 0xAB
    assert out["BE_S12"] == (0xFFE - 0x1000) * 0.5 - 10   # raw -2 -> -11
    assert out["LE_U4"] == 5
    assert abs(out["LE_S16"] - (0xFC18 - 0x10000) * 0.01) < 1e-9   # -10.0
    assert msg.signals["LE_U4"].describe(out["LE_U4"]) is None
    assert msg.signals["LE_U4"].describe(1) == "On_with_Visual"

  def test_short_frame_is_zero_padded(self):
    msg = DBC(text=TEXT).by_name["TEST_0x123"]
    assert msg.decode(b"\x12")["BE_U8"] == 0x12

  def test_encode_roundtrip_world_dbc(self):
    dbc = DBC(DBC_PATH)
    assert len(dbc.messages) > 80
    rng = random.Random(0)
    for msg in dbc.messages.values():
      for _ in range(3):
        data = bytes(rng.getrandbits(8) for _ in range(msg.size))
        values = msg.decode(data)
        again = msg.decode(msg.encode(values))
        for name, v in values.items():
          assert abs(again[name] - v) < 1e-6, (msg.name, name)

  def test_encode_preserves_unlisted_bits(self):
    msg = DBC(text=TEXT).by_name["TEST_0x123"]
    base = bytes([0xAB, 0x0F, 0xFE, 0x00, 0x05, 0x18, 0xFC, 0x77])
    out = msg.encode({"LE_U4": 9}, base=base)
    decoded = msg.decode(out)
    assert decoded["LE_U4"] == 9 and decoded["BE_U8"] == 0xAB and out[7] == 0x77
