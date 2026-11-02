"""Spec 18.3 – transformers: recipes, multi-I/O, speed & quality curves, runtime changes."""

import pytest

from simulator.curves import QualityModel, SpeedModel, validate_speed
from simulator.errors import ConflictError, ValidationError

from conftest import basic_sim, port


def smelter(speed=50.0, processing=20.0, ore_interval=1.0, coal_interval=2.0, seed=1, outputs=None):
    sim = basic_sim(seed)
    sim.add_recipe({"id": "smelt", "name": "Smelt", "processing_time_s": processing,
                    "inputs": [{"product_type_id": "ore", "quantity": 2}, {"product_type_id": "coal", "quantity": 1}],
                    "outputs": outputs or [{"product_type_id": "metal", "quantity": 1},
                                           {"product_type_id": "slag", "quantity": 1, "is_byproduct": True}]})
    sim.add_asset("producer", "Ore", {"product_type_id": "ore", "interval": ore_interval}, asset_id="ore")
    sim.add_asset("producer", "Coal", {"product_type_id": "coal", "interval": coal_interval}, asset_id="coal")
    sim.add_asset("transformer", "T", {"recipe_id": "smelt", "speed_pct": speed}, asset_id="t")
    sim.add_asset("sink", "Metal", {}, asset_id="sm")
    sim.add_asset("sink", "Slag", {}, asset_id="ss")
    sim.add_conveyor("c1", port("ore", "out"), port("t", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c1")
    sim.add_conveyor("c2", port("coal", "out"), port("t", "in1"), {"length_m": 1, "speed_m_s": 1}, asset_id="c2")
    sim.add_conveyor("c3", port("t", "out0"), port("sm", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c3")
    if len(outputs or [1, 2]) > 1:
        sim.add_conveyor("c4", port("t", "out1"), port("ss", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c4")
    return sim


def test_multi_input_multi_output_cycle():
    sim = smelter()
    sim.start()
    sim.run_until(200)
    cycles = [c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"]
    assert cycles
    for c in cycles:
        types = sorted(sim.products[i].product_type_id for i in c.input_ids)
        assert types == ["coal", "ore", "ore"]
        assert sorted(sim.products[o].product_type_id for o in c.output_ids) == ["metal", "slag"]
    metal = sim.products[sim.get_asset("sm").received[0]]
    assert len(metal.parent_ids) == 3
    assert all(sim.products[p].status.value == "consumed" for p in metal.parent_ids)
    assert sim.get_asset("ss").received        # by-product routed to its own port
    assert not sim.check_invariants()


def test_transformer_waits_for_all_inputs():
    sim = smelter(coal_interval=1000)    # one coal at t=0 only
    sim.start()
    sim.run_until(300)
    done = [c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"]
    assert len(done) == 1
    assert sim.get_asset("t").state in ("idle", "blocked")


@pytest.mark.parametrize("speed, expected_duration", [(50, 20.0), (100, 10.0), (1, 40.0)])
def test_speed_multiplier_changes_duration(speed, expected_duration):
    sim = smelter(speed=speed)
    sim.start()
    sim.run_until(200)
    c = next(c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed")
    assert c.ended_at - c.started_at == pytest.approx(expected_duration)


def test_speed_and_quality_curves():
    sm, qm = SpeedModel(), QualityModel()
    assert sm.multiplier(1) == pytest.approx(2.0)
    assert sm.multiplier(50) == pytest.approx(1.0)
    assert sm.multiplier(100) == pytest.approx(0.5)
    assert qm.quality(1) == pytest.approx(1.0)
    assert qm.quality(50) == pytest.approx(0.999)
    assert qm.quality(100) == pytest.approx(0.99)
    qs = [qm.quality(s) for s in range(1, 101)]
    assert all(a >= b - 1e-12 for a, b in zip(qs, qs[1:]))      # monotonically non-increasing
    ms = [sm.multiplier(s) for s in range(1, 101)]
    assert all(a >= b - 1e-12 for a, b in zip(ms, ms[1:]))
    for bad in (0, 101, -5, "fast", None):
        with pytest.raises(ValidationError):
            validate_speed(bad)


def test_quality_drives_defect_rate():
    sim = smelter(speed=100, processing=1, ore_interval=0.5, coal_interval=0.25, seed=3)
    sim.update_config("t", {"quality_model": {"q_at_1": 1.0, "q_at_50": 0.95, "q_at_100": 0.75}})
    sim.start()
    sim.run_until(2000)
    metal = [sim.products[p] for p in sim.get_asset("sm").received]
    assert len(metal) > 500
    rate = sum(p.is_defective for p in metal) / len(metal)
    assert rate == pytest.approx(0.25, abs=0.05)               # q=0.75 at 100 %
    assert all(p.quality_score == pytest.approx(0.75) for p in metal)


def test_same_seed_same_results():
    def run(seed):
        sim = smelter(speed=90, seed=seed)
        sim.start()
        sim.run_until(500)
        return [(p.label, p.is_defective, p.id) for p in sim.products.values()]

    assert run(11) == run(11)
    assert [x[2] for x in run(11)] != [x[2] for x in run(12)]


def test_config_change_mid_cycle_applies_next_cycle():
    """Scenario C: speed changed during a running cycle."""
    sim = smelter(speed=50)
    sim.start()
    sim.run_until(5)       # cycle running (started at t=1, ends t=21)
    t = sim.get_asset("t")
    active = sim.cycles[t.active_cycle_id]
    result = sim.update_config("t", {"speed_pct": 100})
    assert "next cycle" in result["effective"]
    assert result["version"] == 2
    sim.run_until(200)
    assert active.ended_at - active.started_at == pytest.approx(20.0)
    assert active.config_snapshot["speed_pct"] == 50
    later = [c for c in sim.cycles.values() if c.asset_id == "t" and c.started_at > active.started_at
             and c.status == "completed"]
    assert later and all(c.ended_at - c.started_at == pytest.approx(10.0) for c in later)
    assert all(c.config_snapshot["speed_pct"] == 100 for c in later)
    ev = sim.query_events(event_type="configuration_changed", asset_id="t")
    assert ev and ev[-1].details["changes"]["speed_pct"] == {"old": 50.0, "new": 100.0}
    # product history records the configuration under which it was produced
    first = next(p for p in sim.products.values() if p.origin_cycle_id == active.id)
    last = next(p for p in sim.products.values() if p.origin_cycle_id == later[-1].id)
    assert first.history[0].attributes["config_version"] == 1
    assert last.history[0].attributes["config_version"] == 2


def test_stop_cancels_cycle_and_restores_inputs():
    sim = smelter()
    sim.start()
    sim.run_until(5)
    t = sim.get_asset("t")
    cyc = sim.cycles[t.active_cycle_id]
    inputs = list(cyc.input_ids)
    sim.command("t", "fault")
    assert cyc.status == "cancelled"
    assert t.state == "failed"
    for pid in inputs:
        assert sim.products[pid].status.value == "queued"
        assert sim.products[pid].location.asset_id == "t"
    assert sim.query_events(event_type="cycle_cancelled")
    assert sim.query_events(event_type="fault_detected")
    assert not sim.check_invariants()
    sim.command("t", "clear_fault")
    sim.run_until(100)
    assert any(c.status == "completed" for c in sim.cycles.values() if c.asset_id == "t")
    assert not sim.check_invariants()


def test_recipe_validation():
    sim = basic_sim()
    base = {"id": "x", "name": "x", "processing_time_s": 1}
    for bad in (
        {**base, "inputs": [], "outputs": [{"product_type_id": "metal", "quantity": 1}]},
        {**base, "inputs": [{"product_type_id": "ore", "quantity": 1}] * 6, "outputs": [{"product_type_id": "metal", "quantity": 1}]},
        {**base, "inputs": [{"product_type_id": "ore", "quantity": 1}], "outputs": [{"product_type_id": "metal", "quantity": 1}] * 4},
        {**base, "inputs": [{"product_type_id": "ore", "quantity": 0}], "outputs": [{"product_type_id": "metal", "quantity": 1}]},
        {**base, "inputs": [{"product_type_id": "nope", "quantity": 1}], "outputs": [{"product_type_id": "metal", "quantity": 1}]},
        {**base, "processing_time_s": 0, "inputs": [{"product_type_id": "ore", "quantity": 1}], "outputs": [{"product_type_id": "metal", "quantity": 1}]},
    ):
        with pytest.raises(ValidationError):
            sim.add_recipe(bad)


def test_incompatible_recipe_change_rejected():
    sim = smelter()
    sim.add_recipe({"id": "single", "name": "s", "processing_time_s": 1,
                    "inputs": [{"product_type_id": "ore", "quantity": 1}],
                    "outputs": [{"product_type_id": "metal", "quantity": 1}]})
    with pytest.raises(ConflictError):
        sim.update_config("t", {"recipe_id": "single"})    # in1 connected but recipe has 1 input
    assert sim.get_asset("t").config["recipe_id"] == "smelt"


def test_invalid_speed_rejected():
    sim = smelter()
    for v in (0, 101, -1, "x", True):
        with pytest.raises(ValidationError):
            sim.update_config("t", {"speed_pct": v})


def test_operational_history_has_no_zero_length_same_state_intervals():
    sim = smelter(coal_interval=50)
    sim.start()
    sim.run_until(300)
    hist = sim.operational_history["t"]
    for a, b in zip(hist, hist[1:]):
        assert not (a.state == b.state and a.end == a.start), (a, b)
    assert all(h.end is None or h.end >= h.start for h in hist)


def test_ports_follow_recipe_and_sink_has_single_input():
    sim = smelter()
    info = sim.get_asset("t").port_info()
    assert info["inputs"] == ["in0", "in1"] and info["outputs"] == ["out0", "out1"]
    assert info["labels"]["in0"].startswith("2×") and "by-product" in info["labels"]["out1"]
    assert sim.get_asset("sm").port_info()["inputs"] == ["in0"]
    with pytest.raises((ValidationError, ConflictError)):
        sim.add_conveyor("cx", port("t", "out1"), port("sm", "in1"), {"length_m": 1, "speed_m_s": 1})


def test_defects_cascade_by_default():
    sim = smelter(seed=3)
    sim.update_config("ore", {"defect_probability": 1.0})
    assert sim.recipes["smelt"].defect_policy == "any_defective_input"
    sim.start()
    sim.run_until(200)
    outs = [sim.products[o] for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"
            for o in c.output_ids]
    assert outs and all(p.is_defective for p in outs)
    entry = next(h for h in outs[0].history if h.attributes.get("role") == "output")
    assert entry.attributes["defect_cause"] == "defective_input"
