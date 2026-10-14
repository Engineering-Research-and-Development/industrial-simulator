"""Scenarios A–C, persistence/determinism and global invariants."""

import random

import pytest

from simulator.scenario import (export_scenario, list_builtin_scenarios, load_checkpoint, load_scenario,
                                read_builtin_scenario, save_checkpoint)


@pytest.mark.parametrize("sid", [s["id"] for s in list_builtin_scenarios()])
def test_builtin_scenarios_run_cleanly(sid):
    sim = load_scenario(read_builtin_scenario(sid))
    assert sim.validate_network()["valid"]
    sim.start()
    sim.run_until(1800)
    assert not sim.check_invariants()
    m = sim.metrics()
    assert m["products_created"] == (m["products_active"] + m["products_consumed"]
                                     + m["products_completed"] + m["products_stored"])


def test_scenario_a_quality_split():
    sim = load_scenario(read_builtin_scenario("scenario_a_basic_line"))
    sim.start()
    sim.run_until(1800)
    accepted = [sim.products[p] for p in sim.get_asset("accepted-sink").received]
    rejected = [sim.products[p] for p in sim.get_asset("rejected-sink").received]
    assert accepted and rejected
    assert not any(p.is_defective for p in accepted)
    assert all(p.is_defective for p in rejected)


def test_scenario_b_byproduct_and_percentage():
    sim = load_scenario(read_builtin_scenario("scenario_b_multi_io"))
    sim.start()
    sim.run_until(3600)
    stats = {a.id: a.stats() for a in sim.assets.values() if a.kind == "sink"}
    slag = [s for s in stats.values() if s["by_product_type"].get("slag")]
    assert slag
    sp = next(a for a in sim.assets.values() if a.kind == "splitter")
    st = sp.stats()
    total = st["routed_total"]
    assert total > 20
    assert st["share_a"] == pytest.approx(sp.config["percentage_a"] / 100, abs=0.05)


def test_export_load_roundtrip_is_lossless():
    data = read_builtin_scenario("scenario_b_multi_io")
    sim = load_scenario(data)
    again = export_scenario(load_scenario(export_scenario(sim)))
    assert again == export_scenario(sim)


def test_checkpoint_resume_is_deterministic(tmp_path):
    def build():
        return load_scenario(read_builtin_scenario("scenario_a_basic_line"))

    ref = build()
    ref.start()
    ref.run_until(1200)

    sim = build()
    sim.start()
    sim.run_until(500)
    save_checkpoint(sim, "mid", tmp_path)
    resumed = load_checkpoint("mid", tmp_path)
    resumed.run_until(1200)

    def sig(s):
        return [(p.id, p.label, p.status.value, p.is_defective, p.terminal_at) for p in s.products.values()]

    assert sig(resumed) == sig(ref)
    assert [e.event_id for e in resumed.events] == [e.event_id for e in ref.events]
    assert not resumed.check_invariants()


def test_checkpoint_name_is_validated(tmp_path):
    from simulator.errors import ValidationError
    sim = load_scenario(read_builtin_scenario("scenario_a_basic_line"))
    with pytest.raises(ValidationError):
        save_checkpoint(sim, "../../evil", tmp_path)
    assert save_checkpoint(sim, "good_name-1", tmp_path).parent == tmp_path


def test_random_runtime_changes_preserve_invariants():
    """Fuzz: random config changes, stops, faults and restarts never break conservation."""
    rng = random.Random(5)
    sim = load_scenario(read_builtin_scenario("scenario_a_basic_line"))
    sim.start()
    ids = list(sim.assets)
    for step in range(120):
        sim.run_until(sim.now + rng.uniform(1, 20))
        aid = rng.choice(ids)
        a = sim.get_asset(aid)
        action = rng.random()
        try:
            if action < 0.3:
                sim.command(aid, rng.choice(["stop", "fault", "restart", "clear_fault", "start"]))
            elif a.kind == "transformer":
                sim.update_config(aid, {"speed_pct": rng.uniform(1, 100)})
            elif a.kind == "conveyor":
                sim.update_config(aid, {"speed_m_s": rng.uniform(0.2, 5)})
            elif a.kind == "splitter" and a.config["mode"] == "percentage":
                sim.update_config(aid, {"percentage_a": rng.uniform(0, 100)})
            elif a.kind == "producer":
                sim.update_config(aid, {"interval": rng.uniform(1, 60)})
        except Exception as exc:     # validation/conflict errors are fine, corruption is not
            assert type(exc).__name__ in ("ValidationError", "ConflictError")
        assert not sim.check_invariants(), f"step {step}"
    for aid in ids:
        sim.command(aid, "restart")
    sim.run_until(sim.now + 600)
    assert not sim.check_invariants()
