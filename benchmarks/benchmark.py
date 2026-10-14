"""Throughput/memory benchmark.

Builds N independent lines (producer -> conveyor -> transformer -> conveyor -> splitter -> 2 sinks)
and runs them for a fixed simulated duration as fast as possible (no real-time pacing).

    python benchmarks/benchmark.py --lines 10 50 --sim-seconds 3600
"""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator.engine import Simulation  # noqa: E402


def build(n_lines: int, seed: int = 1, event_log_limit: int | None = None) -> Simulation:
    sim = Simulation(seed=seed, event_log_limit=event_log_limit, name=f"bench-{n_lines}")
    sim.add_product_type({"id": "raw", "name": "Raw"})
    sim.add_product_type({"id": "part", "name": "Part"})
    sim.add_recipe({"id": "machine", "name": "Machine", "processing_time_s": 4,
                    "inputs": [{"product_type_id": "raw", "quantity": 2}],
                    "outputs": [{"product_type_id": "part", "quantity": 1}]})

    def p(a, port):
        return {"asset_id": a, "port": port}

    for i in range(n_lines):
        sim.add_asset("producer", f"P{i}", {"product_type_id": "raw", "batch_quantity": 2, "interval": 5},
                      asset_id=f"p{i}")
        sim.add_asset("transformer", f"T{i}", {"recipe_id": "machine", "speed_pct": 80}, asset_id=f"t{i}")
        sim.add_asset("splitter", f"S{i}", {"mode": "conditional", "fallback_branch": "A",
                                             "rules": [{"attribute": "is_defective", "operator": "eq",
                                                        "value": True, "branch": "B"}]}, asset_id=f"s{i}")
        sim.add_asset("sink", f"OK{i}", {}, asset_id=f"ok{i}")
        sim.add_asset("sink", f"NOK{i}", {}, asset_id=f"nok{i}")
        sim.add_conveyor("c", p(f"p{i}", "out"), p(f"t{i}", "in0"), {"length_m": 10, "speed_m_s": 2}, asset_id=f"c1-{i}")
        sim.add_conveyor("c", p(f"t{i}", "out0"), p(f"s{i}", "in"), {"length_m": 6, "speed_m_s": 2}, asset_id=f"c2-{i}")
        sim.add_conveyor("c", p(f"s{i}", "out_a"), p(f"ok{i}", "in0"), {"length_m": 4, "speed_m_s": 2}, asset_id=f"c3-{i}")
        sim.add_conveyor("c", p(f"s{i}", "out_b"), p(f"nok{i}", "in0"), {"length_m": 4, "speed_m_s": 2}, asset_id=f"c4-{i}")
    return sim


def run(n_lines: int, sim_seconds: float, event_log_limit: int | None, trace: bool = True) -> dict:
    if trace:
        tracemalloc.start()
    sim = build(n_lines, event_log_limit=event_log_limit)
    sim.start()
    t0 = time.perf_counter()
    sim.run_until(sim_seconds)
    wall = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1] if trace else 0
    if trace:
        tracemalloc.stop()
    violations = sim.check_invariants()
    m = sim.metrics()
    return {"lines": n_lines, "assets": len(sim.assets), "sim_s": sim_seconds, "wall_s": round(wall, 3),
            "speedup_x": round(sim_seconds / wall, 1) if wall else float("inf"),
            "events": m["events_processed"], "events_per_s": round(m["events_processed"] / wall) if wall else 0,
            "products": m["products_created"], "peak_mem_mb": round(peak / 1e6, 1) if trace else "n/a", "invariants_ok": not violations}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, nargs="+", default=[1, 10, 50])
    ap.add_argument("--sim-seconds", type=float, default=3600)
    ap.add_argument("--event-log-limit", type=int, default=None)
    ap.add_argument("--no-trace", action="store_true", help="skip tracemalloc (faster, no memory figure)")
    args = ap.parse_args()
    cols = ["lines", "assets", "sim_s", "wall_s", "speedup_x", "events", "events_per_s", "products",
            "peak_mem_mb", "invariants_ok"]
    print(" | ".join(f"{c:>12}" for c in cols))
    for n in args.lines:
        r = run(n, args.sim_seconds, args.event_log_limit, trace=not args.no_trace)
        print(" | ".join(f"{str(r[c]):>12}" for c in cols), flush=True)


if __name__ == "__main__":
    main()
