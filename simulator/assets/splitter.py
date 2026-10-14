"""Splitters: route each product from one input to exactly one of two output branches.

* ``percentage`` mode uses *largest-deficit* deterministic allocation: for the (n+1)-th
  product, the branch whose cumulative count is furthest below its target share is chosen;
  exact ties alternate. This never systematically favours a branch and keeps the absolute
  deviation from the target below one product at all times. Counters restart when the
  percentage or mode changes.
* ``conditional`` mode evaluates ``rules`` in list order (list order = priority); the first
  matching rule decides the branch. If no rule matches – including rules skipped because
  the attribute is missing – the product goes to ``fallback_branch``.

Routing is decided once, when the splitter accepts the product, and is recorded in the
product history and in a ``product_routed`` event together with the effective config version.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from ..conditions import evaluate, validate_condition
from ..errors import ValidationError
from ..models import Product, ProductStatus, require_positive_int
from .base import BLOCKED, IDLE, RUNNING, Asset, parse_initial_state

BRANCHES = ("A", "B")
BRANCH_PORT = {"A": "out_a", "B": "out_b"}


class Splitter(Asset):
    kind = "splitter"
    input_port_names = ("in",)
    output_port_names = ("out_a", "out_b")

    def __init__(self, sim, asset_id, name, config, position=None) -> None:
        self.queues: dict[str, deque[str]] = {"A": deque(), "B": deque()}
        self.alloc_counts = {"A": 0, "B": 0}
        self.routed_totals = {"A": 0, "B": 0}
        self._last_tie_branch = "B"
        super().__init__(sim, asset_id, name, config, position)

    @classmethod
    def parse_config(cls, raw: dict, sim) -> dict:
        mode = raw.get("mode", "percentage")
        if mode not in ("percentage", "conditional"):
            raise ValidationError("splitter mode must be 'percentage' or 'conditional'")
        pct = raw.get("percentage_a", 50.0)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not (0 <= pct <= 100):
            raise ValidationError("percentage_a must be within [0, 100]")
        rules_raw = raw.get("rules") or []
        if not isinstance(rules_raw, list):
            raise ValidationError("rules must be a list")
        rules = []
        for i, r in enumerate(rules_raw):
            cond = validate_condition(r, f"rules[{i}]")
            branch = r.get("branch")
            if branch not in BRANCHES:
                raise ValidationError(f"rules[{i}].branch must be 'A' or 'B'")
            cond["branch"] = branch
            if cond["attribute"] == "product_type_id" and cond["operator"] in ("eq", "ne") \
                    and cond["value"] not in sim.product_types:
                raise ValidationError(f"rules[{i}] references unknown product type '{cond['value']}'")
            rules.append(cond)
        if mode == "conditional" and not rules:
            raise ValidationError("conditional splitter requires at least one rule")
        fallback = raw.get("fallback_branch", "B")
        if fallback not in BRANCHES:
            raise ValidationError("fallback_branch must be 'A' or 'B'")
        return {"mode": mode, "percentage_a": float(pct), "rules": rules, "fallback_branch": fallback,
                "buffer_capacity": require_positive_int(raw.get("buffer_capacity", 1), "buffer_capacity"),
                "initial_state": parse_initial_state(raw)}

    def effective_boundary(self, changed: set[str]) -> str:
        return "products accepted after the change; already routed products are not rerouted"

    def on_config_changed(self, old: dict, new: dict) -> None:
        if old["mode"] != new["mode"] or old["percentage_a"] != new["percentage_a"]:
            self.alloc_counts = {"A": 0, "B": 0}
        super().on_config_changed(old, new)

    def contained(self) -> Iterable[tuple[str, str]]:
        for b in BRANCHES:
            for pid in self.queues[b]:
                yield pid, "splitter_queue"

    # ------------------------------------------------------------------ routing
    def _choose_percentage(self) -> tuple[str, dict]:
        share_a = self.config["percentage_a"] / 100.0
        n = self.alloc_counts["A"] + self.alloc_counts["B"] + 1
        deficit_a = n * share_a - self.alloc_counts["A"]
        deficit_b = n * (1.0 - share_a) - self.alloc_counts["B"]
        if abs(deficit_a - deficit_b) < 1e-12:
            branch = "A" if self._last_tie_branch == "B" else "B"
            self._last_tie_branch = branch
        else:
            branch = "A" if deficit_a > deficit_b else "B"
        self.alloc_counts[branch] += 1
        return branch, {"deficit_a": round(deficit_a, 6), "deficit_b": round(deficit_b, 6)}

    def _choose_conditional(self, product: Product) -> tuple[str, dict]:
        missing = []
        for idx, rule in enumerate(self.config["rules"]):
            matched, miss = evaluate(rule, product)
            if miss:
                missing.append(rule["attribute"])
            if matched:
                return rule["branch"], {"rule_index": idx, "rule": dict(rule), "missing_attributes": missing}
        return self.config["fallback_branch"], {"rule_index": None, "fallback": True,
                                                "missing_attributes": missing}

    def can_accept(self, port: str, product: Product) -> tuple[bool, str | None]:
        if not self.is_operational():
            return False, f"splitter {self.state}"
        if sum(len(q) for q in self.queues.values()) >= self.config["buffer_capacity"]:
            return False, "splitter buffer full"
        return True, None

    def accept(self, port: str, product: Product) -> None:
        sim = self.sim
        if self.config["mode"] == "percentage":
            branch, detail = self._choose_percentage()
        else:
            branch, detail = self._choose_conditional(product)
        out_port = BRANCH_PORT[branch]
        self.queues[branch].append(product.id)
        self.routed_totals[branch] += 1
        self.place(product, "splitter_queue", out_port, ProductStatus.QUEUED)
        attrs = {"routing_mode": self.config["mode"], "branch": branch, "output_port": out_port,
                 "config_version": self.config_version, **detail}
        entry = sim.new_history_entry(product, self, start=sim.now, attributes=attrs)
        entry.complete(sim.now)
        sim.emit("product_routed", self, production_order_ids=list(product.production_order_ids),
                 input_product_ids=[product.id], input_quantity=1,
                 config={"mode": self.config["mode"], "config_version": self.config_version},
                 details=attrs)
        sim.request_wake(self.id)

    def wake(self) -> None:
        if not self.is_operational():
            return
        moved = False
        for b in BRANCHES:
            moved |= self.push_queue(BRANCH_PORT[b], self.queues[b])
        if moved:
            self.wake_inputs()
        if any(self.queues.values()):
            blocked = [BRANCH_PORT[b] for b in BRANCHES if self.queues[b]]
            self.set_state(BLOCKED, f"output blocked: {', '.join(blocked)}")
        else:
            self.set_state(IDLE)

    def counts(self) -> dict:
        return {"queued_a": len(self.queues["A"]), "queued_b": len(self.queues["B"])}

    def stats(self) -> dict:
        total = self.routed_totals["A"] + self.routed_totals["B"]
        return {"routed_a": self.routed_totals["A"], "routed_b": self.routed_totals["B"], "routed_total": total,
                "share_a": (self.routed_totals["A"] / total) if total else None}


__all__ = ["Splitter"]
