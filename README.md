# Industrial Digital Twin Simulator (MVP)

A 2D, *Satisfactory*-style production-line simulator built for digital-twin, traceability and
integration experiments. You can draw a network of **producers → conveyors → transformers
(recipes) → splitters → sinks**, run it in real time or accelerated, change machine configuration
while it runs, and trace every product through its **Digital Product Passport (DPP)**.

* Pure-Python, discrete-event engine (no external simulation library).
* FastAPI REST API and a vanilla-JS canvas editor (no Node build step).
* Product, machine, configuration and metrics events go to MQTT, Kafka, Pulsar or an in-memory broker,
  through an outbox with idempotent event ids.
* Scenarios are JSON import/export. Checkpoints save the complete runtime state.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the policies and the known limitations.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m simulator            # http://127.0.0.1:8000
```

`python -m simulator --host 0.0.0.0 --port 8000 --scenario scenario_b_multi_io` picks the bind address
and the built-in scenario loaded at start-up.

On Linux or macOS, use `.venv/bin/python` instead.

`python -m simulator --checkpoint-dir DIR` stores checkpoints in a different directory. Every option
also reads an environment variable: `SIM_HOST`, `SIM_PORT`, `SIM_SCENARIO` and `SIM_CHECKPOINT_DIR`.

## Docker

```powershell
docker compose up --build                      # http://localhost:8000
docker compose --profile mqtt up --build       # also starts a Mosquitto broker
```

* The image (`Dockerfile`, based on `python:3.12-slim`) runs as a non-root user and has a health check
  on `/api/sim`.
* Checkpoints persist in the `sim-data` volume (`/data/checkpoints`).
* Compose variables:
  * `SIM_PUBLISH_PORT`: host port (default 8000).
  * `SIM_SCENARIO`: scenario loaded at start-up.
  * `SIM_EXTRAS`: broker client libraries baked into the image (default `paho-mqtt`; for example
    `"paho-mqtt kafka-python pulsar-client"`).
* With the `mqtt` profile, enable the integration with
  `{"adapter": "mqtt", "options": {"host": "mosquitto", "port": 1883}}`.

Without Compose:

```powershell
docker build -t industrial-sim --build-arg EXTRAS="paho-mqtt" .
docker run -p 8000:8000 -v sim-data:/data industrial-sim
```

## Using the editor

| Action | How |
|---|---|
| Load a built-in scenario | Toolbar select → **Load** |
| Run | **Start** / **Pause** / **Resume** / **Stop** / **Reset**. **Step** advances N simulated seconds while paused |
| Accelerate | **Speed ×** (0.5 … 1000; the API accepts up to 10000) |
| Add an asset | Left palette (producer, transformer, splitter, sink) |
| Connect | Click an **output port** (right side), then an **input port** (left side). This creates a conveyor |
| Move / delete | Drag a node. Select it and press **Del** |
| Pan / zoom / fit | Drag the background / mouse wheel / double-click the background |
| Inspect | Click an asset to see its state, commands (start/stop/fault/restart), JSON config editor (versioned changes), statistics, operational history, config changes, and products received (sinks) |
| Products / DPP | **Products** tab → search → click a product to see its passport (identity, provenance, lineage tree, full history with inherited entries in italics) |
| Catalog | Product types, recipes, production orders |
| Integration | Enable a broker adapter and inspect the published messages (in-memory adapter) |
| Save state | **Export JSON** / **Import JSON** (scenario definition); **Save ckpt** / **Restore ckpt** (full runtime state) |

Conveyor items are drawn at their real position; defective items are red.

## Built-in scenarios

* **Scenario A – basic screw line**: iron and coal producers → steel furnace (10 iron + 5 coal → 2 steel)
  → screw press → quality splitter (conditional on `is_defective` / quality) → accepted and rejected sinks.
* **Scenario B – multiple inputs and outputs**: a transformer with several inputs, a main product and a
  by-product routed to separate sinks, plus a percentage splitter.

Both run without invariant violations. More scenarios are exercised in the tests: configuration change
mid-cycle (C) and blocked/failed downstream (D).

## REST API (selection)

Interactive OpenAPI docs are at `/docs`.

| Area | Endpoints |
|---|---|
| Clock | `GET /api/sim`, `POST /api/sim/{start,pause,resume,stop,reset,step,acceleration}` |
| State | `GET /api/state`, `/api/metrics`, `/api/validation`, `/api/invariants`, `/api/pending` |
| Catalog | `GET/POST/PUT/DELETE /api/product-types`, `/api/recipes`; `GET/POST /api/orders` |
| Network | `GET/POST /api/assets`, `POST /api/connections`, `GET/DELETE /api/assets/{id}`, `PATCH /api/assets/{id}/config`, `POST /api/assets/{id}/rename`, `PUT /api/assets/{id}/position`, `POST /api/assets/{id}/commands` |
| History | `GET /api/assets/{id}/stats`, `/operational-history`, `/products`; `GET /api/config-changes`; `GET /api/events?since_seq=&event_type=&asset_id=` |
| Traceability | `GET /api/products?…`, `GET /api/products/{id}` (DPP), `GET /api/products/{id}/history`, `GET /api/cycles`, `GET /api/cycles/{id}` |
| Persistence | `GET/POST /api/scenario`, `GET /api/scenarios`, `POST /api/scenarios/{id}/load`, `GET/POST /api/checkpoints`, `POST /api/checkpoints/{name}/restore` |
| Integration | `GET/POST /api/integration`, `GET /api/integration/messages` |

Errors return `{"error": ..., "detail": ...}`: 422 for validation, 404 for not found, 409 for conflict.

### Integration

```powershell
curl -X POST localhost:8000/api/integration -H "Content-Type: application/json" `
     -d '{"adapter": "mqtt", "options": {"host": "localhost", "port": 1883}, "topic_prefix": "plant1"}'
```

Adapters: `memory`, `mqtt` (paho-mqtt), `kafka` (kafka-python) and `pulsar` (pulsar-client). Install the
client library you need; the dependencies are listed, commented out, in `requirements.txt`. Messages are
JSON envelopes `{schema, event_id, category, type, sim_time, published_at, payload}` on the topics
`<prefix>.machine`, `<prefix>.product`, `<prefix>.config` and `<prefix>.metrics`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

There are 66 tests covering:

* timing and the accelerator
* material flow and blocking
* transformer and recipe rules
* config versioning
* traceability and DPP lineage
* REST API
* integration (ordering, retries, idempotency)
* scenarios A–D

## Benchmark

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark.py --lines 10 --sim-seconds 3600 --no-trace
```

Each line is producer → conveyor → transformer → conveyor → conditional splitter → 2 sinks.
These are indicative results on a developer laptop, simulating 3,600 s with `--no-trace`:

| Lines | Wall time | Speed-up vs real time | Events/s | Peak memory (tracemalloc run) |
|---|---|---|---|---|
| 1  | 0.27 s | 13,495× | 37.8k | 16 MB |
| 10 | 3.3 s  | 1,078×  | 30k   | 162 MB |
| 50 | 12.4 s | 289×    | 40k   | 801 MB |

Memory is about 7 KB per product, including the full history and event log. The invariant checks passed
in every run.
