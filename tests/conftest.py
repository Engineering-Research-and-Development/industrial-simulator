import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator.engine import Simulation  # noqa: E402


def port(asset_id: str, p: str) -> dict:
    return {"asset_id": asset_id, "port": p}


def basic_sim(seed: int = 1) -> Simulation:
    sim = Simulation(seed=seed)
    for t in ("ore", "coal", "metal", "slag", "widget"):
        sim.add_product_type({"id": t, "name": t.title()})
    return sim


def line(sim: Simulation, *, batch=1, interval=10.0, length=10.0, speed=1.0, cap_per_m=1.0,
         sink_cfg=None, producer_cfg=None) -> None:
    """producer(ore) -> conveyor -> sink"""
    sim.add_asset("producer", "P", {"product_type_id": "ore", "batch_quantity": batch, "interval": interval,
                                    **(producer_cfg or {})}, asset_id="p")
    sim.add_asset("sink", "S", sink_cfg or {}, asset_id="s")
    sim.add_conveyor("C", port("p", "out"), port("s", "in0"),
                     {"length_m": length, "speed_m_s": speed, "capacity_per_m": cap_per_m}, asset_id="c")


@pytest.fixture
def sim() -> Simulation:
    return basic_sim()
