"""MQTT ingestion: subscribes to configurable topics and forwards canonical envelopes to
:func:`simulator.industrial.ingestion.ingest` under the controller lock.

Broker connection details (host, credentials, TLS) are kept here, separate from asset
configuration; asset-level behaviour (which categories are external, external ids) lives in each
asset's sync configuration. When the broker is unavailable the client keeps retrying in the
background while the simulation continues in simulation mode.

The MQTT client is created through an injectable factory (paho-mqtt by default) so the layer can
be exercised without a broker.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections import deque
from typing import Any, Callable

from ..errors import ConflictError, ValidationError
from .ingestion import CATEGORY, MAX_MESSAGE_BYTES, ingest

TOPIC_KINDS = ("telemetry", "events", "configuration")
TOPIC_CATEGORIES = {"telemetry": {"measurements"}, "events": {"production"},
                    "configuration": {"configuration", "state"}}
DEFAULT_TOPICS = {"telemetry": "industrial/{asset_id}/telemetry", "events": "industrial/{asset_id}/events",
                  "configuration": "industrial/{asset_id}/configuration"}


def default_mqtt_config() -> dict:
    return {"host": "localhost", "port": 1883, "client_id": "industrial-sim-ingest", "username": None,
            "password": None, "tls": False, "keepalive_s": 60, "qos": 1, "topics": dict(DEFAULT_TOPICS),
            "reconnect_min_s": 1, "reconnect_max_s": 60}


def normalize_mqtt_config(raw: Any, current: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("MQTT configuration must be an object")
    base = copy.deepcopy(current or default_mqtt_config())
    unknown = set(raw) - set(base)
    if unknown:
        raise ValidationError(f"unknown MQTT field(s) {sorted(unknown)}")
    cfg = {**base, **{k: copy.deepcopy(v) for k, v in raw.items() if k != "topics"}}
    if "password" in raw and raw["password"] == "***":
        cfg["password"] = base.get("password")       # masked value echoed back by the UI
    if not isinstance(cfg["host"], str) or not cfg["host"].strip() or len(cfg["host"]) > 255:
        raise ValidationError("host must be a non-empty string")
    for key, lo, hi in (("port", 1, 65535), ("keepalive_s", 5, 3600), ("qos", 0, 2), ("reconnect_min_s", 1, 3600),
                        ("reconnect_max_s", 1, 3600)):
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValidationError(f"{key} must be an integer in [{lo}, {hi}]")
    if cfg["reconnect_max_s"] < cfg["reconnect_min_s"]:
        raise ValidationError("reconnect_max_s must be >= reconnect_min_s")
    for key in ("client_id", "username", "password"):
        if cfg[key] is not None and not isinstance(cfg[key], str):
            raise ValidationError(f"{key} must be a string or null")
    if not isinstance(cfg["tls"], bool):
        raise ValidationError("tls must be a boolean")
    topics = dict(base["topics"])
    if "topics" in raw:
        if not isinstance(raw["topics"], dict):
            raise ValidationError("topics must be an object")
        for k, v in raw["topics"].items():
            if k not in TOPIC_KINDS:
                raise ValidationError(f"unknown topic kind '{k}' (expected {list(TOPIC_KINDS)})")
            if v in (None, ""):
                topics[k] = None
                continue
            if not isinstance(v, str) or len(v) > 300 or any(c in v for c in "#+\x00"):
                raise ValidationError(f"topics.{k} must be a topic template without wildcards")
            if v.count("{asset_id}") > 1:
                raise ValidationError(f"topics.{k} may contain at most one {{asset_id}} placeholder")
            if "{" in v.replace("{asset_id}", "") or "}" in v.replace("{asset_id}", ""):
                raise ValidationError(f"topics.{k}: only the {{asset_id}} placeholder is supported")
            topics[k] = v
    cfg["topics"] = topics
    return cfg


def _template_regex(tpl: str) -> re.Pattern:
    parts = tpl.split("{asset_id}")
    pat = "(?P<asset_id>[^/]+)".join(re.escape(p) for p in parts)
    return re.compile(f"^{pat}$")


def _paho_factory(cfg: dict):  # pragma: no cover - requires paho-mqtt
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError as exc:
        raise ValidationError("MQTT ingestion requires 'paho-mqtt' (pip install paho-mqtt)") from exc
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg["client_id"] or "")
    return mqtt.Client(client_id=cfg["client_id"] or "")


class MqttIngestor:
    def __init__(self, lock, sim_getter: Callable[[], Any], *,
                 client_factory: Callable[[dict], Any] | None = None,
                 before_ingest: Callable[[], None] | None = None) -> None:
        self.lock = lock
        self.sim_getter = sim_getter
        self.before_ingest = before_ingest
        self.client_factory = client_factory or _paho_factory
        self.config = default_mqtt_config()
        self.client = None
        self.status = "disabled"
        self.last_error: str | None = None
        self.connects = 0
        self.disconnects = 0
        self.received = 0
        self.rejected = 0
        self.recent: deque[dict] = deque(maxlen=100)
        self._mu = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def configure(self, raw: dict) -> dict:
        with self._mu:
            self.config = normalize_mqtt_config(raw, self.config)
        return self.info()

    def start(self) -> dict:
        self.stop()
        cfg = self.config
        try:
            client = self.client_factory(cfg)
        except Exception as exc:
            self.status, self.last_error = "error", str(exc)
            raise ValidationError(f"cannot create MQTT client: {exc}") from exc
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        if cfg["username"]:
            client.username_pw_set(cfg["username"], cfg["password"])
        if cfg["tls"]:
            client.tls_set()
        if hasattr(client, "reconnect_delay_set"):
            client.reconnect_delay_set(min_delay=cfg["reconnect_min_s"], max_delay=cfg["reconnect_max_s"])
        self.client = client
        self.status = "connecting"
        self.last_error = None
        try:
            # connect_async never blocks; paho keeps retrying in its network loop if the broker is down
            connect = getattr(client, "connect_async", None) or client.connect
            connect(cfg["host"], cfg["port"], cfg["keepalive_s"])
            client.loop_start()
        except Exception as exc:  # broker unreachable: keep the simulator running
            self.status, self.last_error = "error", str(exc)
        return self.info()

    def stop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
        self.status = "disabled"

    def subscriptions(self) -> list[str]:
        return [t.replace("{asset_id}", "+") for t in self.config["topics"].values() if t]

    # ------------------------------------------------------------------ callbacks (client thread)
    def _on_connect(self, client, userdata, flags, rc, *_: Any) -> None:
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc not in (0, None)
        if failed:
            self.status, self.last_error = "error", f"connection refused (code {rc})"
            return
        self.status = "connected"
        self.connects += 1
        for topic in self.subscriptions():
            client.subscribe(topic, qos=self.config["qos"])

    def _on_disconnect(self, client, userdata, *args: Any) -> None:
        self.disconnects += 1
        if self.client is not None:
            self.status = "reconnecting"

    def _on_message(self, client, userdata, msg) -> None:
        self.handle_message(msg.topic, msg.payload)

    # ------------------------------------------------------------------ message handling
    def match_topic(self, topic: str) -> tuple[str, str | None] | None:
        for kind, tpl in self.config["topics"].items():
            if not tpl:
                continue
            m = _template_regex(tpl).match(topic)
            if m:
                return kind, m.groupdict().get("asset_id")
        return None

    def handle_message(self, topic: str, payload: bytes | str) -> dict:
        self.received += 1
        result = self._handle(topic, payload)
        if result.get("status") == "rejected":
            self.rejected += 1
        self.recent.append({"topic": topic, "status": result.get("status"), "event_id": result.get("event_id"),
                            "errors": list(result.get("errors") or [])})
        return result

    def _handle(self, topic: str, payload: bytes | str) -> dict:
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
        matched = self.match_topic(topic)
        err = None
        env = None
        if matched is None:
            err = f"topic '{topic}' does not match any configured topic template"
        elif len(raw) > MAX_MESSAGE_BYTES:
            err = f"message exceeds {MAX_MESSAGE_BYTES} bytes"
        else:
            try:
                env = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                err = f"invalid JSON payload: {exc}"
        if err is None and not isinstance(env, dict):
            err = "message must be a JSON object"
        if err is None:
            kind, topic_asset = matched
            if topic_asset is not None:
                if env.get("asset_id") is None:
                    env["asset_id"] = topic_asset
                elif env.get("asset_id") != topic_asset:
                    err = f"asset_id '{env.get('asset_id')}' does not match topic asset '{topic_asset}'"
            et = env.get("event_type")
            if err is None and et in CATEGORY and CATEGORY[et] not in TOPIC_CATEGORIES[kind]:
                err = f"event_type '{et}' is not allowed on {kind} topics"
        with self.lock:
            if self.before_ingest is not None:
                self.before_ingest()
            sim = self.sim_getter()
            if err is not None:
                rec = {"received_at": sim.now, "transport": "mqtt", "topic": topic, "status": "rejected",
                       "event_id": env.get("event_id") if isinstance(env, dict) else None,
                       "asset_id": env.get("asset_id") if isinstance(env, dict) else None,
                       "event_type": env.get("event_type") if isinstance(env, dict) else None,
                       "errors": [err], "warnings": [], "error_type": "ValidationError"}
                st = sim.industrial.ingestion
                st.counts["rejected"] += 1
                st.counts["transport:mqtt"] += 1
                st.messages.append(rec)
                return rec
            return ingest(sim, env, transport="mqtt", topic=topic)

    # ------------------------------------------------------------------ views
    def masked_config(self) -> dict:
        cfg = copy.deepcopy(self.config)
        if cfg.get("password"):
            cfg["password"] = "***"
        return cfg

    def info(self) -> dict:
        return {"status": self.status, "enabled": self.client is not None, "last_error": self.last_error,
                "config": self.masked_config(), "subscriptions": self.subscriptions(), "connects": self.connects,
                "disconnects": self.disconnects, "received": self.received, "rejected": self.rejected,
                "recent": list(self.recent)[-20:]}

    def require_enabled(self) -> None:
        if self.client is None:
            raise ConflictError("MQTT ingestion is not enabled")


__all__ = ["MqttIngestor", "normalize_mqtt_config", "default_mqtt_config", "DEFAULT_TOPICS"]
