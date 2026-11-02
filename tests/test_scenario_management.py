"""Scenario management (new empty plant keeping catalogues, save, save-as, rename) and the per-kind
default asset types that are assigned automatically to assets created through the API."""

import json

import pytest
from fastapi.testclient import TestClient

from simulator.api import SimulationService, create_app
from simulator.scenario import load_scenario, read_builtin_scenario

KINDS = ("producer", "transformer", "conveyor", "splitter", "sink")


@pytest.fixture
def client(tmp_path):
    scen = tmp_path / "scenarios"
    scen.mkdir()
    doc = read_builtin_scenario("scenario_c_industrial_furnaces")
    (scen / "furnaces.json").write_text(json.dumps(doc), encoding="utf-8")
    svc = SimulationService(load_scenario(doc), checkpoint_dir=tmp_path / "ckpt", background=False,
                            scenario_dir=scen, scenario_id="furnaces")
    with TestClient(create_app(svc)) as c:
        c.svc, c.dir, c.doc = svc, scen, doc
        yield c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def test_default_types_exist_for_every_kind(client):
    ids = {t["id"]: t for t in ok(client.get("/api/asset-types"))}
    for k in KINDS:
        t = ids[f"default-{k}"]
        assert t["kind"] == k and t["config"] == {} and all(not v for v in t["profile"].values())


def test_new_assets_get_the_default_type(client):
    c = client
    pt = c.get("/api/product-types").json()[0]["id"]
    p = ok(c.post("/api/assets", json={"kind": "producer", "name": "P", "config": {"product_type_id": pt},
                                       "position": {"x": 1, "y": 2}}), 201)
    s = ok(c.post("/api/assets", json={"kind": "sink", "name": "S"}), 201)
    conv = ok(c.post("/api/connections", json={"source": {"asset_id": p["id"], "port": "out"},
                                               "target": {"asset_id": s["id"], "port": "in0"}}), 201)
    for a in (p, s, conv):
        desc = ok(c.get(f"/api/assets/{a['id']}/industrial"))
        assert desc["type"]["id"] == f"default-{a['kind']}"
        assert ok(c.get(f"/api/assets/{a['id']}"))["config_version"] == 1  # binding is not a config change
    types = {t["id"]: t for t in ok(c.get("/api/asset-types"))}
    assert p["id"] in types["default-producer"]["instances"]
    # explicit opt-out and explicit type
    u = ok(c.post("/api/assets", json={"kind": "sink", "name": "U", "type_id": None}), 201)
    assert ok(c.get(f"/api/assets/{u['id']}/industrial"))["type"] is None
    r = c.post("/api/assets", json={"kind": "sink", "name": "X", "type_id": "default-producer"})
    assert r.status_code == 422


def test_default_type_template_applies_to_new_assets(client):
    c = client
    prof = {"parameters": [{"name": "zone", "type": "string", "default": "A"}]}
    ok(c.put("/api/asset-types/default-sink", json={"profile": prof}))
    s = ok(c.post("/api/assets", json={"kind": "sink", "name": "S2"}), 201)
    desc = ok(c.get(f"/api/assets/{s['id']}/industrial"))
    assert desc["type"]["bound_version"] == 2 and {p["name"] for p in desc["parameters"]} == {"zone"}


def test_deleted_default_type_is_recreated(client):
    c = client
    assert c.delete("/api/asset-types/default-splitter").status_code == 204
    ok(c.post("/api/assets", json={"kind": "splitter", "name": "Sp"}), 201)
    assert "default-splitter" in {t["id"] for t in ok(c.get("/api/asset-types"))}


def test_new_scenario_is_empty_and_keeps_catalogues(client):
    c = client
    before = ok(c.get("/api/scenario"))
    custom = ok(c.post("/api/asset-types", json={"id": "probe", "name": "Probe", "kind": "sink"}), 201)
    cur = ok(c.post("/api/scenarios", json={"name": "Line 2", "description": "fresh", "seed": 7}), 201)
    assert cur == {"id": "line_2", "name": "Line 2", "description": "fresh", "saved": True}
    after = ok(c.get("/api/scenario"))
    assert after["assets"] == [] and after["seed"] == 7 and after["name"] == "Line 2"
    for k in ("product_types", "recipes", "production_orders"):
        assert after[k] == before[k]
    assert after["product_types"] and after["recipes"]
    type_ids = {t["id"] for t in after["asset_types"]}
    assert {"furnace", custom["id"]} <= type_ids and {f"default-{k}" for k in KINDS} <= type_ids
    assert all(t["instances"] == [] for t in ok(c.get("/api/asset-types")))
    assert ok(c.get("/api/sim"))["sim_time"] == 0
    # a new scenario file is created; the original file is untouched
    saved = json.loads((c.dir / "line_2.json").read_text(encoding="utf-8"))
    assert saved["assets"] == [] and saved["description"] == "fresh"
    assert json.loads((c.dir / "furnaces.json").read_text(encoding="utf-8")) == c.doc
    assert {s["id"] for s in ok(c.get("/api/scenarios"))} == {"furnaces", "line_2"}
    assert ok(c.get("/api/scenarios/current"))["id"] == "line_2"
    # ids must be unique and valid
    assert c.post("/api/scenarios", json={"name": "Line 2"}).status_code == 409
    assert c.post("/api/scenarios", json={"name": "x", "id": "bad id!"}).status_code == 422
    assert c.post("/api/scenarios", json={"name": ""}).status_code == 422


def test_new_scenario_without_catalogues(client):
    c = client
    ok(c.post("/api/scenarios", json={"name": "Blank", "keep_catalog": False, "keep_asset_types": False}), 201)
    doc = ok(c.get("/api/scenario"))
    assert doc["product_types"] == [] and doc["recipes"] == [] and doc["assets"] == []
    assert {t["id"] for t in doc["asset_types"]} == {f"default-{k}" for k in KINDS}


def test_save_and_save_as(client):
    c = client
    ok(c.post("/api/scenarios", json={"name": "Work"}), 201)
    pt = c.get("/api/product-types").json()[0]["id"]
    ok(c.post("/api/assets", json={"kind": "producer", "name": "P", "id": "p1", "config": {"product_type_id": pt}}), 201)
    cur = ok(c.put("/api/scenarios/work", json={"description": "one producer"}))
    assert cur["id"] == "work" and cur["description"] == "one producer"
    saved = json.loads((c.dir / "work.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in saved["assets"]] == ["p1"]
    assert saved["assets"][0]["industrial"]["type_id"] == "default-producer"
    # the saved file loads back identically (including the default-type binding)
    ok(c.post("/api/scenarios/work/load"))
    assert ok(c.get("/api/assets/p1/industrial"))["type"]["id"] == "default-producer"
    assert ok(c.get("/api/scenario")) == ok(c.get("/api/scenario"))
    # save as: refuses to overwrite another file unless asked
    assert c.put("/api/scenarios/furnaces").status_code == 409
    cur = ok(c.put("/api/scenarios/work_copy", json={"name": "Work copy"}))
    assert cur["id"] == "work_copy" and (c.dir / "work_copy.json").is_file()
    assert json.loads((c.dir / "work.json").read_text(encoding="utf-8"))["name"] == "Work"
    ok(c.put("/api/scenarios/furnaces?overwrite=true"))
    assert c.put("/api/scenarios/bad id").status_code in (404, 422)


def test_rename_scenario(client):
    c = client
    r = ok(c.post("/api/scenarios/furnaces/rename", json={"new_id": "furnace_line", "name": "Furnace line"}))
    assert r["id"] == "furnace_line" and r["current"]["id"] == "furnace_line"
    assert not (c.dir / "furnaces.json").exists()
    assert json.loads((c.dir / "furnace_line.json").read_text(encoding="utf-8"))["name"] == "Furnace line"
    assert ok(c.get("/api/scenarios/current"))["name"] == "Furnace line"
    # name only
    ok(c.post("/api/scenarios/furnace_line/rename", json={"name": "Renamed"}))
    assert {s["id"]: s["name"] for s in ok(c.get("/api/scenarios"))} == {"furnace_line": "Renamed"}
    # conflicts and errors
    ok(c.post("/api/scenarios", json={"name": "Other"}), 201)
    assert c.post("/api/scenarios/other/rename", json={"new_id": "furnace_line"}).status_code == 409
    assert c.post("/api/scenarios/missing/rename", json={"new_id": "x"}).status_code == 404
    assert c.post("/api/scenarios/other/rename", json={"new_id": "bad id"}).status_code == 422
    assert c.post("/api/scenarios/other/rename", json={"name": " "}).status_code == 422
    # renaming a file that is not the current one leaves the current plant alone
    ok(c.post("/api/scenarios/furnace_line/rename", json={"new_id": "fl"}))
    assert ok(c.get("/api/scenarios/current"))["id"] == "other"


def test_import_marks_scenario_unsaved_and_reset_keeps_file(client):
    c = client
    ok(c.post("/api/sim/reset"))
    assert ok(c.get("/api/scenarios/current"))["id"] == "furnaces"
    ok(c.post("/api/scenario", json=c.doc))
    cur = ok(c.get("/api/scenarios/current"))
    assert cur["id"] is None and cur["saved"] is False
