"""Amendment §3–§5 – sensors, composite measurements, working conditions and behavior models."""

import math

import pytest

from conftest import basic_sim, port
from industrial_helpers import attribute_sim, furnace_line, furnace_type
from simulator.errors import ValidationError
from simulator.industrial.expressions import EvaluationError, MissingValue, compile_expression
from simulator.industrial.units import convert


class Env:
    def __init__(self, **values):
        self.values = values

    def get(self, name, default=None):
        if name not in self.values:
            raise MissingValue(name)
        return self.values[name]


# ---------------------------------------------------------------- expression language & units
def test_expression_language_is_safe_and_typed():
    e = compile_expression("clamp(sensor.temp / param.sp, 0, 1) if sensor.temp > 0 else 0")
    assert e.variables == {"sensor.temp", "param.sp"}
    assert e.evaluate(Env(**{"sensor.temp": 450, "param.sp": 900})) == pytest.approx(0.5)
    with pytest.raises(MissingValue):
        e.evaluate(Env(**{"param.sp": 900}))
    with pytest.raises(EvaluationError):
        compile_expression("1 / x").evaluate(Env(x=0))
    for bad in ("__import__('os')", "x.__class__", "open('f')", "[i for i in x]", "lambda: 1", "x[0]"):
        with pytest.raises(ValidationError):
            compile_expression(bad)


def test_unit_conversion():
    assert convert(1500, "degF", "degC") == pytest.approx(815.5556, rel=1e-4)
    assert convert(2, "bar", "kPa") == pytest.approx(200)
    assert convert(5, "degC", "degC") == 5
    with pytest.raises(ValidationError):
        convert(1, "bar", "degC")


# ---------------------------------------------------------------- sensors & composites
def run_furnace(seed=1, until=400):
    sim = furnace_line(attribute_sim(seed))
    sim.start()
    sim.run_until(until)
    return sim


def test_simulated_sensors_follow_their_models_and_feed_composites():
    sim = run_furnace()
    hub = sim.industrial
    temp = hub.store.query(asset_id="t", measurement_id="temp", limit=10_000)
    assert len(temp) >= 70
    assert temp[0].value < 100 and temp[-1].value == pytest.approx(900, abs=10)   # first-order approach
    assert all(o.unit == "degC" and o.quality == "good" and o.source == "simulated" for o in temp)
    assert hub.store.latest_value("t", "pressure") == pytest.approx(2.0)
    health = hub.store.latest_value("t", "health")
    assert health == pytest.approx(min(1.0, hub.store.latest_value("t", "temp") / 900), abs=1e-6)


def test_sensor_validation_and_missing_values():
    sim = basic_sim()
    hub = sim.industrial
    with pytest.raises(ValidationError):
        hub.create_type({"id": "x", "name": "x", "kind": "transformer",
                         "profile": {"sensors": [{"id": "t", "min": 10, "max": 0}]}})
    with pytest.raises(ValidationError):
        hub.create_type({"id": "x", "name": "x", "kind": "transformer",
                         "profile": {"sensors": [{"id": "bad id"}]}})
    with pytest.raises(ValidationError):
        hub.create_type({"id": "x", "name": "x", "kind": "transformer",
                         "profile": {"sensors": [{"id": "t"}, {"id": "t"}]}})
    # a composite over a sensor with no model has no value (missing input) rather than raising
    hub.create_type({"id": "x", "name": "x", "kind": "producer", "config": {"product_type_id": "ore"},
                     "profile": {"sensors": [{"id": "ext", "source": "external"}],
                                 "composites": [{"id": "twice", "expression": "sensor.ext * 2"},
                                                {"id": "safe", "expression": "sensor.ext * 2", "missing": "default",
                                                 "default": -1}]}})
    hub.deploy("x", "P", asset_id="p")
    sim.add_asset("sink", "S", {}, asset_id="s")
    sim.add_conveyor("c", port("p", "out"), port("s", "in0"), {}, asset_id="c")
    sim.start()
    sim.run_until(50)
    d = {c["id"]: c for c in hub.describe("p")["composites"]}
    assert d["twice"]["value"] is None and d["safe"]["value"] == -1
    assert not hub.describe("p")["evaluation_errors"]


def test_composite_cycles_are_rejected():
    sim = basic_sim()
    with pytest.raises(ValidationError, match="cycle"):
        sim.industrial.create_type({"id": "x", "name": "x", "kind": "transformer", "profile": {"composites": [
            {"id": "a", "expression": "composite.c"}, {"id": "b", "expression": "composite.a"},
            {"id": "c", "expression": "composite.b"}]}})


def test_cycle_measurement_aggregation_is_recorded_on_cycles():
    sim = run_furnace()
    cyc = [c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"]
    assert cyc
    m = cyc[-1].measurements["temp"]
    assert m["count"] >= 2 and m["min"] <= m["avg"] <= m["max"]
    assert {"initial", "final"} <= set(m)


# ---------------------------------------------------------------- working conditions
def test_condition_thresholds_hysteresis_and_effects():
    sim = run_furnace()
    hub = sim.industrial
    log = hub.condition_log(asset_id="t")
    assert [r["active"] for r in log] == [True, False]       # cold at start, cleared once warm
    on, off = log
    assert on["measurements"]["sensor.temp"] < 500 and off["measurements"]["sensor.temp"] > 520
    assert on["severity"] == "warning" and on["classification"] == "warning"
    assert hub.classification("t") == "normal"
    cycles = sorted((c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"),
                    key=lambda c: c.started_at)
    cold = [c for c in cycles if c.started_at < off["time"]]
    warm = [c for c in cycles if c.started_at >= off["time"]]
    assert cold and warm
    base = cycles[-1].config_snapshot["industrial"]["features"]["processing_time_s"]
    assert cold[0].ended_at - cold[0].started_at == pytest.approx(
        cold[0].config_snapshot["industrial"]["features"]["processing_time_s"])
    cold_feat = cold[0].config_snapshot["industrial"]
    assert "cold" in cold_feat["working_conditions"]
    assert any(a.get("condition") == "cold" and a["op"] == "multiply" for a in cold_feat["applied"])
    assert not warm[-1].config_snapshot["industrial"]["working_conditions"]
    assert base > 0
    ev = [e.event_type for e in sim.query_events(asset_id="t")]
    assert "condition_activated" in ev and "condition_cleared" in ev
    hist = sim.operational_history_for("t")
    assert any(i["conditions"].get("working_conditions") == ["cold"] for i in hist)


def test_condition_delay_and_configurable_severity():
    sim = attribute_sim()
    prof = {"parameters": [{"name": "limit", "type": "number", "default": 100}],
            "sensors": [{"id": "temp", "model": {"type": "expression", "expression": "t"},
                         "sample_interval_s": 1}],
            "conditions": [{"id": "hot", "severity": "critical", "activation": "sensor.temp > param.limit",
                            "delay_on_s": 10, "action": "block"}]}
    furnace_type(sim, profile=prof, config={})
    furnace_line(sim)
    sim.start()
    sim.run_until(105)
    assert sim.industrial.classification("t") == "normal"      # threshold crossed but delay not elapsed
    sim.run_until(115)
    assert sim.industrial.classification("t") == "critical"
    assert sim.industrial.blocking_condition("t") == "hot"
    at = sim.industrial.condition_log(asset_id="t")[0]["time"]
    assert at == pytest.approx(111, abs=1.01)
    started_after = [c for c in sim.cycles.values() if c.asset_id == "t" and c.started_at > at + 1e-6]
    sim.run_until(300)
    started_after = [c for c in sim.cycles.values() if c.asset_id == "t" and c.started_at > at + 30]
    assert not started_after                                   # blocking condition prevents new cycles
    # raising the limit through an instance parameter clears the condition and production resumes
    sim.industrial.set_parameters("t", {"limit": 10_000})
    sim.run_until(400)
    assert sim.industrial.classification("t") == "normal"
    assert [c for c in sim.cycles.values() if c.asset_id == "t" and c.started_at > 300]
    assert not sim.check_invariants()


def test_stop_action_stops_and_restarts_asset():
    sim = attribute_sim()
    prof = {"sensors": [{"id": "temp", "model": {"type": "expression", "expression": "200 if 50 < t < 80 else 20"},
                         "sample_interval_s": 1}],
            "conditions": [{"id": "hot", "severity": "critical", "activation": "sensor.temp > 100",
                            "action": "stop"}]}
    furnace_type(sim, profile=prof, config={})
    furnace_line(sim)
    sim.start()
    sim.run_until(65)
    assert sim.assets["t"].state == "stopped"
    sim.run_until(100)
    assert sim.assets["t"].state != "stopped"


# ---------------------------------------------------------------- behavior models
def test_behavior_maps_parameters_to_features_and_is_snapshotted():
    sim = attribute_sim()
    prof = {"parameters": [{"name": "power", "type": "number", "default": 50, "min": 0, "max": 100}],
            "behaviors": [{"id": "speed_from_power", "output": "speed_pct", "expression": "param.power * 2",
                           "min": 10, "max": 100, "version": 3},
                          {"id": "quality", "output": "quality_score", "expression": "self - 0.1"}]}
    furnace_type(sim, profile=prof, config={})
    furnace_line(sim)
    sim.start()
    sim.run_until(100)
    snap = [c for c in sim.cycles.values() if c.asset_id == "t"][0].config_snapshot["industrial"]
    assert snap["features"]["speed_pct"] == pytest.approx(100)
    assert snap["parameters"] == {"power": 50}
    applied = {a["behavior"]: a for a in snap["applied"] if "behavior" in a}
    assert applied["speed_from_power"]["version"] == 3 and applied["speed_from_power"]["new"] == 100
    assert applied["quality"]["new"] == pytest.approx(applied["quality"]["old"] - 0.1)
    assert snap["asset_type"] == {"id": "furnace", "version": 1}
    sim.industrial.set_parameters("t", {"power": 20})
    sim.run_until(300)
    last = sorted((c for c in sim.cycles.values() if c.asset_id == "t"), key=lambda c: c.started_at)[-1]
    assert last.config_snapshot["industrial"]["features"]["speed_pct"] == pytest.approx(40)
    assert last.ended_at is None or last.ended_at - last.started_at > 30   # lower speed -> longer cycle


def test_behavior_invalid_inputs_fall_back_to_nominal():
    sim = attribute_sim()
    prof = {"parameters": [{"name": "power", "type": "number", "default": 50}],
            "sensors": [{"id": "ext", "source": "external"}],
            "behaviors": [{"id": "from_sensor", "output": "speed_pct", "expression": "sensor.ext"},
                          {"id": "ranged", "output": "quality_score", "expression": "0.1",
                           "input_ranges": {"param.power": [60, 100]}},
                          {"id": "div0", "output": "time_multiplier", "expression": "1 / (param.power - 50)"}]}
    furnace_type(sim, profile=prof, config={"speed_pct": 70})
    furnace_line(sim)
    sim.start()
    sim.run_until(60)
    snap = [c for c in sim.cycles.values() if c.asset_id == "t"][0].config_snapshot["industrial"]
    assert snap["features"]["speed_pct"] == 70
    skipped = {a["behavior"]: a for a in snap["applied"] if a.get("skipped")}
    assert set(skipped) == {"from_sensor", "ranged", "div0"}
    assert "outside valid range" in skipped["ranged"]["reason"]
    assert sim.industrial.describe("t")["evaluation_errors"]
    assert math.isfinite(snap["features"]["processing_time_s"])
