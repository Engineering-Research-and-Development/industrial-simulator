"""Shared builders for the industrial sensing / synchronisation / dataset tests."""

from conftest import basic_sim, port

FURNACE_PROFILE = {
    "parameters": [{"name": "setpoint", "type": "number", "unit": "degC", "default": 900, "min": 0, "max": 2000}],
    "sensors": [
        {"id": "temp", "unit": "degC", "external_key": "TT101", "sample_interval_s": 5,
         "model": {"type": "first_order", "target": "param.setpoint", "time_constant_s": 20, "initial": 20}},
        {"id": "pressure", "unit": "bar", "model": {"type": "constant", "value": 2.0}, "sample_interval_s": 10},
    ],
    "composites": [{"id": "health", "expression": "clamp(sensor.temp / param.setpoint, 0, 1)"}],
    "conditions": [{"id": "cold", "severity": "warning", "activation": "sensor.temp < 500",
                    "deactivation": "sensor.temp > 520",
                    "effects": [{"target": "processing_time_s", "op": "multiply", "value": 2}]}],
    "behaviors": [{"id": "hot_fast", "output": "speed_pct", "expression": "self * 1.1", "when": "sensor.temp > 800"}],
}


def furnace_type(sim, *, type_id="furnace", config=None, profile=None):
    return sim.industrial.create_type({"id": type_id, "name": "Furnace", "kind": "transformer",
                                       "config": config if config is not None else {"speed_pct": 80},
                                       "profile": profile if profile is not None else FURNACE_PROFILE})


def attribute_sim(seed: int = 1):
    """Product types with attribute definitions and a recipe with a main product and a by-product."""
    sim = basic_sim(seed)
    sim.update_product_type("ore", {"attributes": [{"name": "purity", "type": "number", "default": 0.9}]})
    sim.update_product_type("metal", {"attributes": [
        {"name": "temperature", "type": "number", "unit": "degC"},
        {"name": "grade", "type": "string"},
        {"name": "ore_purity", "type": "number"},
        {"name": "batch_code", "type": "string"}]})
    sim.update_product_type("slag", {"attributes": [
        {"name": "pressure", "type": "number", "unit": "bar"},
        {"name": "source_cycle", "type": "string"}]})
    sim.add_recipe({"id": "smelt", "name": "Smelt", "processing_time_s": 30,
                    "inputs": [{"product_type_id": "ore", "quantity": 1}],
                    "outputs": [
                        {"product_type_id": "metal", "quantity": 1, "attribute_mappings": [
                            {"attribute": "temperature", "source": "measurement", "ref": "temp",
                             "aggregation": "avg"},
                            {"attribute": "grade", "source": "expression",
                             "expression": "'A' if composite.health > 0.5 else 'B'"},
                            {"attribute": "ore_purity", "source": "input", "ref": "purity", "aggregation": "avg"}]},
                        {"product_type_id": "slag", "quantity": 1, "is_byproduct": True, "attribute_mappings": [
                            {"attribute": "pressure", "source": "measurement", "ref": "pressure",
                             "aggregation": "max"},
                            {"attribute": "source_cycle", "source": "cycle", "ref": "cycle_id"}]}]})
    return sim


def furnace_line(sim, *, interval=20, producer_mappings=None, asset_id="t", config=None):
    """producer(ore) -> furnace transformer (deployed from the type) -> metal sink / slag sink."""
    if "furnace" not in sim.industrial.asset_types:
        furnace_type(sim)
    sim.add_asset("producer", "P", {"product_type_id": "ore", "interval": interval,
                                    "attribute_mappings": producer_mappings or []}, asset_id="p")
    sim.industrial.deploy("furnace", "F1", asset_id=asset_id, config={"recipe_id": "smelt", **(config or {})})
    sim.add_asset("sink", "S", {}, asset_id="s")
    sim.add_asset("sink", "Slag", {}, asset_id="ss")
    sim.add_conveyor("c1", port("p", "out"), port(asset_id, "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c1")
    sim.add_conveyor("c2", port(asset_id, "out0"), port("s", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c2")
    sim.add_conveyor("c3", port(asset_id, "out1"), port("ss", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c3")
    return sim


def envelope(i, event_type, asset_id="FURN-1", **kw):
    return {"schema_version": "1.0", "event_id": f"e{i}", "asset_id": asset_id, "event_type": event_type, **kw}
