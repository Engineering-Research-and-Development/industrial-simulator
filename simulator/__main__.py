"""Run the simulator server: ``python -m simulator [--host H] [--port P] [--scenario ID]``."""

from __future__ import annotations

import argparse

import uvicorn

from .api import SimulationService, create_app
from .scenario import load_scenario, read_builtin_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="Industrial Digital Twin Simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scenario", default="scenario_a_basic_line",
                        help="built-in scenario to load at startup ('' for an empty simulation)")
    args = parser.parse_args()
    service = SimulationService(load_scenario(read_builtin_scenario(args.scenario)) if args.scenario else None)
    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
