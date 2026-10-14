"""Spec 18.4 – traceability (DPP), history, events, identity rules."""

import pytest

from simulator.errors import NotFoundError, ValidationError

from conftest import basic_sim, port
from test_transformer import smelter


def test_history_entries_have_required_schema():
    sim = smelter()
    sim.start()
    sim.run_until(200)
    pid = sim.get_asset("sm").received[0]
    hist = sim.full_history(pid)
    assert hist
    for h in hist:
        for key in ("case_id", "activity", "timestamp_start", "timestamp_end", "attributes"):
            assert key in h
        assert h["timestamp_end"] is None or h["timestamp_end"] >= h["timestamp_start"]
    starts = [(h["timestamp_start"], h["seq"]) for h in hist]
    assert starts == sorted(starts)
    activities = {h["activity"] for h in hist}
    assert {"ore", "coal", "c1", "c2", "t", "c3", "sm"} <= activities
    own = [h for h in hist if not h["inherited"]]
    inherited = [h for h in hist if h["inherited"]]
    assert own and inherited
    assert all(h["product_id"] == pid for h in own)


def test_product_record_lineage_and_provenance():
    sim = smelter()
    sim.start()
    sim.run_until(200)
    pid = sim.get_asset("sm").received[0]
    rec = sim.product_record(pid)
    assert rec["identity"]["product_id"] == pid
    prov = rec["provenance"]
    assert len(prov["parent_product_ids"]) == 3
    assert set(prov["parent_product_ids"]) <= set(prov["ancestor_product_ids"])
    for parent in prov["parent_product_ids"]:
        assert pid in sim.product_record(parent)["provenance"]["child_product_ids"]
    assert {p["product_type_id"] for p in rec["lineage"]["parents"]} == {"ore", "coal"}
    slag = sim.products[sim.get_asset("ss").received[0]]
    assert set(slag.parent_ids) == set(sim.products[pid].parent_ids) or slag.origin_cycle_id != sim.products[pid].origin_cycle_id


def test_completed_history_is_immutable():
    sim = smelter()
    sim.start()
    sim.run_until(100)
    p = sim.products[sim.get_asset("sm").received[0]]
    entry = p.history[0]
    with pytest.raises(Exception):
        entry.complete(999.0)


def test_unique_ids_and_events_link_products():
    sim = smelter()
    sim.start()
    sim.run_until(300)
    assert len(sim.products) == len({p.id for p in sim.products.values()})
    assert len({e.event_id for e in sim.events}) == len(sim.events)
    done = sim.query_events(event_type="processing_completed", asset_id="t")
    assert done
    ev = done[0]
    assert ev.cycle_id and ev.input_product_ids and ev.output_product_ids
    for pid in ev.output_product_ids:
        assert sim.products[pid].origin_cycle_id == ev.cycle_id
    assert sim.query_events(product_id=ev.output_product_ids[0])


def test_operational_history_tracks_states():
    sim = smelter()
    sim.start()
    sim.run_until(50)
    sim.command("t", "stop")
    sim.run_until(60)
    sim.command("t", "restart")
    sim.run_until(100)
    ops = sim.operational_history_for("t")
    states = [o["state"] for o in ops]
    assert "processing" in states and "stopped" in states
    for a, b in zip(ops, ops[1:]):
        assert a["end"] == pytest.approx(b["start"])
    stopped = next(o for o in ops if o["state"] == "stopped")
    assert stopped["start"] == pytest.approx(50) and stopped["end"] == pytest.approx(60)


def test_production_orders_are_separate_from_cycles():
    sim = basic_sim()
    sim.add_order({"id": "PO-1", "name": "Order one"})
    sim.add_asset("producer", "P", {"product_type_id": "ore", "batch_quantity": 2, "interval": 5,
                                    "production_order_id": "PO-1"}, asset_id="p")
    sim.add_asset("sink", "S", {}, asset_id="s")
    sim.add_conveyor("c", port("p", "out"), port("s", "in0"), {}, asset_id="c")
    sim.start()
    sim.run_until(30)
    cycles = {p.origin_cycle_id for p in sim.products.values()}
    assert len(cycles) > 1                                              # many cycles...
    assert all(p.production_order_ids == ["PO-1"] for p in sim.products.values())   # ...one order
    assert all(h["case_id"] == "PO-1" for h in sim.full_history(next(iter(sim.products))))
    with pytest.raises(ValidationError):
        sim.update_config("p", {"production_order_id": "PO-unknown"})


def test_identity_protection_and_unknown_assets():
    sim = smelter()
    with pytest.raises(ValidationError):
        sim.update_config("t", {"id": "other"})
    with pytest.raises(ValidationError):
        sim.update_config("t", {"kind": "sink"})
    with pytest.raises(ValidationError):
        sim.update_config("t", {"no_such_field": 1})
    with pytest.raises(NotFoundError):
        sim.update_config("missing", {"speed_pct": 10})
    with pytest.raises((ValidationError, Exception)):
        sim.add_asset("sink", "dup", {}, asset_id="t")
    sim.rename_asset("t", "Renamed")
    assert sim.get_asset("t").name == "Renamed"


def test_removal_rules():
    sim = smelter()
    sim.start()
    sim.run_until(100)
    with pytest.raises(Exception):
        sim.remove_asset("t")          # holds products / connected
    with pytest.raises(Exception):
        sim.remove_asset("sm")         # sink with received products
