"""Amendment §9 – dataset collection sessions, sampling, aggregation, joins, deduplication and export."""

import csv
import io
import json
import zipfile

import pytest

from industrial_helpers import attribute_sim, envelope, furnace_line
from simulator.errors import ConflictError, ValidationError
from simulator.industrial.ingestion import ingest


def line():
    return furnace_line(attribute_sim(), producer_mappings=[
        {"attribute": "purity", "source": "expression", "expression": "0.9"}])


def read_csv(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


def test_config_validation():
    ds = line().datasets
    for bad in ({"datasets": []}, {"datasets": ["bogus"]}, {"aggregations": ["median"]},
                {"sampling": {"mode": "sometimes"}}, {"sampling": {"interval_s": 0}},
                {"end": {"type": "cycle_count", "value": 1.5}}, {"start": {"type": "at_time"}},
                {"time_window": {"start_s": 10, "end_s": 5}}, {"assets": ["nope"]}, {"unknown": 1},
                {"columns": {"sensor": {"rename": {"a": "x", "b": "x"}}}}, {"format": "xlsx"}):
        with pytest.raises(ValidationError):
            ds.create_session({"config": bad})


def test_session_lifecycle_and_segments():
    sim = line()
    ds = sim.datasets
    s = ds.create_session({"name": "run", "config": {"sampling": {"mode": "periodic", "interval_s": 10}}})
    assert s.status == "created"
    sim.start()
    sim.run_until(50)
    assert not s.sensor_rows                                   # not started -> nothing collected
    ds.start(s.id)
    sim.run_until(100)
    n = len(s.sensor_rows)
    assert n > 0 and all(r["row_type"] == "sampled" for r in s.sensor_rows)
    ds.pause(s.id)
    sim.run_until(150)
    assert len(s.sensor_rows) == n                             # paused -> no samples
    ds.resume(s.id)
    sim.run_until(200)
    ds.stop(s.id)
    assert s.status == "stopped" and len(s.segments) == 2 and s.segments[0] == [50.0, 100.0]
    with pytest.raises(ConflictError):
        ds.start(s.id)
    times = {r["timestamp"] for r in s.sensor_rows}
    assert not any(100 < t < 150 for t in times)
    # periodic samples are on the configured grid relative to the segment start
    assert {50.0, 60.0, 150.0, 160.0} <= times


def test_end_conditions():
    sim = line()
    ds = sim.datasets
    a = ds.create_session({"config": {"end": {"type": "duration", "value": 60}}, "autostart": True})
    b = ds.create_session({"config": {"end": {"type": "cycle_count", "value": 3}}, "autostart": True})
    c = ds.create_session({"config": {"start": {"type": "at_time", "at_s": 100},
                                      "end": {"type": "at_time", "value": 200}}, "autostart": True})
    assert c.status == "scheduled"
    sim.start()
    sim.run_until(400)
    assert a.status == "completed" and a.end_reason == "duration" and a.stopped_at == 60
    assert b.status == "completed" and len(b.cycle_ids) == 3
    assert c.status == "completed" and c.segments == [[100.0, 200.0]]


def test_cycle_and_product_datasets_join():
    sim = line()
    ds = sim.datasets
    s = ds.create_session({"config": {"aggregations": ["avg", "max", "count"]}, "autostart": True})
    sim.start()
    sim.run_until(400)
    ds.stop(s.id)
    cycles = {r["cycle_id"]: r for r in ds.preview(s.id, "cycle", 0)["rows"] or ds.table(s.id, "cycle")[1]}
    t_cycles = {k: r for k, r in cycles.items() if r["asset_id"] == "t"}
    assert t_cycles
    for cid, r in t_cycles.items():
        agg = sim.cycles[cid].measurements["temp"]
        assert r["temp_avg"] == pytest.approx(agg["avg"]) and r["temp_count"] == agg["count"]
    _, products, _ = ds.table(s.id, "product")
    metal = [p for p in products if p["product_type_id"] == "metal"]
    slag = [p for p in products if p["product_type_id"] == "slag"]
    assert metal and slag
    for p in metal + slag:
        assert p["cycle_id"] in cycles                         # product.cycle_id -> cycle.cycle_id
        assert p["cycle_temp_avg"] == cycles[p["cycle_id"]]["temp_avg"]
    assert metal[0]["attr_temperature_source"] == "measurement" and metal[0]["attr_pressure"] is None
    assert slag[0]["attr_pressure"] is not None and slag[0]["attr_grade"] is None
    _, sensor, _ = ds.table(s.id, "sensor")
    raw_cids = {r["cycle_id"] for r in sensor if r["asset_id"] == "t" and r["cycle_id"]}
    assert raw_cids & set(t_cycles)                            # sensor.cycle_id -> cycle.cycle_id
    # filters
    ds2 = ds.create_session({"config": {"product_types": ["slag"], "attributes": ["pressure"],
                                        "assets": ["t"], "measurements": ["temp"]}})
    assert ds2.status == "created"


def test_filters_and_column_shaping():
    sim = line()
    ds = sim.datasets
    s = ds.create_session({"autostart": True, "config": {
        "assets": ["t"], "measurements": ["temp"], "include_composites": False, "product_types": ["metal"],
        "attributes": ["grade"], "datasets": ["sensor", "product"],
        "columns": {"sensor": {"select": ["timestamp", "value"], "rename": {"value": "temp_c"}}},
        "missing": {"policy": "fill", "fill_value": -1}}})
    sim.start()
    sim.run_until(300)
    assert {(r["asset_id"], r["measurement_id"]) for r in s.sensor_rows} == {("t", "temp")}
    cols, rows, units = ds.table(s.id, "sensor")
    assert cols == ["timestamp", "temp_c"] and set(rows[0]) == {"timestamp", "temp_c"}
    cols, rows, _ = ds.table(s.id, "product")
    assert {r["product_type_id"] for r in rows} == {"metal"}
    assert "attr_grade" in cols and "attr_temperature" not in cols
    assert all(v != None for r in rows for v in r.values())    # noqa: E711 – filled
    assert "machine" not in s.config["datasets"]
    ds.update_session(s.id, {"name": "renamed"})
    with pytest.raises(ConflictError):
        ds.update_session(s.id, {"config": {"assets": []}})
    s.config["columns"]["sensor"]["select"] = ["nope"]
    with pytest.raises(ValidationError):
        ds.table(s.id, "sensor")


def test_external_duplicates_are_skipped_and_marked():
    from industrial_helpers import FURNACE_PROFILE
    sim = line()
    sim.industrial.set_sync("t", {"mode": "hybrid", "external_id": "FURN-1",
                                  "categories": {"measurements": "external"}})
    s = sim.datasets.create_session({"autostart": True, "config": {"assets": ["t"]}})
    sim.start()
    sim.run_until(10)
    msg = envelope(1, "telemetry", measurements={"temp": {"value": 500, "unit": "degC"}})
    assert ingest(sim, msg)["status"] == "accepted"
    ingest(sim, msg)                                           # duplicate message
    ext = [r for r in s.sensor_rows if r["source"] == "rest"]
    assert len(ext) == 1 and ext[0]["message_id"] == "e1" and ext[0]["value"] == 500
    assert FURNACE_PROFILE["sensors"][0]["id"] == "temp"


def test_csv_export_manifest_and_bundle():
    sim = line()
    ds = sim.datasets
    s = ds.create_session({"name": "exp", "autostart": True, "config": {"missing": {"policy": "null"}}})
    sim.start()
    sim.run_until(300)
    data, name, mime = ds.export(s.id, "cycle")
    assert name == f"{s.id}_cycle.csv" and mime == "text/csv"
    rows = read_csv(data)
    assert rows and len(rows) == ds.preview(s.id, "cycle")["total_rows"]
    assert "NULL" in data.decode()                             # explicit null marker
    jl, name, _ = ds.export(s.id, "product", "jsonl")
    assert name.endswith(".jsonl") and json.loads(jl.decode().splitlines()[0])["product_id"]
    with pytest.raises(ValidationError):
        ds.export(s.id, "cycle", "xlsx")
    blob, zname = ds.bundle(s.id, ["cycle", "product"])
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert set(z.namelist()) == {f"{s.id}_cycle.csv", f"{s.id}_product.csv", "manifest.json"}
        man = json.loads(z.read("manifest.json"))
    assert man["datasets"]["cycle"]["rows"] == len(rows) and man["seed"] == sim.seed
    assert {"from": "product.cycle_id", "to": "cycle.cycle_id"} in man["relationships"]
    assert man["datasets"]["cycle"]["units"]["duration_s"] == "s"


def test_continuous_collector_and_persistence():
    from simulator.scenario import export_scenario, load_scenario
    sim = line()
    ds = sim.datasets
    ds.start("continuous")
    ds.create_session({"id": "keep", "name": "keep", "config": {"aggregations": ["min"]}})
    sim.start()
    sim.run_until(100)
    assert ds.continuous.cycle_ids
    with pytest.raises(ConflictError):
        ds.delete_session("continuous")
    ds.clear("continuous")
    assert not ds.continuous.cycle_ids and not ds.continuous.sensor_rows
    doc = json.loads(json.dumps(export_scenario(sim)))
    sim2 = load_scenario(doc)
    assert sim2.datasets.get("keep").config["aggregations"] == ["min"]
    assert sim2.datasets.continuous.status in ("running", "scheduled")
