"""Spec 18.1 – simulation time, acceleration and real-time pacing."""

import time

import pytest

from simulator.controller import SimulationController
from simulator.errors import ConflictError, ValidationError

from conftest import basic_sim, line


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(acc=1.0):
    sim = basic_sim()
    line(sim, interval=10.0)
    clock = FakeClock()
    return sim, clock, SimulationController(sim, time_fn=clock, acceleration=acc)


def test_one_minute_at_1x_advances_60s():
    sim, clock, ctl = make(1.0)
    ctl.start()
    clock.t += 60
    ctl.tick()
    assert sim.now == pytest.approx(60.0)
    # producer every 10 s starting at t=0 -> batches at 0,10,...,60
    assert sim.counters["created"] == 7


def test_one_minute_at_10x_advances_600s():
    sim, clock, ctl = make(10.0)
    ctl.start()
    clock.t += 60
    ctl.tick()
    assert sim.now == pytest.approx(600.0)
    assert sim.counters["created"] == 61


def test_acceleration_change_does_not_reset_or_alter_active_durations():
    sim = basic_sim()
    sim.add_recipe({"id": "r", "name": "r", "processing_time_s": 30,
                    "inputs": [{"product_type_id": "ore", "quantity": 1}],
                    "outputs": [{"product_type_id": "metal", "quantity": 1}]})
    sim.add_asset("producer", "P", {"product_type_id": "ore", "interval": 1000}, asset_id="p")
    sim.add_asset("transformer", "T", {"recipe_id": "r", "speed_pct": 50}, asset_id="t")
    sim.add_asset("sink", "S", {}, asset_id="s")
    from conftest import port
    sim.add_conveyor("c1", port("p", "out"), port("t", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c1")
    sim.add_conveyor("c2", port("t", "out0"), port("s", "in0"), {"length_m": 1, "speed_m_s": 1}, asset_id="c2")
    clock = FakeClock()
    ctl = SimulationController(sim, time_fn=clock)
    ctl.start()
    clock.t += 11       # cycle started at t=1, scheduled to end at t=31
    ctl.tick()
    started = sim.query_events(event_type="processing_started")[0]
    assert started.timestamp == pytest.approx(1.0)
    ctl.set_acceleration(10.0)
    assert sim.now == pytest.approx(11.0)       # no reset
    clock.t += 2        # 20 more simulated seconds
    ctl.tick()
    assert sim.now == pytest.approx(31.0)
    done = sim.query_events(event_type="processing_completed")
    assert len(done) == 1 and done[0].timestamp == pytest.approx(31.0)
    assert done[0].timestamp - started.timestamp == pytest.approx(30.0)
    assert len(sim.query_events(event_type="processing_started")) == 1   # no duplicate events


def test_pause_freezes_time_and_resume_continues():
    sim, clock, ctl = make(1.0)
    ctl.start()
    clock.t += 5
    ctl.pause()
    assert sim.now == pytest.approx(5.0)
    clock.t += 100
    ctl.tick()
    assert sim.now == pytest.approx(5.0)
    ctl.resume()
    clock.t += 5
    ctl.tick()
    assert sim.now == pytest.approx(10.0)


def test_controller_validation_and_state_rules():
    sim, clock, ctl = make()
    with pytest.raises(ValidationError):
        ctl.set_acceleration(0)
    with pytest.raises(ValidationError):
        ctl.set_acceleration(-2)
    with pytest.raises(ValidationError):
        ctl.set_acceleration(1e9)
    with pytest.raises(ConflictError):
        ctl.pause()
    ctl.start()
    with pytest.raises(ConflictError):
        ctl.step(5)
    ctl.stop()
    with pytest.raises(ConflictError):
        ctl.start()


def test_real_time_pacing_short_run():
    sim, _, _ = make()
    ctl = SimulationController(sim, acceleration=20.0)
    ctl.start()
    ctl.start_background(period_s=0.01)
    try:
        wall0 = time.monotonic()
        time.sleep(0.5)
        ctl.pause()
        elapsed = time.monotonic() - wall0
    finally:
        ctl.stop_background()
    assert sim.now == pytest.approx(elapsed * 20.0, rel=0.15, abs=0.5)
