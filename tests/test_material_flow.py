"""Spec 18.2 – material flow: producers, conveyors, splitters, sinks, backpressure."""

import pytest

from simulator.errors import ConflictError, ValidationError

from conftest import basic_sim, line, port


def test_producer_batches_and_conveyor_travel_time():
    sim = basic_sim()
    line(sim, batch=3, interval=10, length=10, speed=2, cap_per_m=1)
    sim.start()
    sim.run_until(5.0)
    s = sim.get_asset("s")
    assert len(s.received) == 3          # 10 m at 2 m/s = 5 s
    for pid in s.received:
        p = sim.products[pid]
        assert p.terminal_at == pytest.approx(5.0)
        assert p.status.value == "completed"   # no completion criteria -> completed
    assert not sim.check_invariants()


def test_lot_and_label_assignment():
    sim = basic_sim()
    line(sim, batch=2, interval=10)
    sim.start()
    sim.run_until(10.0)
    ps = list(sim.products.values())
    assert [p.label for p in ps] == ["ore-000001", "ore-000002", "ore-000003", "ore-000004"]
    assert ps[0].lot_number == ps[1].lot_number != ps[2].lot_number
    assert len({p.id for p in ps}) == 4


def test_conveyor_capacity_and_backpressure_without_loss():
    sim = basic_sim()
    # sink rejects ore -> conveyor fills to capacity (5) and blocks; producer must skip batches
    line(sim, batch=2, interval=5, length=5, speed=1, cap_per_m=1,
         sink_cfg={"accepted_product_type_ids": ["metal"]})
    sim.start()
    sim.run_until(200)
    c = sim.get_asset("c")
    p = sim.get_asset("p")
    assert len(c.items) == c.capacity == 5
    assert c.state == "blocked"
    assert sim.query_events(event_type="product_rejected")
    assert sim.query_events(event_type="production_skipped")
    assert sim.query_events(event_type="conveyor_blocked")
    # a batch is skipped while pending >= max_pending_batches * batch_quantity (2); nothing lost
    assert len(p.pending) < 2 + 2
    assert sim.counters["created"] == len(c.items) + len(p.pending)
    assert not sim.get_asset("s").received
    assert not sim.check_invariants()
    # unblocking: accept ore -> everything flows into the sink
    sim.update_config("s", {"accepted_product_type_ids": None})
    sim.run_until(260)
    assert sim.get_asset("s").received
    assert c.state != "blocked"
    assert not sim.check_invariants()


def test_conveyor_capacity_validation():
    sim = basic_sim()
    line(sim)
    with pytest.raises(ValidationError):
        sim.update_config("c", {"length_m": 0.5, "capacity_per_m": 1})
    with pytest.raises(ValidationError):
        sim.update_config("c", {"speed_m_s": 0})


def test_conveyor_fifo_no_overtaking_after_speed_change():
    sim = basic_sim()
    line(sim, batch=1, interval=1, length=10, speed=1, cap_per_m=2)
    sim.start()
    sim.run_until(1.0)                     # products at t=0 and t=1 on the belt
    sim.update_config("c", {"speed_m_s": 10})
    sim.run_until(30)
    received = [sim.products[pid] for pid in sim.get_asset("s").received]
    times = [p.terminal_at for p in received]
    labels = [p.label for p in received]
    assert times == sorted(times)
    assert labels == sorted(labels)
    # product admitted after the change (t=2) cannot overtake the one admitted at t=1 (arrives at 11)
    assert received[2].terminal_at == pytest.approx(11.0)


def test_one_input_one_output_per_port():
    sim = basic_sim()
    line(sim)
    sim.add_asset("sink", "S2", {}, asset_id="s2")
    with pytest.raises((ValidationError, ConflictError)):
        sim.add_conveyor("dup", port("p", "out"), port("s2", "in0"), {}, asset_id="dup")
    with pytest.raises(ValidationError):
        sim.add_conveyor("bad", port("p", "out_x"), port("s2", "in0"), {})
    with pytest.raises((ValidationError, ConflictError)):
        sim.add_conveyor("loop", port("p", "out"), port("p", "out"), {})


def _splitter_sim(cfg, producer_cfg=None, types=("ore",)):
    sim = basic_sim()
    sim.add_asset("producer", "P", {"product_type_id": types[0], "batch_quantity": 1, "interval": 1,
                                    **(producer_cfg or {})}, asset_id="p")
    sim.add_asset("splitter", "SP", cfg, asset_id="sp")
    sim.add_asset("sink", "A", {}, asset_id="a")
    sim.add_asset("sink", "B", {}, asset_id="b")
    sim.add_conveyor("c0", port("p", "out"), port("sp", "in"), {"length_m": 1, "speed_m_s": 1}, asset_id="c0")
    sim.add_conveyor("ca", port("sp", "out_a"), port("a", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="ca")
    sim.add_conveyor("cb", port("sp", "out_b"), port("b", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="cb")
    return sim


def test_splitter_percentage_is_accurate():
    sim = _splitter_sim({"mode": "percentage", "percentage_a": 70})
    sim.start()
    sim.run_until(1000)
    a, b = len(sim.get_asset("a").received), len(sim.get_asset("b").received)
    assert a + b >= 990
    assert a / (a + b) == pytest.approx(0.70, abs=0.01)
    assert not sim.check_invariants()


@pytest.mark.parametrize("pct, branch", [(100, "a"), (0, "b")])
def test_splitter_extremes(pct, branch):
    sim = _splitter_sim({"mode": "percentage", "percentage_a": pct})
    sim.start()
    sim.run_until(50)
    other = "b" if branch == "a" else "a"
    assert sim.get_asset(branch).received and not sim.get_asset(other).received


def test_splitter_conditional_and_missing_attribute():
    rules = [{"attribute": "is_defective", "operator": "eq", "value": True, "branch": "B"},
             {"attribute": "attributes.grade", "operator": "ge", "value": 5, "branch": "A"}]
    sim = _splitter_sim({"mode": "conditional", "rules": rules, "fallback_branch": "B"},
                        producer_cfg={"attributes": {"grade": 7}, "defect_probability": 0.3})
    sim.start()
    sim.run_until(300)
    for pid in sim.get_asset("a").received:
        assert not sim.products[pid].is_defective
    defects_b = [pid for pid in sim.get_asset("b").received if sim.products[pid].is_defective]
    assert defects_b and len(defects_b) == len(sim.get_asset("b").received)
    # missing attribute -> fallback, no crash
    sim.update_config("p", {"attributes": {}, "defect_probability": 0.0})
    before_a = len(sim.get_asset("a").received)
    sim.run_until(400)
    assert len(sim.get_asset("a").received) <= before_a + 3   # only products produced before the change
    routed = sim.query_events(event_type="product_routed", limit=5)
    assert routed[-1].details["branch"] == "B"
    assert not sim.check_invariants()


def test_splitter_runtime_change_applies_to_new_products_only():
    sim = _splitter_sim({"mode": "percentage", "percentage_a": 100})
    sim.start()
    sim.run_until(20.5)
    sim.update_config("sp", {"percentage_a": 0})
    a_before = len(sim.get_asset("a").received)
    sim.run_until(60)
    a_after = len(sim.get_asset("a").received)
    assert a_after - a_before <= 2        # already routed products in flight still reach A
    assert len(sim.get_asset("b").received) > 30
    assert sim.get_asset("sp").config_version == 2


def test_splitter_invalid_config_rejected():
    sim = _splitter_sim({"mode": "percentage", "percentage_a": 50})
    for bad in ({"percentage_a": 101}, {"percentage_a": -1}, {"mode": "conditional", "rules": []},
                {"mode": "weird"}, {"rules": [{"attribute": "x", "operator": "zz", "value": 1, "branch": "A"}]}):
        with pytest.raises(ValidationError):
            sim.update_config("sp", bad)
    assert sim.get_asset("sp").config_version == 1


def test_sink_completion_criteria_and_stats():
    sim = basic_sim()
    line(sim, producer_cfg={"defect_probability": 0.5},
         sink_cfg={"completion_criteria": [{"attribute": "is_defective", "operator": "eq", "value": False}]})
    sim.start()
    sim.run_until(500)
    s = sim.get_asset("s")
    st = s.stats()
    assert st["total_received"] == st["total_completed"] + st["total_stored"]
    assert st["total_completed"] == st["non_defective"] and st["total_stored"] == st["defective"]
    assert all(p.is_defective for p in s.products(completed=False, limit=1000))
    assert all(not p.is_defective for p in s.products(completed=True, limit=1000))


def test_stop_and_restart_conveyor_preserves_products():
    sim = basic_sim()
    line(sim, batch=1, interval=2, length=10, speed=1, cap_per_m=1)
    sim.start()
    sim.run_until(5)
    sim.command("c", "stop")
    on_belt = len(sim.get_asset("c").items)
    sim.run_until(50)
    assert len(sim.get_asset("c").items) == on_belt
    assert not sim.get_asset("s").received
    sim.command("c", "restart")
    sim.run_until(100)
    assert sim.get_asset("s").received
    assert not sim.check_invariants()
