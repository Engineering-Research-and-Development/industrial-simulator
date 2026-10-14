"""External event publishing (message broker adapters).

Architecture:

* The engine notifies in-process listeners synchronously. :class:`IntegrationBridge` is such
  a listener; it only converts events into stable envelopes and appends them to a bounded
  in-memory *outbox*. It never blocks or raises into the engine.
* Delivery happens outside the simulation loop (background thread or explicit
  :meth:`IntegrationBridge.deliver_pending`). Failed deliveries are retried up to
  ``max_attempts`` with the *same* ``event_id`` (at-least-once semantics). Consumers must
  deduplicate by ``event_id`` – see :class:`IdempotentConsumer`.
* Broker failures therefore cannot corrupt simulation state or create duplicate products.

Envelope schema (``schema`` field identifies the version)::

    {"schema": "industrial-sim.<category>.v1", "event_id": str, "category": str,
     "type": str, "sim_time": float, "published_at": float (wall clock), "payload": {...}}
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

from .errors import ValidationError

PRODUCT_EVENT_TYPES = {"product_generated", "product_routed", "product_completed", "product_stored",
                       "product_rejected"}
CATEGORIES = ("machine", "product", "config", "metrics")


class EventPublisher(ABC):
    name = "abstract"

    @abstractmethod
    def publish(self, topic: str, key: str, value: bytes) -> None:
        """Deliver one message; raise on failure (the bridge retries)."""

    def close(self) -> None:
        pass


class InMemoryPublisher(EventPublisher):
    """Test/demo publisher. ``fail_next`` simulates transient broker failures."""

    name = "memory"

    def __init__(self, max_messages: int = 10_000) -> None:
        self.messages: deque[tuple[str, str, bytes]] = deque(maxlen=max_messages)
        self.fail_next = 0
        self.lock = threading.Lock()

    def publish(self, topic: str, key: str, value: bytes) -> None:
        with self.lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ConnectionError("simulated broker failure")
            self.messages.append((topic, key, value))

    def decoded(self) -> list[dict]:
        with self.lock:
            return [json.loads(v) for _, _, v in self.messages]


class MqttPublisher(EventPublisher):  # pragma: no cover - requires an external broker
    name = "mqtt"

    def __init__(self, host: str = "localhost", port: int = 1883, qos: int = 1, **_: Any) -> None:
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except ImportError as exc:
            raise ValidationError("MQTT adapter requires 'paho-mqtt' (pip install paho-mqtt)") from exc
        self.qos = qos
        if hasattr(mqtt, "CallbackAPIVersion"):     # paho-mqtt >= 2.0
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        else:
            self.client = mqtt.Client()
        self.client.connect(host, int(port))
        self.client.loop_start()

    def publish(self, topic: str, key: str, value: bytes) -> None:
        info = self.client.publish(topic.replace(".", "/"), value, qos=self.qos)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise ConnectionError("MQTT publish not acknowledged")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class KafkaPublisher(EventPublisher):  # pragma: no cover - requires an external broker
    name = "kafka"

    def __init__(self, bootstrap_servers: str = "localhost:9092", **_: Any) -> None:
        try:
            from kafka import KafkaProducer  # type: ignore
        except ImportError as exc:
            raise ValidationError("Kafka adapter requires 'kafka-python' (pip install kafka-python)") from exc
        self.producer = KafkaProducer(bootstrap_servers=bootstrap_servers, acks="all", retries=0)

    def publish(self, topic: str, key: str, value: bytes) -> None:
        self.producer.send(topic, key=key.encode(), value=value).get(timeout=10)

    def close(self) -> None:
        self.producer.close()


class PulsarPublisher(EventPublisher):  # pragma: no cover - requires an external broker
    name = "pulsar"

    def __init__(self, service_url: str = "pulsar://localhost:6650", **_: Any) -> None:
        try:
            import pulsar  # type: ignore
        except ImportError as exc:
            raise ValidationError("Pulsar adapter requires 'pulsar-client' (pip install pulsar-client)") from exc
        self.client = pulsar.Client(service_url)
        self.producers: dict[str, Any] = {}

    def publish(self, topic: str, key: str, value: bytes) -> None:
        if topic not in self.producers:
            self.producers[topic] = self.client.create_producer(topic)
        self.producers[topic].send(value, partition_key=key)

    def close(self) -> None:
        self.client.close()


ADAPTERS: dict[str, type[EventPublisher]] = {"memory": InMemoryPublisher, "mqtt": MqttPublisher,
                                             "kafka": KafkaPublisher, "pulsar": PulsarPublisher}


def create_publisher(adapter: str, options: dict | None = None) -> EventPublisher:
    cls = ADAPTERS.get(adapter)
    if cls is None:
        raise ValidationError(f"unknown adapter '{adapter}' (expected one of {list(ADAPTERS)})")
    return cls(**(options or {}))


class IntegrationBridge:
    def __init__(self, publisher: EventPublisher, *, topic_prefix: str = "industrial-sim",
                 categories: list[str] | None = None, max_outbox: int = 100_000, max_attempts: int = 5) -> None:
        cats = categories or list(CATEGORIES)
        bad = [c for c in cats if c not in CATEGORIES]
        if bad:
            raise ValidationError(f"unknown categories {bad}; expected {CATEGORIES}")
        self.publisher = publisher
        self.topic_prefix = topic_prefix
        self.categories = set(cats)
        self.max_attempts = max_attempts
        self.outbox: deque[dict] = deque()
        self.max_outbox = max_outbox
        self.stats = {"enqueued": 0, "delivered": 0, "retries": 0, "failed": 0, "dropped_overflow": 0}
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ engine listener
    def __call__(self, channel: str, payload: dict) -> None:
        if channel == "machine_event":
            category = "product" if payload["event_type"] in PRODUCT_EVENT_TYPES else "machine"
            self._enqueue(category, payload["event_type"], payload["event_id"], payload["timestamp"],
                          payload.get("asset_id", ""), payload)
        elif channel == "config_change":
            self._enqueue("config", "configuration_changed", payload["change_id"], payload["timestamp"],
                          payload["asset_id"], payload)

    def publish_metrics(self, metrics: dict, metric_id: str) -> None:
        self._enqueue("metrics", "metrics", metric_id, metrics.get("sim_time", 0.0), "simulation", metrics)

    def _enqueue(self, category: str, etype: str, event_id: str, sim_time: float, key: str, payload: dict) -> None:
        if category not in self.categories:
            return
        envelope = {"schema": f"industrial-sim.{category}.v1", "event_id": event_id, "category": category,
                    "type": etype, "sim_time": sim_time, "published_at": None, "payload": payload}
        with self.lock:
            if len(self.outbox) >= self.max_outbox:
                self.outbox.popleft()
                self.stats["dropped_overflow"] += 1
            self.outbox.append({"envelope": envelope, "key": key, "attempts": 0})
            self.stats["enqueued"] += 1

    # ------------------------------------------------------------------ delivery
    def topic_for(self, category: str) -> str:
        return f"{self.topic_prefix}.{category}"

    def deliver_pending(self, max_items: int = 10_000) -> int:
        delivered = 0
        for _ in range(max_items):
            with self.lock:
                if not self.outbox:
                    break
                item = self.outbox[0]
            env = item["envelope"]
            env["published_at"] = time.time()
            try:
                self.publisher.publish(self.topic_for(env["category"]), item["key"],
                                       json.dumps(env, default=str).encode("utf-8"))
            except Exception:
                item["attempts"] += 1
                with self.lock:
                    if item["attempts"] >= self.max_attempts:
                        self.outbox.popleft()
                        self.stats["failed"] += 1
                    else:
                        self.stats["retries"] += 1
                break  # keep ordering; retry later
            with self.lock:
                self.outbox.popleft()
                self.stats["delivered"] += 1
            delivered += 1
        return delivered

    def start(self, period_s: float = 0.1) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            backoff = period_s
            while not self._stop.is_set():
                before = self.stats["retries"] + self.stats["failed"]
                self.deliver_pending()
                failed = (self.stats["retries"] + self.stats["failed"]) > before
                backoff = min(5.0, backoff * 2) if failed else period_s
                self._stop.wait(backoff)

        self._thread = threading.Thread(target=loop, name="broker-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            self.publisher.close()
        except Exception:
            pass

    def info(self) -> dict:
        with self.lock:
            return {"adapter": self.publisher.name, "topic_prefix": self.topic_prefix,
                    "categories": sorted(self.categories), "outbox": len(self.outbox), **self.stats}


class IdempotentConsumer:
    """Reference consumer that processes each ``event_id`` at most once."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.processed: list[dict] = []
        self.duplicates = 0

    def handle(self, raw: bytes | dict) -> bool:
        env = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
        if env["event_id"] in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(env["event_id"])
        self.processed.append(env)
        return True
