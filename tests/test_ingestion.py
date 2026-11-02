"""Amendment §6 – canonical envelopes, asset mapping, telemetry, production-cycle sync, hybrid mode,
duplicates, late and out-of-order messages, and the MQTT adapter (with a fake client)."""

import json

import pytest

from industrial_helpers import attribute_sim, envelope, furnace_line, furnace_type
from simulator.errors import ValidationError
from simulator.industrial.ingestion import ingest
from simulator.industrial.mqtt import MqttIngestor, normalize_mqtt_config

EXTERNAL_PROFILE = {
    "sensors": [{"id": "temp", "unit": "degC", "external_key": "TT101"},
                {"id": "pressure", "unit": "bar", "source": "simulated", "model": {"type": "constant", "value": 2.0},
                 "sample_interval_s": 10}],
    "conditions": [{"id": "overheat", "severity": "critical", "activation": "sensor.temp > 1000",
                    "deactivation": "sensor.temp < 950", "action": "block"}],
}


def ext_sim(production="external", **sync):
    sim = attribute_sim()
    furnace_type(sim, profile=EXTERNAL_PROFILE, config={})
    furnace_line(sim, interval=5)
    sim.industrial.set_sync("t", {"mode": "hybrid" if production != "external" else "external",
                                  "external_id": "FURN-1",
                                  "categories": {"production": production, "measurements": "external"}, **sync})
    sim.start()
    sim.run_until(20)
    return sim


# ---------------------------------------------------------------- envelope validation & mapping
def test_envelope_validation_and_asset_mapping():
    sim = ext_sim()
    bad = [None, [], {"event_id": "x"}, {**envelope(1, "telemetry"), "schema_version": "9"},
           {**envelope(2, "nope")}, {**envelope(3, "telemetry"), "measurements": [1, 2]}]
    for b in bad:
        r = ingest(sim, b)
        assert r["status"] == "rejected" and r["errors"], b
    r = ingest(sim, envelope(4, "telemetry", asset_id="UNKNOWN", measurements={"TT101": 1}))
    assert r["status"] == "rejected" and "unknown asset" in r["errors"][0]
    # mapped by external id and by internal asset id
    assert ingest(sim, envelope(5, "telemetry", measurements={"TT101": 10}))["asset_id"] == "t"
    assert ingest(sim, envelope(6, "telemetry", asset_id="t", measurements={"temp": 11}))["status"] == "accepted"
    # simulated sensor and unknown keys are rejected individually -> partial
    r = ingest(sim, envelope(7, "telemetry", measurements={"TT101": 12, "pressure": 3, "bogus": 1}))
    assert r["status"] == "partial" and len(r["errors"]) == 2
    assert sim.industrial.store.latest_value("t", "temp") == 12
    # a production event for an asset whose production is simulated is refused
    sim.industrial.set_sync("p", {"mode": "hybrid", "categories": {"measurements": "external"}})
    r = ingest(sim, envelope(8, "batch_produced", asset_id="p"))
    assert r["status"] == "rejected" and r["error_type"] == "ConflictError"
    st = sim.industrial.ingestion.status()
    assert st["counts"]["rejected"] >= 8


def test_telemetry_units_conditions_and_duplicates():
    sim = ext_sim()
    hub = sim.industrial
    r = ingest(sim, envelope(1, "telemetry", timestamp="2024-01-01T10:00:00Z",
                             measurements={"TT101": {"value": 2000, "unit": "degF", "quality": "good"}}))
    assert r["status"] == "accepted" and r["observations"] == 1
    assert hub.store.latest_value("t", "temp") == pytest.approx(1093.333, rel=1e-4)
    assert hub.classification("t") == "critical" and hub.blocking_condition("t") == "overheat"
    assert ingest(sim, envelope(1, "telemetry", measurements={"TT101": 10}))["status"] == "duplicate"
    assert hub.store.latest_value("t", "temp") == pytest.approx(1093.333, rel=1e-4)
    obs = hub.store.query(asset_id="t", measurement_id="temp")[-1]
    assert obs.source == "rest" and obs.raw_value == 2000 and obs.message_id == "e1"
    assert obs.source_timestamp.startswith("2024-01-01T10:00:00")
    ingest(sim, envelope(2, "telemetry", measurements={"TT101": 900}))
    assert hub.classification("t") == "normal"
    r = ingest(sim, envelope(3, "telemetry", measurements={"TT101": "hot"}))
    assert r["status"] == "rejected"


def test_out_of_order_and_late_messages():
    sim = ext_sim(time_mode="source", max_lateness_s=5, late_policy="store")
    hub = sim.industrial
    ingest(sim, envelope(1, "telemetry", timestamp="2024-01-01T10:00:10Z", measurements={"TT101": 100}))
    r = ingest(sim, envelope(2, "telemetry", timestamp="2024-01-01T10:00:05Z", measurements={"TT101": 50}))
    assert r["status"] == "accepted"
    assert hub.store.latest_value("t", "temp") == 100                       # older reading does not win
    assert hub.store.query(asset_id="t", measurement_id="temp")[-1].quality == "out_of_order"
    sim.run_until(120)
    r = ingest(sim, envelope(3, "telemetry", timestamp="2024-01-01T10:00:20Z", measurements={"TT101": 300}))
    late = hub.store.query(asset_id="t", measurement_id="temp")[-1]
    assert late.quality == "late" and late.sim_time == pytest.approx(30)   # placed at its source time
    assert hub.store.latest_value("t", "temp") == 100
    hub.set_sync("t", {"late_policy": "discard"})
    r = ingest(sim, envelope(4, "telemetry", timestamp="2024-01-01T10:00:21Z", measurements={"TT101": 301}))
    assert r["warnings"] and "discarded" in r["warnings"][0]
    hub.set_sync("t", {"late_policy": "apply"})
    ingest(sim, envelope(5, "telemetry", timestamp="2024-01-01T10:00:22Z", measurements={"TT101": 302}))
    assert hub.store.latest_value("t", "temp") == 302
    assert hub.ingestion.status()["clock_skew"]["t"]["count"] >= 4


# ---------------------------------------------------------------- production lifecycle sync
def test_external_cycles_drive_production_without_duplicates():
    sim = ext_sim()
    hub = sim.industrial
    assert not [c for c in sim.cycles.values() if c.asset_id == "t"]      # no simulated cycles
    r = ingest(sim, envelope(1, "cycle_started", cycle_id="EXT-1", timestamp="2024-01-01T10:00:00Z"))
    assert r["status"] == "accepted"
    cid = r["cycle_id"]
    assert sim.cycles[cid].status == "active" and sim.cycles[cid].source == "external"
    assert ingest(sim, envelope(2, "cycle_started", cycle_id="EXT-1"))["warnings"]   # same cycle twice
    sim.run_until(30)
    ingest(sim, envelope(3, "telemetry", cycle_id="EXT-1", measurements={"TT101": 800}))
    ingest(sim, envelope(4, "telemetry", cycle_id="EXT-1", measurements={"TT101": 840}))
    r = ingest(sim, envelope(5, "cycle_completed", cycle_id="EXT-1",
                             outputs=[{"product_id": "M-1", "attributes": {"batch_code": "B7"}}]))
    assert r["status"] == "accepted" and r["cycle_id"] == cid
    metal = sim.products[r["output_product_ids"][0]]
    assert metal.external_id == "M-1" and metal.product_type_id == "metal"
    assert metal.attributes["batch_code"] == "B7" and metal.attribute_provenance["batch_code"]["kind"] == "external"
    assert metal.attributes["temperature"] == pytest.approx(820)          # mapping still evaluated
    n_products, n_cycles = len(sim.products), len(sim.cycles)
    # replays (same event id) and repeated completions (new event id) never duplicate anything
    assert ingest(sim, envelope(5, "cycle_completed", cycle_id="EXT-1"))["status"] == "duplicate"
    assert "already completed" in ingest(sim, envelope(6, "cycle_completed", cycle_id="EXT-1"))["warnings"][0]
    r = ingest(sim, envelope(7, "cycle_completed", cycle_id="EXT-2", outputs=[{"product_id": "M-1"}]))
    assert r["status"] in ("rejected", "pending")
    assert (len(sim.products), len(sim.cycles)) == (n_products, n_cycles)
    # product update by external id
    r = ingest(sim, envelope(8, "product_updated", product_id="M-1", attributes={"batch_code": "B8"}))
    assert r["status"] == "accepted" and sim.products[metal.id].attributes["batch_code"] == "B8"
    sim.run_until(80)
    assert not sim.check_invariants()
    assert hub.ingestion.status()["mapped_cycles"] >= 1


def test_completion_before_start_is_reconciled_or_rejected():
    sim = ext_sim()
    r = ingest(sim, envelope(1, "cycle_completed", cycle_id="EXT-9"))
    assert r["status"] == "pending"
    r = ingest(sim, envelope(2, "cycle_started", cycle_id="EXT-9"))
    assert any("pending completion" in w for w in r["warnings"])
    assert sim.cycles[r["cycle_id"]].status == "completed"
    sim.industrial.set_sync("t", {"reconciliation": "strict"})
    r = ingest(sim, envelope(3, "cycle_completed", cycle_id="EXT-10"))
    assert r["status"] == "rejected" and r["error_type"] == "ConflictError"
    r = ingest(sim, envelope(4, "cycle_started", cycle_id="EXT-11"))
    r = ingest(sim, envelope(5, "cycle_cancelled", cycle_id="EXT-11"))
    assert r["status"] == "accepted" and sim.cycles[r["cycle_id"]].status == "cancelled"
    sim.run_until(60)
    assert not sim.check_invariants()


def test_external_producer_batches():
    sim = attribute_sim()
    furnace_line(sim, interval=1000)
    sim.industrial.set_sync("p", {"mode": "external", "external_id": "SRC",
                                  "categories": {"production": "external"}})
    sim.start()
    sim.run_until(5)
    r = ingest(sim, envelope(1, "batch_produced", asset_id="SRC", cycle_id="B-1",
                             outputs=[{"product_id": "ORE-1", "attributes": {"purity": 0.75}}]))
    assert r["status"] == "accepted"
    ore = sim.products[r["output_product_ids"][0]]
    assert ore.attributes["purity"] == 0.75 and ore.external_id == "ORE-1"
    assert "already recorded" in ingest(sim, envelope(2, "batch_produced", asset_id="SRC", cycle_id="B-1"))["warnings"][0]
    sim.run_until(100)
    metal = [p for p in sim.products.values() if p.product_type_id == "metal"]
    assert metal and metal[0].attributes["ore_purity"] == 0.75           # lineage-derived attribute
    assert ore.id in metal[0].parent_ids if hasattr(metal[0], "parent_ids") else True


def test_hybrid_mode_keeps_simulated_production_with_external_measurements():
    sim = ext_sim(production="simulation")
    ingest(sim, envelope(1, "telemetry", measurements={"TT101": 700}))
    sim.run_until(200)
    cycles = [c for c in sim.cycles.values() if c.asset_id == "t" and c.status == "completed"]
    assert cycles and all(c.source != "external" for c in cycles)
    assert sim.industrial.store.latest_value("t", "temp") == 700
    assert sim.industrial.store.latest_value("t", "pressure") == 2.0       # still simulated
    ingest(sim, envelope(2, "telemetry", measurements={"TT101": 1200}))   # blocking condition
    n = len([c for c in sim.cycles.values() if c.asset_id == "t"])
    sim.run_until(400)
    assert len([c for c in sim.cycles.values() if c.asset_id == "t"]) <= n + 1
    ingest(sim, envelope(3, "configuration_changed", config={"speed_pct": 65}))
    assert sim.assets["t"].config["speed_pct"] != 65                      # configuration is not external
    sim.industrial.set_sync("t", {"categories": {"configuration": "external", "state": "external"}})
    assert ingest(sim, envelope(4, "configuration_changed", config={"speed_pct": 65}))["status"] == "accepted"
    assert sim.assets["t"].config["speed_pct"] == 65
    assert ingest(sim, envelope(5, "state_changed", state="stopped"))["applied_command"] == "stop"
    assert sim.assets["t"].state == "stopped"


# ---------------------------------------------------------------- MQTT adapter
class FakeClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.subscribed = []
        self.on_connect = self.on_disconnect = self.on_message = None
        self.started = False

    def username_pw_set(self, u, p): self.auth = (u, p)
    def tls_set(self, *a, **k): self.tls = True
    def reconnect_delay_set(self, min_delay, max_delay): self.delays = (min_delay, max_delay)
    def connect_async(self, host, port, keepalive): self.target = (host, port, keepalive)
    def loop_start(self): self.started = True
    def loop_stop(self): self.started = False
    def disconnect(self): pass
    def subscribe(self, topic, qos=0): self.subscribed.append((topic, qos))

    def deliver(self, topic, payload):
        msg = type("M", (), {"topic": topic, "payload": payload.encode() if isinstance(payload, str) else payload})
        self.on_message(self, None, msg)


def test_mqtt_adapter_with_fake_client():
    import threading
    sim = ext_sim()
    clients = []

    def factory(cfg):
        clients.append(FakeClient(cfg))
        return clients[-1]

    ing = MqttIngestor(threading.RLock(), lambda: sim, client_factory=factory)
    info = ing.configure({"host": "broker", "port": 1884, "username": "u", "password": "secret"})
    assert info["config"]["password"] == "***"
    assert normalize_mqtt_config({"password": "***"}, ing.config)["password"] == "secret"
    with pytest.raises(Exception):
        ing.configure({"port": 70000})
    ing.start()
    c = clients[-1]
    assert c.started and c.auth == ("u", "secret") and c.target[:2] == ("broker", 1884)
    c.on_connect(c, None, {}, 0)
    assert ing.info()["status"] == "connected"
    assert {t for t, _ in c.subscribed} == {"industrial/+/telemetry", "industrial/+/events",
                                            "industrial/+/configuration"}
    body = {"schema_version": "1.0", "event_id": "m1", "event_type": "telemetry", "measurements": {"TT101": 777}}
    c.deliver("industrial/FURN-1/telemetry", json.dumps(body))
    assert sim.industrial.store.latest_value("t", "temp") == 777
    assert sim.industrial.store.query(asset_id="t", measurement_id="temp")[-1].source == "mqtt"
    c.deliver("industrial/FURN-1/telemetry", json.dumps(body))                        # duplicate
    c.deliver("industrial/FURN-1/telemetry", "{not json")
    c.deliver("industrial/OTHER/telemetry", json.dumps({**body, "event_id": "m2", "asset_id": "FURN-1"}))
    c.deliver("industrial/FURN-1/events", json.dumps({**body, "event_id": "m3"}))     # wrong topic kind
    c.deliver("industrial/FURN-1/events", json.dumps({"schema_version": "1.0", "event_id": "m4",
                                                      "event_type": "cycle_started", "cycle_id": "X1"}))
    msgs = list(sim.industrial.ingestion.messages)
    statuses = [m["status"] for m in msgs if m.get("transport") == "mqtt"]
    assert statuses.count("accepted") == 2 and statuses.count("duplicate") == 1
    assert statuses.count("rejected") == 3
    assert ing.info()["received"] == 6 and ing.info()["rejected"] == 3
    c.on_disconnect(c, None, 1)
    assert ing.info()["status"] in ("reconnecting", "disconnected")
    ing.stop()
    assert ing.info()["status"] == "disabled" and not c.started


def test_mqtt_connect_failure_is_reported_not_raised():
    import threading

    class Unreachable(FakeClient):
        def connect_async(self, host, port, keepalive):
            raise OSError("connection refused")

    ing = MqttIngestor(threading.RLock(), lambda: None, client_factory=Unreachable)
    info = ing.start()
    assert info["status"] == "error" and "refused" in info["last_error"]

    def broken(cfg):
        raise ImportError("paho-mqtt is not installed")

    ing = MqttIngestor(threading.RLock(), lambda: None, client_factory=broken)
    with pytest.raises(ValidationError):
        ing.start()
    assert ing.info()["status"] == "error"
