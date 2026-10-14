"""Spec 18.5 – REST API, validation and error handling."""

import pytest
from fastapi.testclient import TestClient

from simulator.api import SimulationService, create_app
from simulator.scenario import load_scenario, read_builtin_scenario


@pytest.fixture
def client(tmp_path):
    svc = SimulationService(load_scenario(read_builtin_scenario("scenario_a_basic_line")),
                            checkpoint_dir=tmp_path, background=False)
    with TestClient(create_app(svc)) as c:
        c.svc = svc
        yield c


def test_state_and_step(client):
    assert client.get("/api/state").json()["sim"]["status"] == "idle"
    r = client.post("/api/sim/step", json={"seconds": 100})
    assert r.status_code == 200 and r.json()["sim_time"] == pytest.approx(100)
    st = client.get("/api/state").json()
    assert {a["kind"] for a in st["assets"]} >= {"producer", "conveyor", "transformer", "splitter", "sink"}
    assert client.get("/api/invariants").json()["ok"]


def test_control_flow_and_errors(client):
    assert client.post("/api/sim/start").json()["status"] == "running"
    assert client.post("/api/sim/step", json={"seconds": 1}).status_code == 409
    assert client.post("/api/sim/pause").json()["status"] == "paused"
    assert client.post("/api/sim/acceleration", json={"factor": 10}).json()["acceleration"] == 10
    assert client.post("/api/sim/acceleration", json={"factor": 0}).status_code == 422
    assert client.post("/api/sim/acceleration", json={"factor": "fast"}).status_code == 422
    assert client.post("/api/sim/stop").json()["status"] == "stopped"
    assert client.post("/api/sim/start").status_code == 409
    assert client.post("/api/sim/reset").json()["status"] == "idle"
    assert client.get("/api/state").json()["time"] == 0


def test_asset_crud_and_validation(client):
    r = client.post("/api/assets", json={"kind": "sink", "name": "Extra", "position": {"x": 10, "y": 10}})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post("/api/assets", json={"kind": "sink", "name": "Dup", "id": sid}).status_code == 409
    assert client.post("/api/assets", json={"kind": "rocket", "name": "x"}).status_code == 422
    assert client.patch(f"/api/assets/{sid}/config", json={"id": "hijack"}).status_code == 422
    assert client.patch(f"/api/assets/{sid}/config", json={"capacity": -3}).status_code == 422
    r = client.patch(f"/api/assets/{sid}/config", json={"capacity": 5})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.post(f"/api/assets/{sid}/rename", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert client.get("/api/assets/nope").status_code == 404
    assert client.patch("/api/assets/nope/config", json={}).status_code == 404
    assert client.post(f"/api/assets/{sid}/commands", json={"command": "explode"}).status_code == 422
    assert client.get(f"/api/config-changes?asset_id={sid}").json()[0]["changes"]["capacity"]["new"] == 5
    assert client.delete(f"/api/assets/{sid}").status_code == 204
    assert client.get(f"/api/assets/{sid}").status_code == 404


def test_connection_validation(client):
    r = client.post("/api/connections", json={"source": {"asset_id": "iron-producer", "port": "out"},
                                              "target": {"asset_id": "accepted-sink", "port": "in1"}})
    assert r.status_code in (409, 422)    # producer output already connected
    r = client.post("/api/connections", json={"source": {"asset_id": "missing", "port": "out"},
                                              "target": {"asset_id": "accepted-sink", "port": "in1"}})
    assert r.status_code in (404, 422)


def test_bad_json_bodies(client):
    assert client.post("/api/product-types", json=[1, 2]).status_code == 422
    assert client.post("/api/product-types", json={"id": "", "name": "x"}).status_code == 422
    assert client.post("/api/product-types", json={"id": "iron-billet", "name": "x"}).status_code == 409
    assert client.post("/api/recipes", json={"id": "r", "name": "r", "processing_time_s": 1,
                                             "inputs": [], "outputs": []}).status_code == 422


def test_products_dpp_and_events(client):
    client.post("/api/sim/step", json={"seconds": 300})
    prods = client.get("/api/products?status=completed&limit=5").json()
    assert prods
    rec = client.get(f"/api/products/{prods[0]['id']}").json()
    assert rec["identity"]["product_id"] == prods[0]["id"]
    assert rec["history"] and rec["provenance"]["parent_product_ids"]
    assert client.get("/api/products/PRD-nope").status_code == 404
    evs = client.get("/api/events?limit=10").json()
    assert len(evs) == 10
    newer = client.get(f"/api/events?since_seq={evs[0]['seq']}&limit=5").json()
    assert all(e["seq"] > evs[0]["seq"] for e in newer)
    sink = client.get("/api/assets/accepted-sink/products?completed=true&limit=3").json()
    assert sink and all(p["status"] == "completed" for p in sink)
    ops = client.get("/api/assets/steel-furnace/operational-history").json()
    assert ops and ops[0]["state"]
    cyc = client.get("/api/cycles?asset_id=steel-furnace&limit=1").json()[0]
    assert client.get(f"/api/cycles/{cyc['id']}").json()["events"]


def test_scenario_export_import_roundtrip(client):
    exported = client.get("/api/scenario").json()
    assert client.post("/api/scenario", json=exported).json()["ok"]
    assert client.get("/api/scenario").json() == exported
    assert client.post("/api/scenario", json={"format": "bogus"}).status_code == 422
    assert client.get("/api/scenario").json() == exported      # failed import leaves state untouched
    assert len(client.get("/api/scenarios").json()) >= 2
    assert client.post("/api/scenarios/scenario_b_multi_io/load").json()["ok"]
    assert client.post("/api/scenarios/..%2Fsecret/load").status_code in (404, 422)


def test_checkpoint_save_restore(client):
    client.post("/api/sim/step", json={"seconds": 50})
    assert client.post("/api/checkpoints/cp1").json()["ok"]
    before = client.get("/api/metrics").json()
    client.post("/api/sim/step", json={"seconds": 100})
    assert client.post("/api/checkpoints/cp1/restore").json()["sim_time"] == pytest.approx(50)
    assert client.get("/api/metrics").json() == before
    assert "cp1" in client.get("/api/checkpoints").json()
    assert client.post("/api/checkpoints/missing/restore").status_code == 404


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<canvas" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_order_crud(client):
    assert client.post("/api/orders", json={"id": "PO-X", "name": "X"}).status_code == 201
    r = client.put("/api/orders/PO-X", json={"description": "edited", "attributes": {"customer": "ACME"}})
    assert r.status_code == 200 and r.json()["description"] == "edited" and r.json()["attributes"] == {"customer": "ACME"}
    assert client.put("/api/orders/PO-X", json={"id": "other"}).status_code == 422
    assert client.put("/api/orders/missing", json={"name": "n"}).status_code == 404
    assert client.delete("/api/orders/PO-X").status_code == 204
    assert client.delete("/api/orders/order-default").status_code == 409
    used = next(a["config"]["production_order_id"] for a in client.get("/api/assets").json() if a["kind"] == "producer")
    assert client.delete(f"/api/orders/{used}").status_code == 409
