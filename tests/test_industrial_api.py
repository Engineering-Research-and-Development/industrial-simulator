"""Amendment REST API surface and the §13 end-to-end scenario (two typed instances, sensors, composites,
conditions, product mappings, external telemetry on one instance, dataset export with joins)."""

import csv
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import port
from industrial_helpers import FURNACE_PROFILE, attribute_sim, envelope, furnace_line
from simulator.api import SimulationService, create_app
from test_ingestion import FakeClient


@pytest.fixture
def client(tmp_path):
    clients = []

    def factory(cfg):
        clients.append(FakeClient(cfg))
        return clients[-1]

    sim = furnace_line(attribute_sim(), producer_mappings=[
        {"attribute": "purity", "source": "expression", "expression": "0.9"}])
    svc = SimulationService(sim, checkpoint_dir=tmp_path, background=False, mqtt_client_factory=factory)
    with TestClient(create_app(svc)) as c:
        c.svc = svc
        c.mqtt_clients = clients
        yield c


def step(c, seconds):
    r = c.post("/api/sim/step", json={"seconds": seconds})
    assert r.status_code == 200, r.text
    return r.json()


def test_asset_type_endpoints(client):
    c = client
    types = c.get("/api/asset-types").json()
    assert [t["id"] for t in types] == ["furnace"] and types[0]["instances"] == ["t"]
    r = c.post("/api/asset-types", json={"id": "bad", "name": "x", "kind": "rocket"})
    assert r.status_code == 422
    r = c.post("/api/asset-types/furnace/duplicate", json={"id": "furnace2", "name": "Furnace 2"})
    assert r.status_code == 201 and r.json()["instances"] == []
    r = c.post("/api/asset-types/furnace2/deploy", json={"name": "F2", "id": "t2", "config": {"recipe_id": "smelt"}})
    assert r.status_code == 201 and r.json()["industrial"]["type"]["id"] == "furnace2"
    prof = {**FURNACE_PROFILE, "parameters": [{**FURNACE_PROFILE["parameters"][0], "default": 950}]}
    r = c.put("/api/asset-types/furnace2", json={"profile": prof})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert c.get("/api/assets/t2/industrial").json()["type"]["outdated"]
    preview = c.post("/api/asset-types/furnace2/sync?dry_run=true").json()
    assert preview[0]["to_version"] == 2 and c.get("/api/assets/t2/industrial").json()["type"]["outdated"]
    c.post("/api/asset-types/furnace2/sync")
    desc = c.get("/api/assets/t2/industrial").json()
    assert not desc["type"]["outdated"]
    assert {p["name"]: p["value"] for p in desc["parameters"]}["setpoint"] == 950
    assert c.delete("/api/asset-types/furnace2").status_code == 409        # still has instances
    assert c.post("/api/assets/t2/unbind").status_code == 200
    assert c.delete("/api/asset-types/furnace2").status_code == 204
    assert c.get("/api/asset-types/furnace2").status_code == 404


def test_instance_override_endpoints(client):
    c = client
    r = c.put("/api/assets/t/parameters", json={"setpoint": 1200})
    assert r.status_code == 200
    assert {p["name"]: p for p in r.json()["parameters"]}["setpoint"]["overridden"]
    assert c.put("/api/assets/t/parameters", json={"setpoint": 99999}).status_code == 422
    assert c.put("/api/assets/t/parameters", json={"nope": 1}).status_code == 422
    r = c.put("/api/assets/t/overrides/sensors/temp", json={"sample_interval_s": 2})
    assert r.status_code == 200
    temp = {s["id"]: s for s in r.json()["sensors"]}["temp"]
    assert temp["sample_interval_s"] == 2 and temp["origin"] == "override"
    assert c.put("/api/assets/t/overrides/sensors/ghost", json={"unit": "x"}).status_code == 404
    r = c.delete("/api/assets/t/overrides/sensors/temp")
    assert {s["id"]: s for s in r.json()["sensors"]}["temp"]["origin"] == "template"
    r = c.put("/api/assets/t/local", json={"composites": [{"id": "hot", "expression": "sensor.temp > 100"}]})
    assert r.status_code == 200 and {x["id"]: x["origin"] for x in r.json()["composites"]}["hot"] == "local"
    r = c.put("/api/assets/t/local", json={"composites": [{"id": "loop", "expression": "composite.loop + 1"}]})
    assert r.status_code == 422


def test_expressions_units_measurements_conditions(client):
    c = client
    ok = c.post("/api/expressions/validate", json={"expression": "sensor.temp * 2", "asset_id": "t"}).json()
    assert ok["valid"] and ok["variables"] == ["sensor.temp"]
    bad = c.post("/api/expressions/validate", json={"expression": "__import__('os')"}).json()
    assert not bad["valid"] and bad["error"]
    unknown = c.post("/api/expressions/validate", json={"expression": "sensor.nope", "asset_id": "t"}).json()
    assert not unknown["valid"]
    assert "degC" in json.dumps(c.get("/api/units").json())
    step(c, 120)
    m = c.get("/api/assets/t/measurements?measurement_id=temp&limit=5").json()
    assert set(m["latest"]) >= {"temp", "pressure", "health"} and len(m["observations"]) == 5
    assert all(o["measurement_id"] == "temp" for o in m["observations"])
    conds = c.get("/api/assets/t/conditions").json()
    assert {"active", "classification", "blocking", "log"} <= set(conds)
    assert any(e["condition_id"] == "cold" for e in conds["log"])
    assert c.get("/api/conditions/log").status_code == 200
    assert c.get("/api/assets/nope/measurements").status_code == 404


def test_ingestion_and_mqtt_endpoints(client):
    c = client
    r = c.put("/api/assets/t/sync", json={"mode": "hybrid", "external_id": "FURN-1",
                                         "categories": {"measurements": "external"}})
    assert r.status_code == 200 and r.json()["categories"]["measurements"] == "external"
    assert c.put("/api/assets/t/sync", json={"mode": "telepathy"}).status_code == 422
    step(c, 5)
    res = c.post("/api/ingestion/events", json=envelope(1, "telemetry", measurements={"TT101": 640})).json()
    assert res["status"] == "accepted" and res["asset_id"] == "t"
    batch = c.post("/api/ingestion/events", json=[envelope(1, "telemetry", measurements={"TT101": 1}),
                                                  {"event_id": "x"}]).json()["results"]
    assert [b["status"] for b in batch] == ["duplicate", "rejected"]
    assert c.post("/api/ingestion/events", json=[{}] * 1001).status_code == 422
    assert c.get("/api/assets/t/measurements").json()["latest"]["temp"]["value"] == 640
    st = c.get("/api/ingestion/status").json()
    assert st["counts"].get("accepted", 0) >= 1 and st["mqtt"]["status"] == "disabled"
    assert all(m["status"] == "rejected" for m in c.get("/api/ingestion/messages?status=rejected").json())
    # MQTT through a fake client
    r = c.put("/api/ingestion/mqtt", json={"host": "broker", "port": 1883, "password": "s3cret", "username": "u"})
    assert r.json()["config"]["password"] != "s3cret"
    assert c.post("/api/ingestion/mqtt/start").json()["status"] == "connecting"
    fc = c.mqtt_clients[-1]
    fc.on_connect(fc, None, {}, 0)
    fc.deliver("industrial/FURN-1/telemetry", json.dumps(envelope(7, "telemetry", measurements={"TT101": 700})))
    info = c.get("/api/ingestion/mqtt").json()
    assert info["status"] == "connected" and info["received"] == 1
    assert c.get("/api/assets/t/measurements").json()["latest"]["temp"]["value"] == 700
    assert c.post("/api/ingestion/mqtt/stop").json()["status"] == "disabled"


def test_dataset_endpoints(client):
    c = client
    r = c.post("/api/datasets/sessions", json={"name": "run1", "config": {"aggregations": ["avg"]}})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert c.post("/api/datasets/sessions", json={"config": {"datasets": ["x"]}}).status_code == 422
    assert c.post(f"/api/datasets/sessions/{sid}/start").json()["status"] == "running"
    assert c.post(f"/api/datasets/sessions/{sid}/explode").status_code == 404
    step(c, 300)
    assert c.put(f"/api/datasets/sessions/{sid}", json={"config": {"assets": []}}).status_code == 409
    assert c.post(f"/api/datasets/sessions/{sid}/stop").json()["status"] == "stopped"
    pv = c.get(f"/api/datasets/sessions/{sid}/preview/cycle?limit=3").json()
    assert len(pv["rows"]) == 3 and "temp_avg" in pv["columns"]
    assert c.get(f"/api/datasets/sessions/{sid}/preview/bogus").status_code == 422
    r = c.get(f"/api/datasets/sessions/{sid}/export/product")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows and "attr_purity" in rows[0]
    man = c.get(f"/api/datasets/sessions/{sid}/manifest").json()
    assert set(man["datasets"]) == {"sensor", "cycle", "product", "machine"}
    r = c.get(f"/api/datasets/sessions/{sid}/bundle?datasets=cycle,product&format=jsonl")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert sorted(z.namelist()) == [f"{sid}_cycle.jsonl", f"{sid}_product.jsonl", "manifest.json"]
    summary = c.get("/api/datasets").json()
    assert summary["continuous"]["id"] == "continuous" and summary["sessions"][0]["id"] == sid
    assert c.delete(f"/api/datasets/sessions/{sid}").status_code == 204
    assert c.get(f"/api/datasets/sessions/{sid}").status_code == 404


def test_end_to_end_two_instances_external_and_simulated(client):
    """Spec §13: one asset type, two instances with different overrides; one simulated, one fed by external
    telemetry; main product + by-product mappings; dataset session joining sensors, cycles and products."""
    c, sim = client, client.svc.sim
    # second line deployed from the same type with instance overrides
    r = c.post("/api/asset-types/furnace/deploy", json={
        "name": "F2", "id": "t2", "config": {"recipe_id": "smelt", "speed_pct": 60}})
    assert r.status_code == 201
    for kind, aid, cfg in (("producer", "p2", {"product_type_id": "ore", "interval": 20}),
                           ("sink", "s2", {}), ("sink", "ss2", {})):
        assert c.post("/api/assets", json={"kind": kind, "name": aid, "id": aid, "config": cfg}).status_code == 201
    for cid, src, dst in (("d1", port("p2", "out"), port("t2", "in0")), ("d2", port("t2", "out0"), port("s2", "in0")),
                          ("d3", port("t2", "out1"), port("ss2", "in0"))):
        r = c.post("/api/connections", json={"name": cid, "id": cid, "source": src, "target": dst,
                                           "config": {"length_m": 1, "speed_m_s": 1}})
        assert r.status_code == 201, r.text
    c.put("/api/assets/t2/parameters", json={"setpoint": 700})
    c.put("/api/assets/t2/overrides/conditions/cold", json={"activation": "sensor.temp < 300",
                                                            "deactivation": "sensor.temp > 320"})
    c.put("/api/assets/t2/sync", json={"mode": "hybrid", "external_id": "FURN-2",
                                       "categories": {"measurements": "external"}})
    d1, d2 = c.get("/api/assets/t/industrial").json(), c.get("/api/assets/t2/industrial").json()
    assert d1["type"]["id"] == d2["type"]["id"] == "furnace"
    assert {p["name"]: p["value"] for p in d2["parameters"]}["setpoint"] == 700
    assert {x["id"]: x["origin"] for x in d2["conditions"]}["cold"] == "override"
    sid = c.post("/api/datasets/sessions", json={"name": "e2e", "autostart": True, "config": {
        "aggregations": ["avg", "max", "count"], "sampling": {"mode": "both", "interval_s": 30}}}).json()["id"]

    # run 10 minutes; t2 receives external furnace telemetry every 10 s
    for i in range(60):
        step(c, 10)
        res = c.post("/api/ingestion/events", json=envelope(
            100 + i, "telemetry", asset_id="FURN-2", measurements={"TT101": 650 + i, "pressure": 3.0})).json()
        assert res["status"] == "accepted", res
    c.post(f"/api/datasets/sessions/{sid}/stop")
    assert c.get("/api/invariants").json()["ok"]

    _, cycles, _ = sim.datasets.table(sid, "cycle")
    _, products, _ = sim.datasets.table(sid, "product")
    _, sensor, _ = sim.datasets.table(sid, "sensor")
    by_cycle = {r["cycle_id"]: r for r in cycles}
    t1 = [r for r in cycles if r["asset_id"] == "t"]
    t2 = [r for r in cycles if r["asset_id"] == "t2"]
    assert t1 and t2
    assert all(r["asset_type_id"] == "furnace" for r in t1 + t2)
    # simulated instance aggregates simulated readings; external instance aggregates received telemetry only
    assert all(r["pressure_max"] == 2.0 for r in t1) and all(r["pressure_max"] == 3.0 for r in t2)
    assert all(650 <= r["temp_avg"] <= 710 for r in t2 if r["temp_count"])
    srcs = {(r["asset_id"], r["measurement_id"]): r["source"] for r in sensor if r["row_type"] == "raw"}
    assert srcs[("t", "temp")] == "simulated" and srcs[("t2", "temp")] == "rest"
    # every product joins to a cycle of the session; main product and by-product share the cycle
    for p in products:
        assert p["cycle_id"] in by_cycle
    for aid in ("t", "t2"):
        metal = [p for p in products if p["asset_id"] == aid and p["product_type_id"] == "metal"]
        slag = [p for p in products if p["asset_id"] == aid and p["product_type_id"] == "slag"]
        assert metal and slag
        assert {m["cycle_id"] for m in metal} == {s["cycle_id"] for s in slag}
        for m in metal:
            assert m["attr_temperature"] == pytest.approx(by_cycle[m["cycle_id"]]["temp_avg"])
            assert m["attr_ore_purity"] is not None or aid == "t2"
        for s in slag:
            assert s["attr_source_cycle"] == s["cycle_id"]
            assert s["attr_pressure"] == by_cycle[s["cycle_id"]]["pressure_max"]
    # lineage: metal from t was produced from ore with recorded purity
    m = next(p for p in sim.products.values() if p.product_type_id == "metal" and p.origin_cycle_id
             and sim.cycles[p.origin_cycle_id].asset_id == "t")
    assert m.attribute_provenance["ore_purity"]["input_product_ids"]
    assert m.attributes["ore_purity"] == pytest.approx(0.9)
    # the bundle carries all datasets and a manifest describing the joins
    r = c.get(f"/api/datasets/sessions/{sid}/bundle")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        man = json.loads(z.read("manifest.json"))
    assert {f"{sid}_{d}.csv" for d in ("sensor", "cycle", "product", "machine")} <= names
    assert man["datasets"]["product"]["rows"] == len(products)
