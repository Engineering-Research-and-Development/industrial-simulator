"""Spec 18.6 / Scenario D – broker integration, failures, duplicates."""

import json

import pytest

from simulator.errors import ValidationError
from simulator.integration import IdempotentConsumer, InMemoryPublisher, IntegrationBridge, create_publisher
from simulator.scenario import load_scenario, read_builtin_scenario


def setup(**kw):
    sim = load_scenario(read_builtin_scenario("scenario_a_basic_line"))
    pub = InMemoryPublisher()
    bridge = IntegrationBridge(pub, **kw)
    sim.listeners.append(bridge)
    return sim, pub, bridge


def test_events_published_with_envelope():
    sim, pub, bridge = setup()
    sim.start()
    sim.run_until(100)
    sim.update_config("screw-press", {"speed_pct": 60})
    bridge.publish_metrics(sim.metrics(), "MET-1")
    bridge.deliver_pending()
    msgs = pub.decoded()
    assert msgs
    topics = {t for t, _, _ in pub.messages}
    assert {"industrial-sim.machine", "industrial-sim.product", "industrial-sim.config",
            "industrial-sim.metrics"} <= topics
    for m in msgs:
        assert set(m) >= {"schema", "event_id", "category", "type", "sim_time", "payload"}
        assert m["schema"].endswith(".v1")
    assert len({m["event_id"] for m in msgs}) == len(msgs)
    assert bridge.info()["delivered"] == len(msgs)


def test_broker_failure_does_not_affect_simulation_and_retries():
    sim, pub, bridge = setup()
    pub.fail_next = 3
    sim.start()
    sim.run_until(100)
    created = sim.counters["created"]
    for _ in range(5):
        bridge.deliver_pending()
    assert bridge.info()["retries"] == 3
    assert bridge.info()["outbox"] == 0
    assert bridge.info()["failed"] == 0
    assert len(pub.messages) == bridge.info()["enqueued"]
    assert sim.counters["created"] == created and not sim.check_invariants()


def test_listener_exceptions_are_isolated():
    sim = load_scenario(read_builtin_scenario("scenario_a_basic_line"))

    def boom(channel, payload):
        raise RuntimeError("listener failure")

    sim.listeners.append(boom)
    sim.start()
    sim.run_until(100)
    assert sim.listener_errors > 0
    assert not sim.check_invariants()


def test_duplicate_delivery_is_idempotent_for_consumers():
    sim, pub, bridge = setup(categories=["product"])
    sim.start()
    sim.run_until(60)
    bridge.deliver_pending()
    raw = [v for _, _, v in pub.messages]
    consumer = IdempotentConsumer()
    for v in raw + raw:                    # every message delivered twice
        consumer.handle(v)
    assert consumer.duplicates == len(raw)
    assert len(consumer.processed) == len(raw)
    generated = [m for m in consumer.processed if m["type"] == "product_generated"]
    assert len(generated) == len({m["event_id"] for m in generated})


def test_permanent_failure_drops_after_max_attempts():
    sim, pub, bridge = setup(max_attempts=2)
    pub.fail_next = 10**6
    sim.start()
    sim.run_until(30)
    enq = bridge.info()["enqueued"]
    for _ in range(enq * 2 + 2):
        bridge.deliver_pending()
    assert bridge.info()["failed"] == enq and bridge.info()["outbox"] == 0


def test_adapter_selection_errors():
    with pytest.raises(ValidationError):
        create_publisher("carrier-pigeon")
    with pytest.raises(ValidationError):
        IntegrationBridge(InMemoryPublisher(), categories=["nope"])


def test_api_integration_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from simulator.api import SimulationService, create_app
    svc = SimulationService(load_scenario(read_builtin_scenario("scenario_a_basic_line")),
                            checkpoint_dir=tmp_path, background=False)
    with TestClient(create_app(svc)) as c:
        assert c.post("/api/integration", json={"adapter": "memory"}).json()["adapter"] == "memory"
        c.post("/api/sim/step", json={"seconds": 30})
        svc.bridge.deliver_pending()
        msgs = c.get("/api/integration/messages?limit=5").json()
        assert len(msgs) == 5 and json.dumps(msgs)
        # integration survives scenario reload
        c.post("/api/sim/reset")
        c.post("/api/sim/step", json={"seconds": 30})
        assert svc.bridge.info()["outbox"] > 0
        assert c.post("/api/integration", json={"adapter": "none"}).json() == {"adapter": None}
        assert c.post("/api/integration", json={"adapter": "bogus"}).status_code == 422
