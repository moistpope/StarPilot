"""
The StarPilot services the bridge streams, under the sunnypilot names and shapes the Oceanfront View page reads.
"""
from cereal import custom
from cereal.services import SERVICE_LIST
from openpilot.starpilot.system.webhud.extract import EXTRACTORS, OP_NAMES, SERVICES
from openpilot.starpilot.system.webhud.state import StreamBuilder


class TestExtract:
  def test_services_exist(self):
    # SubMaster raises on a service this cereal doesn't have
    assert set(SERVICES) <= set(SERVICE_LIST)

  def test_always_on_lateral_as_mads(self, subtests):
    cases = [
      (dict(), "disabled", False, False),
      (dict(alwaysOnLateralAllowed=True), "disabled", False, False),
      (dict(alwaysOnLateralAllowed=True, alwaysOnLateralEnabled=True), "enabled", True, True),
      (dict(alwaysOnLateralAllowed=True, alwaysOnLateralEnabled=True, pauseLateral=True), "paused", True, False),
    ]
    for fields, state, enabled, active in cases:
      with subtests.test(**fields):
        mads = EXTRACTORS["starpilotCarState"](custom.StarPilotCarState.new_message(**fields))["mads"]
        assert mads == {"state": state, "enabled": enabled, "active": active,
                        "available": bool(fields.get("alwaysOnLateralAllowed"))}

  def test_speed_limit_controller(self):
    sl = EXTRACTORS["starpilotPlan"](custom.StarPilotPlan.new_message(
      slcSpeedLimit=20.1168, slcSpeedLimitOffset=2.0, slcSpeedLimitSource="Map Data"))["speedLimit"]
    assert sl == {"value": 20.12, "valid": True, "offset": 2.0, "source": "Map Data", "distance": None}
    sl = EXTRACTORS["starpilotPlan"](custom.StarPilotPlan.new_message())["speedLimit"]
    assert sl["valid"] is False and sl["source"] is None

  def test_mapd(self):
    m = EXTRACTORS["mapdOut"](custom.MapdOut.new_message(
      roadName="Pacific Coast Hwy", speedLimit=24.5872, nextSpeedLimit=29.0576, nextSpeedLimitDistance=412.4))
    assert m == {"speedLimit": 24.59, "speedLimitAhead": 29.06, "speedLimitAheadDistance": 412.0,
                 "roadName": "Pacific Coast Hwy"}
    m = EXTRACTORS["mapdOut"](custom.MapdOut.new_message())
    assert m["speedLimit"] is None and m["speedLimitAhead"] is None and m["roadName"] == ""

  def test_streamed_under_page_names(self):
    builder = StreamBuilder()
    builder.feed_service("starpilotCarState", custom.StarPilotCarState.new_message(alwaysOnLateralEnabled=True), 1.0)
    builder.feed_service("mapdOut", custom.MapdOut.new_message(roadName="Main St"), 1.0)
    names = [name for name, _, _ in builder.take_tick(1.0)["op"]]
    assert names == [OP_NAMES["starpilotCarState"], OP_NAMES["mapdOut"]] == ["selfdriveStateSP", "liveMapDataSP"]
