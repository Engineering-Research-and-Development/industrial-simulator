"""Amendment §2 – asset types, deployment, instance overrides, template updates and config inheritance."""

import pytest

from conftest import basic_sim
from industrial_helpers import FURNACE_PROFILE, attribute_sim, furnace_type
from simulator.errors import ConflictError, NotFoundError, ValidationError


def deploy_two(sim):
    furnace_type(sim)
    a = sim.industrial.deploy("furnace", "F1", asset_id="f1", config={"recipe_id": "smelt"})
    b = sim.industrial.deploy("furnace", "F2", asset_id="f2", config={"recipe_id": "smelt", "speed_pct": 60})
    return a, b


def test_template_reuse_and_config_inheritance():
    sim = attribute_sim()
    a, b = deploy_two(sim)
    hub = sim.industrial
    assert a.config["speed_pct"] == 80 and b.config["speed_pct"] == 60
    da, db = hub.describe("f1"), hub.describe("f2")
    assert da["type"]["id"] == db["type"]["id"] == "furnace"
    assert {s["id"] for s in da["sensors"]} == {"temp", "pressure"}
    cfg_a = {c["key"]: c for c in da["config"]}
    cfg_b = {c["key"]: c for c in db["config"]}
    assert cfg_a["speed_pct"]["origin"] == "template"
    assert cfg_b["speed_pct"]["origin"] == "override" and cfg_b["speed_pct"]["template_value"] == 80
    assert sorted(hub.type_summary(hub.get_type("furnace"))["instances"]) == ["f1", "f2"]


def test_parameter_and_item_overrides_are_per_instance():
    sim = attribute_sim()
    deploy_two(sim)
    hub = sim.industrial
    hub.set_parameters("f2", {"setpoint": 1200})
    hub.set_item_override("f2", "sensors", "temp", {"noise_std": 3.0})
    d1, d2 = hub.describe("f1"), hub.describe("f2")
    p1 = {p["name"]: p for p in d1["parameters"]}["setpoint"]
    p2 = {p["name"]: p for p in d2["parameters"]}["setpoint"]
    assert p1["value"] == 900 and not p1["overridden"]
    assert p2["value"] == 1200 and p2["overridden"]
    t2 = {s["id"]: s for s in d2["sensors"]}["temp"]
    assert t2["noise_std"] == 3.0 and t2["origin"] == "override"
    assert {s["id"]: s for s in d1["sensors"]}["temp"]["origin"] == "template"
    with pytest.raises(ValidationError):
        hub.set_parameters("f1", {"setpoint": 5000})     # above max
    with pytest.raises(ValidationError):
        hub.set_item_override("f1", "sensors", "temp", {"id": "other"})
    with pytest.raises(NotFoundError):
        hub.set_item_override("f1", "sensors", "nope", {"noise_std": 1})
    hub.reset_override("f2", "parameters", "setpoint")
    hub.reset_override("f2", "sensors", "temp")
    d2 = hub.describe("f2")
    assert {p["name"]: p for p in d2["parameters"]}["setpoint"]["value"] == 900
    assert {s["id"]: s for s in d2["sensors"]}["temp"]["origin"] == "template"


def test_template_update_requires_explicit_sync_and_preserves_overrides():
    sim = attribute_sim()
    deploy_two(sim)
    hub = sim.industrial
    hub.set_item_override("f2", "sensors", "temp", {"noise_std": 3.0})
    prof = {**FURNACE_PROFILE, "sensors": [*FURNACE_PROFILE["sensors"],
                                           {"id": "flow", "unit": "m3/h", "model": {"type": "constant", "value": 4}}]}
    t = hub.update_type("furnace", {"config": {"speed_pct": 90}, "profile": prof, "change_note": "add flow"})
    assert t.version == 2 and t.changelog[-1]["summary"] == "add flow"
    # instances keep their snapshot until synchronised
    assert hub.describe("f1")["type"]["outdated"]
    assert "flow" not in {s["id"] for s in hub.describe("f1")["sensors"]}
    assert sim.assets["f1"].config["speed_pct"] == 80
    preview = hub.sync_preview("f2")
    assert preview["valid"] and preview["config_changes"]["speed_pct"]["result"] == 60   # override wins
    results = hub.sync_type_instances("furnace")
    assert all(r["applied"] for r in results)
    assert sim.assets["f1"].config["speed_pct"] == 90       # inherited the new template value
    assert sim.assets["f2"].config["speed_pct"] == 60       # kept its override
    d2 = hub.describe("f2")
    assert "flow" in {s["id"] for s in d2["sensors"]} and not d2["type"]["outdated"]
    assert {s["id"]: s for s in d2["sensors"]}["temp"]["noise_std"] == 3.0


def test_type_validation_and_lifecycle():
    sim = attribute_sim()
    hub = sim.industrial
    with pytest.raises(ValidationError):
        hub.create_type({"id": "bad", "name": "Bad", "kind": "transformer", "profile": {
            "composites": [{"id": "a", "expression": "composite.b + 1"},
                           {"id": "b", "expression": "composite.a + 1"}]}})
    with pytest.raises(ValidationError):
        hub.create_type({"id": "bad2", "name": "Bad", "kind": "transformer", "profile": {
            "behaviors": [{"id": "x", "output": "not_a_feature", "expression": "1"}]}})
    with pytest.raises(ValidationError):
        hub.create_type({"id": "bad3", "name": "Bad", "kind": "transformer", "profile": {
            "composites": [{"id": "a", "expression": "sensor.unknown * 2"}]}})
    furnace_type(sim)
    with pytest.raises(ConflictError):
        furnace_type(sim)
    copy_ = hub.duplicate_type("furnace", "furnace2")
    assert copy_.version == 1 and copy_.profile == hub.get_type("furnace").profile
    hub.deploy("furnace", "F1", asset_id="f1", config={"recipe_id": "smelt"})
    with pytest.raises(ConflictError):
        hub.delete_type("furnace")
    hub.detach_type("f1")
    d = hub.describe("f1")
    assert d["type"] is None and {s["id"] for s in d["sensors"]} == {"temp", "pressure"}
    assert all(s["origin"] == "local" for s in d["sensors"])
    hub.delete_type("furnace")
    hub.attach_type("f1", "furnace2")
    assert hub.describe("f1")["type"]["id"] == "furnace2"


def test_deploy_rolls_back_on_invalid_profile_and_kind_mismatch():
    sim = basic_sim()
    hub = sim.industrial
    hub.create_type({"id": "src", "name": "Src", "kind": "producer", "config": {"product_type_id": "ore"}})
    with pytest.raises(ValidationError):
        hub.deploy("src", "P", asset_id="p", overrides={"bogus": {}})
    assert "p" not in sim.assets
    hub.deploy("src", "P", asset_id="p")
    sim.add_asset("sink", "S", {}, asset_id="s")
    with pytest.raises(ValidationError):
        hub.attach_type("s", "src")


def test_local_definitions_extend_template():
    sim = attribute_sim()
    furnace_type(sim)
    hub = sim.industrial
    hub.deploy("furnace", "F1", asset_id="f1", config={"recipe_id": "smelt"},
               local={"sensors": [{"id": "vib", "unit": "mm/s", "model": {"type": "constant", "value": 1}}]})
    d = hub.describe("f1")
    assert {s["id"]: s["origin"] for s in d["sensors"]} == {"temp": "template", "pressure": "template",
                                                           "vib": "local"}
