"""Run the simulator server: ``python -m simulator [--host H] [--port P] [--scenario ID]``."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .api import SimulationService, create_app
from .scenario import load_scenario, read_builtin_scenario


def main() -> None:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Industrial Digital Twin Simulator")
    parser.add_argument("--host", default=env("SIM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "8000")))
    parser.add_argument("--scenario", default=env("SIM_SCENARIO", "scenario_a_basic_line"),
                        help="built-in scenario to load at startup ('' for an empty simulation)")
    parser.add_argument("--checkpoint-dir", default=env("SIM_CHECKPOINT_DIR") or None,
                        help="directory for checkpoint files (default: ./checkpoints)")
    args = parser.parse_args()
    sim = load_scenario(read_builtin_scenario(args.scenario)) if args.scenario else None
    ckpt_dir = Path(args.checkpoint_dir) if args.checkpoint_dir else None
    if ckpt_dir is not None:
        ckpt_dir.mkdir(parents=True, exist_ok=True)
    service = SimulationService(sim, checkpoint_dir=ckpt_dir)
    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
