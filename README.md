# Industrial Plant Simulator

A 2D, *[Satisfactory](https://it.wikipedia.org/wiki/Satisfactory)*-style production-line simulator built for digital-twins, traceability and
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
| Load a scenario | Toolbar select (every file in `scenarios/`) → **Load**. The current scenario name and file are shown on the left of the toolbar |
| New scenario | **New…** asks for a name, file name, description and seed, then creates `scenarios/<file>.json` with an empty plant. Product types, recipes, production orders and asset types are kept (both can be unticked) |
| Save / rename | **Save** writes the current plant to its scenario file; **Save as…** writes a new file and switches to it; **Rename…** changes the display name and/or file name. Existing files are never overwritten silently |
| Run | **Start** / **Pause** / **Resume** / **Stop** / **Reset**. **Step** advances N simulated seconds while paused |
| Accelerate | **Speed ×** (0.5 … 1000; the API accepts up to 10000) |
| Add an asset | Left palette (producer, transformer, splitter, sink) opens a form dialog with name, settings and a live preview (throughput, cycle time, quality) |
| Connect | Click an **output port** (right side), then an **input port** (left side). This creates a conveyor |
| Move / delete | Drag a node. Select it and press **Del** |
| Copy / paste | Select a machine and press **Ctrl+C**, then **Ctrl+V** (or **Ctrl+D** / **Duplicate** in the inspector). The copy keeps the full configuration; connections are not copied |
| Ports | Transformers show one input/output port per recipe slot, labelled with quantity and product type (e.g. `10× Iron billet`). Sinks have a single input |
| Pan / zoom / fit | Drag the background / mouse wheel / double-click the background |
| Inspect | Click an asset. The inspector is a stack of sections (overview and commands, configuration, industrial profile, statistics, operational history, config changes, received products for sinks, API). Each section has a header with a summary and action buttons and can be collapsed. Basic settings are edited inline (**Apply changes** sends only the changed fields as a versioned change). Advanced groups such as buffers, quality curves and **attribute mappings** appear as one-line summaries with an **Edit…** button that opens a modal, and **{ } JSON…** opens the raw configuration. During a run the sections update in place, so the layout does not jump and inputs being edited are kept |
| Products / DPP | **Products** tab → search → click a product to see its passport (identity, provenance, lineage tree, history: inherited entries from the same batch or cycle are merged into one step with a product count, and attributes are shown as chips) |
| Catalog | Cards for product types, recipes and production orders with **+ New**, **Edit** and **Delete**. Recipes are built visually: input/output rows (type, quantity, by-product), processing time, defect policy, parameters |
| Integration | Choose an adapter and fill in its connection fields (host/port/QoS, bootstrap servers, service URL), topic prefix and event categories; inspect published messages (in-memory adapter). Below it, **External data ingestion** configures the MQTT subscriber, shows ingestion counters and recent/rejected messages, and sends test envelopes over REST |
| Industrial profile | Inspector section of every asset: bound asset type (bind, detach, update to the latest version with a preview), live measurements, parameters, sensors, composites, conditions and behaviors with origin badges (*template*, *override*, *local*) and **Override** / **Reset** buttons, local definitions (JSON), an expression tester, a synchronisation summary with **Edit…** (modal) and the last evaluated features |
| Types | Asset-type cards: create/edit with a form (name, ID, asset class, description, template config, parameters, sensors) or as JSON (**Edit as JSON…**, with expression tester, for composites, conditions and behaviors); every save is a new version. Duplicate, delete, **Deploy** a bound instance, and update outdated instances after a preview. Every asset class has a default type (`default-producer`, `default-transformer`, …) that new assets are bound to automatically |
| Datasets | Continuous collector and collection sessions: create/edit (datasets, assets, measurements, product types, attributes, aggregations, sampling, start/end conditions, missing-value policy, format), start/pause/resume/stop/clear, preview tables, per-dataset export, ZIP bundle with manifest |
| Save state | **Export JSON** / **Import JSON** (scenario definition); **Save ckpt** / **Restore ckpt** (full runtime state) |

Conveyor items are drawn at their real position; defective items are red. Port and conveyor labels are
placed so that they do not overlap nodes or each other (long port labels are shortened with "…").

## Built-in scenarios

* **Scenario A – basic screw line**: iron and coal producers → steel furnace (10 iron + 5 coal → 2 steel)
  → screw press → quality splitter (conditional on `is_defective` / quality) → accepted and rejected sinks.
* **Scenario B – multiple inputs and outputs**: a transformer with several inputs, a main product and a
  by-product routed to separate sinks, plus a percentage splitter.
* **Scenario C – instrumented smelting furnaces**: two instances of the `furnace` asset type
  (temperature/pressure sensors, composite health, `cold` and `overpressure` working conditions, a
  speed behavior). *Furnace 2* runs in **hybrid** mode: its measurements are ingested externally as
  `FURN-2` (sensor keys `TT101`, `PT102`). Ingot and slag attributes are mapped from measurements, input
  products, conditions and cycle context. A ready-to-start dataset session collects the first hour.

All three run without invariant violations. More scenarios are exercised in the tests: configuration change
mid-cycle (C) and blocked/failed downstream (D).

## REST API (selection)

Interactive OpenAPI docs are at `/docs` (also linked from the **API docs** toolbar button); ReDoc is at `/redoc` and the raw schema at `/openapi.json`.

### Finding the API for a device

Select a machine on the canvas and open **API for this device** in the inspector. It lists every endpoint for that asset with its real ID, and a **curl** button copies a ready-to-run command for each one. The main per-device routes are:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/assets/{id}` | State, config, counters, statistics |
| GET | `/api/assets/{id}/stats` | Statistics only |
| GET | `/api/assets/{id}/operational-history` | State intervals |
| GET | `/api/events?asset_id={id}` | Machine events |
| GET | `/api/config-changes?asset_id={id}` | Versioned config changes |
| GET | `/api/cycles?asset_id={id}` | Production cycles (producers, transformers) |
| GET | `/api/assets/{id}/products` | Received products (sinks) |
| PATCH | `/api/assets/{id}/config` | Partial config update, e.g. `{"speed_pct": 70}` |
| POST | `/api/assets/{id}/commands` | `{"command": "start" \| "stop" \| "fault" \| "restart"}` |

When a broker adapter is enabled, the device publishes on `<topic_prefix>.machine`, `.product` and `.config`, using the asset ID as the message key.

| Area | Endpoints |
|---|---|
| Clock | `GET /api/sim`, `POST /api/sim/{start,pause,resume,stop,reset,step,acceleration}` |
| State | `GET /api/state`, `/api/metrics`, `/api/validation`, `/api/invariants`, `/api/pending` |
| Catalog | `GET/POST/PUT/DELETE /api/product-types`, `/api/recipes`, `/api/orders` |
| Network | `GET/POST /api/assets`, `POST /api/connections`, `GET/DELETE /api/assets/{id}`, `PATCH /api/assets/{id}/config`, `POST /api/assets/{id}/rename`, `PUT /api/assets/{id}/position`, `POST /api/assets/{id}/commands` |
| History | `GET /api/assets/{id}/stats`, `/operational-history`, `/products`; `GET /api/config-changes`; `GET /api/events?since_seq=&event_type=&asset_id=` |
| Traceability | `GET /api/products?…`, `GET /api/products/{id}` (DPP), `GET /api/products/{id}/history`, `GET /api/cycles`, `GET /api/cycles/{id}` |
| Persistence | `GET/POST /api/scenario`, `GET /api/scenarios`, `POST /api/scenarios/{id}/load`, `GET/POST /api/checkpoints`, `POST /api/checkpoints/{name}/restore` |
| Scenario files | `GET /api/scenarios/current` (`{id, name, description, saved}`), `POST /api/scenarios` (new empty plant in a new file: `{name, id?, description?, seed?, keep_catalog?, keep_asset_types?}`; 409 if the file exists), `PUT /api/scenarios/{id}?overwrite=` (save the current plant), `POST /api/scenarios/{id}/rename` (`{new_id?, name?}`) |
| Integration | `GET/POST /api/integration`, `GET /api/integration/messages` |
| Asset types | `GET/POST /api/asset-types`, `GET/PUT/DELETE /api/asset-types/{id}`, `POST /api/asset-types/{id}/duplicate`, `/deploy`, `/sync?dry_run=` |
| Industrial profile | `GET /api/assets/{id}/industrial`, `PUT /api/assets/{id}/parameters`, `PUT/DELETE /api/assets/{id}/overrides/{section}/{key}`, `PUT /api/assets/{id}/local`, `POST /api/assets/{id}/bind`, `/unbind`, `/sync-type?dry_run=`, `GET /api/assets/{id}/measurements`, `/conditions`, `GET /api/conditions/log`, `POST /api/expressions/validate`, `GET /api/units` |
| Synchronisation & ingestion | `GET/PUT /api/assets/{id}/sync`, `POST /api/ingestion/events` (one envelope or an array of up to 1000), `GET /api/ingestion/status`, `/messages?status=`, `GET/PUT /api/ingestion/mqtt`, `POST /api/ingestion/mqtt/{start,stop}` |
| Datasets | `GET /api/datasets`, `POST /api/datasets/sessions`, `GET/PUT/DELETE /api/datasets/sessions/{id}`, `POST /api/datasets/sessions/{id}/{start,pause,resume,stop,clear}`, `GET …/preview/{dataset}`, `…/export/{dataset}?format=`, `…/manifest`, `…/bundle` (the continuous collector has id `continuous`) |

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

## Industrial sensing, synchronisation and datasets

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#industrial-extension) for the full model. In short:

* **Asset types** are versioned templates (kind, config and a *profile*: parameters, sensors, composites,
  working conditions, behaviors). Deployed instances keep the version they were synchronised with;
  they can override config values, parameters and individual profile items, and add local items.
* **Sensors** are simulated (constant, expression, first-order lag, random walk; noise, gain, offset,
  resolution, range, filters) or fed externally. **Composites**, **conditions** and **behaviors** use a
  safe expression language, e.g. `clamp(sensor.temp / param.setpoint, 0, 1)`.
* **Working conditions** activate/deactivate with hysteresis and delays, modify cycle features
  (`processing_time_s`, `defect_probability`, …) and can block, stop or fault the machine.
* **Synchronisation**: each asset runs in `simulation`, `external` or `hybrid` mode, per category
  (`production`, `measurements`, `configuration`, `state`). External data arrives as envelopes:

```powershell
curl -X POST localhost:8000/api/ingestion/events -H "Content-Type: application/json" -d '{
  "schema_version": "1.0", "event_id": "evt-1", "asset_id": "FURN-2", "event_type": "telemetry",
  "timestamp": "2024-01-01T10:00:00Z", "measurements": {"TT101": {"value": 1395.2, "unit": "degC"}, "PT102": 1.31}}'
```

  Event types: `telemetry`, `cycle_started`, `cycle_completed`, `cycle_cancelled`, `batch_produced`,
  `product_updated`, `configuration_changed` and `state_changed`. Duplicates (same `event_id`) are ignored,
  and invalid messages are rejected without side effects. MQTT ingestion (`paho-mqtt`) subscribes to
  configurable topics carrying the same envelopes.
* **Product attributes** are declared on product types (type, unit, range, allowed values, required,
  default). Recipe outputs and producers fill them through **attribute mappings** from constants,
  parameters, measurements (cycle aggregations), features, input products (aggregations),
  expressions, the production order, the cycle and conditions. Every value records its provenance
  in the DPP.
* **Datasets**: a continuous collector and named collection sessions build `sensor`, `cycle`, `product`
  and `machine` datasets. These join on `asset_id`, `cycle_id`, `product_id` and `production_order_ids`,
  and export as CSV or JSONL, individually or as a ZIP bundle with a manifest.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

There are 125 tests covering:

* timing and the accelerator
* material flow and blocking
* transformer and recipe rules
* config versioning
* traceability and DPP lineage
* REST API
* integration (ordering, retries, idempotency)
* scenarios A–D and the built-in scenarios (no invariant violations)
* asset types: versioning, deployment, overrides, outdated instances and synchronisation
* the expression language, sensors, composites, working conditions and behaviors
* product attributes and attribute mappings (provenance, validation, by-products)
* ingestion over REST and MQTT: deduplication, late and out-of-order data, unit conversion,
  strict/lenient reconciliation, hybrid mode
* dataset collection, sessions and CSV/JSONL/ZIP export
* the industrial REST API and the furnace example scenario (scenario C)
* scenario files: new empty plant that keeps the catalogues, save, save as, rename, conflicts, and
  default asset types bound to new assets

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
