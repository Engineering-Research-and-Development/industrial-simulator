"""The built-in industrial example scenario loads, simulates, maps attributes and collects datasets."""

from simulator.industrial.ingestion import ingest
from simulator.scenario import export_scenario, list_builtin_scenarios, load_scenario, read_builtin_scenario

SCENARIO = "scenario_c_industrial_furnaces"


def test_example_scenario_end_to_end():
    assert SCENARIO in [s["id"] for s in list_builtin_scenarios()]
    sim = load_scenario(read_builtin_scenario(SCENARIO))
    assert sim.industrial.describe("furnace1")["type"]["id"] == "furnace"
    assert sim.industrial.describe("furnace2")["sync"]["categories"]["measurements"] == "external"
    sim.datasets.start("DS-0001")
    sim.start()
    for t in range(10, 1210, 10):
        sim.run_until(t)
        r = ingest(sim, {"schema_version": "1.0", "event_id": f"e{t}", "asset_id": "FURN-2", "event_type": "telemetry",
                         "measurements": {"TT101": {"value": 1390.0, "unit": "degC"}, "PT102": 1.3}})
        assert r["status"] == "accepted", r
    metal = [p for p in sim.products.values() if p.product_type_id == "metal"]
    f1 = [p for p in metal if p.attributes.get("lot", "").startswith("LOT-furnace1")]
    f2 = [p for p in metal if p.attributes.get("lot", "").startswith("LOT-furnace2")]
    assert f1 and f2
    assert {"melt_temperature", "peak_pressure", "grade", "ore_purity", "cold_cycle"} <= set(f1[-1].attributes)
    assert f1[-1].attributes["ore_purity"] == 0.93 and f2[-1].attributes["ore_purity"] == 0.87
    assert f2[-1].attributes["melt_temperature"] == 1390.0       # from the externally ingested TT101
    session = sim.datasets.get("DS-0001").summary()
    assert session["status"] == "running" and session["counts"]["sensor"] > 0 and session["counts"]["product"] > 0
    # export / re-import round-trip keeps types, bindings, sync settings and the session definition
    again = load_scenario(export_scenario(sim))
    assert "furnace" in again.industrial.asset_types
    assert again.industrial.describe("furnace2")["sync"]["external_id"] == "FURN-2"
