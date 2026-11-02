"""Amendment §7–§8 – product attribute definitions, producer mappings, independent recipe output and
by-product mappings, input attribute aggregation, measurement aggregation and provenance."""

import pytest

from conftest import basic_sim, port
from industrial_helpers import attribute_sim, furnace_line
from simulator.errors import ValidationError


def run(sim, until=300):
    sim.start()
    sim.run_until(until)
    return sim


def products(sim, type_id):
    return [p for p in sim.products.values() if p.product_type_id == type_id]


def test_attribute_definitions_are_validated():
    sim = basic_sim()
    with pytest.raises(ValidationError):
        sim.update_product_type("ore", {"attributes": [{"name": "a"}, {"name": "a"}]})
    with pytest.raises(ValidationError):
        sim.update_product_type("ore", {"attributes": [{"name": "g", "type": "enum"}]})
    with pytest.raises(ValidationError):
        sim.update_product_type("ore", {"attributes": [{"name": "x", "type": "number", "min": 5, "max": 1}]})
    sim.update_product_type("ore", {"attributes": [{"name": "grade", "type": "enum", "allowed": ["A", "B"]}]})
    # a mapping must target a defined attribute
    with pytest.raises(ValidationError):
        sim.add_asset("producer", "P", {"product_type_id": "ore", "attribute_mappings": [
            {"attribute": "undefined", "source": "constant", "value": 1}]})
    with pytest.raises(ValidationError):
        sim.add_asset("producer", "P", {"product_type_id": "ore", "attribute_mappings": [
            {"attribute": "grade", "source": "measurement"}]})     # ref required


def test_producer_mappings_and_provenance():
    sim = attribute_sim()
    sim.update_product_type("ore", {"attributes": [
        {"name": "purity", "type": "number", "default": 0.9, "min": 0, "max": 1},
        {"name": "lot", "type": "string"}, {"name": "rate", "type": "number"},
        {"name": "flag", "type": "boolean", "default": False}]})
    sim.industrial.create_type({"id": "mine", "name": "Mine", "kind": "producer",
                                "config": {"product_type_id": "ore", "interval": 10},
                                "profile": {"parameters": [{"name": "grade", "type": "number", "default": 0.7}]}})
    sim.industrial.deploy("mine", "M", asset_id="p", config={"attribute_mappings": [
        {"attribute": "purity", "source": "parameter", "ref": "grade"},
        {"attribute": "lot", "source": "cycle", "ref": "lot_number"},
        {"attribute": "rate", "source": "feature", "ref": "interval_s", "scale": 2},
        {"attribute": "flag", "source": "expression", "expression": "1 / 0", "on_missing": "default"}]})
    sim.add_asset("sink", "S", {}, asset_id="s")
    sim.add_conveyor("c", port("p", "out"), port("s", "in0"), {}, asset_id="c")
    run(sim, 100)
    ore = products(sim, "ore")
    assert ore
    p = ore[0]
    assert p.attributes["purity"] == 0.7 and p.attributes["lot"] == p.lot_number and p.attributes["rate"] == 20
    assert p.attributes["flag"] is False
    prov = p.attribute_provenance
    assert prov["purity"]["kind"] == "copied" and prov["purity"]["parameter"] == "grade"
    assert prov["rate"]["transform"] == {"scale": 2, "offset": 0.0}
    assert prov["lot"]["kind"] == "metadata" and prov["lot"]["cycle_id"] == p.origin_cycle_id
    assert prov["flag"]["status"] in ("missing_default", "missing_type_default")
    # parameter changes reach the next batch
    sim.industrial.set_parameters("p", {"grade": 0.5})
    sim.run_until(200)
    assert products(sim, "ore")[-1].attributes["purity"] == 0.5
    # out-of-range values are rejected and fall back to the default with an 'invalid' status
    sim.industrial.set_parameters("p", {"grade": 3})
    sim.run_until(300)
    last = products(sim, "ore")[-1]
    assert last.attributes["purity"] == 0.9 and last.attribute_provenance["purity"]["status"] == "invalid"


def test_recipe_output_and_byproduct_mappings_are_independent():
    sim = furnace_line(attribute_sim(), producer_mappings=[
        {"attribute": "purity", "source": "expression", "expression": "0.8 + 0.1 * (t > 100)"}])
    run(sim, 400)
    metal, slag = products(sim, "metal"), products(sim, "slag")
    assert metal and slag
    m, s = metal[-1], slag[-1]
    assert set(m.attributes) >= {"temperature", "grade", "ore_purity"} and "pressure" not in m.attributes
    assert set(s.attributes) >= {"pressure", "source_cycle"} and "grade" not in s.attributes
    assert m.origin_cycle_id == s.origin_cycle_id == s.attributes["source_cycle"]
    cyc = sim.cycles[m.origin_cycle_id]
    assert m.attributes["temperature"] == pytest.approx(cyc.measurements["temp"]["avg"])
    assert s.attributes["pressure"] == pytest.approx(cyc.measurements["pressure"]["max"])
    assert m.attribute_provenance["temperature"]["kind"] == "aggregated"
    assert m.attribute_provenance["temperature"]["observation_count"] == cyc.measurements["temp"]["count"]
    assert m.attribute_provenance["grade"]["kind"] == "calculated"
    assert m.attribute_provenance["grade"]["formula"].startswith("'A'")
    # grade follows the health composite (warm furnace late in the run)
    assert m.attributes["grade"] == "A" and all(p.attributes["grade"] in ("A", "B") for p in metal)
    # input aggregation from the consumed ore
    parents = [sim.products[i] for i in m.parent_ids] if getattr(m, "parent_ids", None) else []
    if parents:
        assert m.attributes["ore_purity"] == pytest.approx(sum(p.attributes["purity"] for p in parents) / len(parents))
    assert m.attribute_provenance["ore_purity"]["kind"] == "inherited"
    assert m.attribute_provenance["ore_purity"]["input_product_ids"]


def test_input_aggregation_functions():
    sim = basic_sim()
    sim.update_product_type("ore", {"attributes": [{"name": "w", "type": "number"},
                                                   {"name": "tag", "type": "string"}]})
    sim.update_product_type("metal", {"attributes": [
        {"name": n, "type": "number"} for n in ("w_sum", "w_min", "w_max", "w_avg", "w_count", "w_first")]
        + [{"name": "tags", "type": "string"}]})
    aggs = ("sum", "min", "max", "avg", "count", "first")
    sim.add_recipe({"id": "r", "name": "r", "processing_time_s": 5,
                    "inputs": [{"product_type_id": "ore", "quantity": 3}],
                    "outputs": [{"product_type_id": "metal", "quantity": 1, "attribute_mappings": [
                        *[{"attribute": f"w_{a}", "source": "input", "ref": "w", "aggregation": a} for a in aggs],
                        {"attribute": "tags", "source": "input", "ref": "tag", "aggregation": "concat"}]}]})
    sim.add_asset("producer", "P", {"product_type_id": "ore", "interval": 1, "attribute_mappings": [
        {"attribute": "w", "source": "cycle", "ref": "lot_number"}]}, asset_id="p")
    # lot numbers aren't numbers -> use an expression on t instead
    sim.update_config("p", {"attribute_mappings": [
        {"attribute": "w", "source": "expression", "expression": "t"},
        {"attribute": "tag", "source": "expression", "expression": "'x' if t < 2 else 'y'"}]})
    sim.add_asset("transformer", "T", {"recipe_id": "r"}, asset_id="t")
    sim.add_asset("sink", "S", {}, asset_id="s")
    sim.add_conveyor("c1", port("p", "out"), port("t", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c1")
    sim.add_conveyor("c2", port("t", "out0"), port("s", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c2")
    run(sim, 30)
    m = products(sim, "metal")[0]
    ws = sorted(sim.products[i].attributes["w"] for i in m.attribute_provenance["w_sum"]["input_product_ids"])
    assert len(ws) == 3
    assert m.attributes["w_sum"] == pytest.approx(sum(ws)) and m.attributes["w_count"] == 3
    assert m.attributes["w_min"] == min(ws) and m.attributes["w_max"] == max(ws)
    assert m.attributes["w_avg"] == pytest.approx(sum(ws) / 3)
    assert set(m.attributes["tags"].split(",")) <= {"x", "y"}


def test_mapping_snapshot_is_taken_at_cycle_start_and_survives_round_trip():
    import json

    from simulator.scenario import export_scenario, load_scenario
    sim = furnace_line(attribute_sim())
    doc = export_scenario(sim)
    outputs = {r["id"]: r for r in doc["recipes"]}["smelt"]["outputs"]
    assert outputs[0]["attribute_mappings"][0]["attribute"] == "temperature"
    assert {a["name"] for a in {t["id"]: t for t in doc["product_types"]}["metal"]["attributes"]} >= {"grade"}
    sim2 = run(load_scenario(json.loads(json.dumps(doc))), 300)
    sim1 = run(sim, 300)
    a = [p.attributes for p in products(sim1, "metal")]
    b = [p.attributes for p in products(sim2, "metal")]
    assert a == b and a                                  # deterministic, identical after reload
