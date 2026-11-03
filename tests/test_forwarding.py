"""Traffic forwarding use case end to end: file source -> engine -> fake ZIA (forwarding_control, proxies)."""

import json

import pytest
import yaml

from pac_sync import main as m
from pac_sync import zia_cli
from pac_sync.zia_objects import UseCaseState

from .helpers import demo_cfg, sim

DIRECT, EGRESS = "demo-partner-direct", "demo-partner-egress"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = demo_cfg(tmp_path, monkeypatch)
    c["regions"] = {"EMEA": {"mode": "paused"}, "NA": {"mode": "paused"}, "APAC": {"mode": "paused"}}
    c["use_cases"] = {"state_file": str(tmp_path / "pac-state-usecases.json"),
                      "forwarding": {"enabled": True, "mode": "auto", "rule_rank": 7}}
    c["guardrails"] = {"max_removed_ratio": 0.2, "min_previous_rules_for_ratio": 10}
    sim(c).set_proxy_gateways("OTHER-GW", "PARTNER-EGRESS-GW")
    return c


def edit_snow(cfg, tmp_path, fn):
    path = cfg["servicenow"]["sample_file"]
    data = json.loads(open(path, encoding="utf-8").read())
    fn(data)
    out = tmp_path / f"snow-{len(list(tmp_path.glob('snow-*.json')))}.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    cfg["servicenow"]["sample_file"] = str(out)


def set_state(cfg, tmp_path, sys_id, state):
    def fn(data):
        next(r for r in data["u_fwd_rule"] if r["sys_id"] == sys_id)["u_state"] = state
    edit_snow(cfg, tmp_path, fn)


def rules(cfg):
    return {r["name"]: r for r in sim(cfg).store.load()["forwarding_rules"].values()}


def snapshot(cfg):
    return sim(cfg).store.path.read_bytes()


def updates(cfg):
    return {sys_id: body for _t, sys_id, body in cfg["_sources"][-1].entry_updates}


def ustate(cfg):
    return UseCaseState(cfg["use_cases"]["state_file"]).get("forwarding")


def test_creates_prefixed_rules_then_is_idempotent(cfg):
    assert m.sync(cfg) == 0
    r = rules(cfg)
    assert sorted(r) == [DIRECT, EGRESS]
    assert r[DIRECT]["type"] == "FORWARDING" and r[DIRECT]["forward_method"] == "DIRECT" and not r[DIRECT]["proxy_gateway"]
    assert r[DIRECT]["dest_addresses"] == ["203.0.113.0/24", "portal.partner-example.com"]
    assert r[DIRECT]["state"] == "ENABLED" and r[DIRECT]["description"].startswith("PAC Sync ")
    assert r[EGRESS]["forward_method"] == "PROXYCHAIN" and r[EGRESS]["proxy_gateway"] == {"id": 2, "name": "PARTNER-EGRESS-GW"}
    assert r[EGRESS]["dest_addresses"] == ["egress.partner-example.net"]
    ups = updates(cfg)
    assert ups["f3001"]["u_deploy_status"] == "deployed" and ups["f3001"]["u_deploy_version"].startswith("partner-direct:v")
    assert ups["f3006"]["u_deploy_status"] == "failed" and ups["f3007"]["u_deploy_status"] == "failed"
    assert "conflicts with FWDR0001001" in ups["f3007"]["u_deploy_message"]
    assert ups["f3004"]["u_state"] == "retired" and ups["f3005"]["u_state"] == "retired"
    assert {d["u_object"]: d["u_use_case"] for d in cfg["_sources"][-1].deployments} == {DIRECT: "forwarding", EGRESS: "forwarding"}
    before = snapshot(cfg)
    assert m.sync(cfg) == 0
    assert snapshot(cfg) == before


def test_new_entry_updates_addresses_and_keeps_order_and_rank(cfg, tmp_path):
    m.sync(cfg)
    before = rules(cfg)[DIRECT]

    def add(data):
        data["u_fwd_rule"].append({"sys_id": "f4001", "number": "FWDR0004001", "u_profile": "fp-direct",
                                   "u_match_type": "host_exact", "u_value": "NEW.partner-example.com", "u_state": "approved",
                                   "u_valid_from": "", "u_valid_to": "", "u_deploy_status": "pending", "u_deploy_version": ""})
    edit_snow(cfg, tmp_path, add)
    assert m.sync(cfg) == 0
    after = rules(cfg)[DIRECT]
    assert after["dest_addresses"] == ["203.0.113.0/24", "new.partner-example.com", "portal.partner-example.com"]
    assert (after["id"], after["order"], after["rank"]) == (before["id"], before["order"], before["rank"])


def test_retire_and_empty_group_deletes_the_rule(cfg, tmp_path):
    m.sync(cfg)
    set_state(cfg, tmp_path, "f3003", "retire_requested")  # the only live entry of partner-egress
    assert m.sync(cfg) == 0
    assert sorted(rules(cfg)) == [DIRECT]
    assert updates(cfg)["f3003"]["u_state"] == "retired"


def test_missing_gateway_fails_only_its_group(cfg):
    sim(cfg).set_proxy_gateways("OTHER-GW")
    assert m.sync(cfg) == 1
    assert sorted(rules(cfg)) == [DIRECT]
    ups = updates(cfg)
    assert ups["f3003"]["u_deploy_status"] == "failed" and "no proxy gateway named 'PARTNER-EGRESS-GW'" in ups["f3003"]["u_deploy_message"]
    assert ups["f3001"]["u_deploy_status"] == "deployed"


def test_unprefixed_and_foreign_objects_are_never_touched(cfg):
    client = sim(cfg)
    client.seed_object("forwarding", {"name": "corp-direct", "dest_addresses": ["x.example.org"], "description": "mine"})
    client.seed_object("forwarding", {"name": DIRECT, "dest_addresses": ["hand.example.com"], "description": "by hand"})
    seeded = rules(cfg)
    for force in (False, True):
        assert m.sync(cfg, force=force) == 1  # partner-direct is foreign: its group fails
        assert rules(cfg)["corp-direct"] == seeded["corp-direct"] and rules(cfg)[DIRECT] == seeded[DIRECT]
    assert EGRESS in rules(cfg)
    assert zia_cli.demo_cleanup(cfg, yes=True, out=lambda s: None) == 0
    assert sorted(rules(cfg)) == ["corp-direct", DIRECT]


def test_portal_edit_pauses_until_resume_and_force_overrides(cfg):
    m.sync(cfg)
    client = sim(cfg)
    rule = rules(cfg)[DIRECT]
    client.zia.forwarding_control.update_rule(rule["id"], dest_addresses=rule["dest_addresses"] + ["sneaky.example.com"])
    client.zia.activate.activate()
    before = snapshot(cfg)
    assert m.sync(cfg) == 1 and snapshot(cfg) == before
    assert ustate(cfg)["paused_reason"].startswith("manual change in ZIA: " + DIRECT)
    assert m.resume_use(cfg, "forwarding") == 0
    assert m.sync(cfg) == 0
    assert rules(cfg)[DIRECT]["dest_addresses"] == rule["dest_addresses"]
    client.zia.forwarding_control.update_rule(rule["id"], forward_method="PROXYCHAIN")
    client.zia.activate.activate()
    assert m.sync(cfg, force=True) == 0
    assert rules(cfg)[DIRECT]["forward_method"] == "DIRECT"


def test_guardrails_block_and_force_passes(cfg, tmp_path):
    cfg["guardrails"]["min_previous_rules_for_ratio"] = 2
    m.sync(cfg)
    set_state(cfg, tmp_path, "f3001", "retire_requested")
    set_state(cfg, tmp_path, "f3002", "retire_requested")
    before = snapshot(cfg)
    assert m.sync(cfg) == 1 and snapshot(cfg) == before
    edit_snow(cfg, tmp_path, lambda d: d.update(u_fwd_rule=[]))
    assert m.sync(cfg) == 1 and snapshot(cfg) == before and "0 entries" in ustate(cfg)["last_error"]
    assert m.sync(cfg, force=True) == 0 and not rules(cfg)


def test_pending_activation_saves_but_does_not_activate(cfg, tmp_path):
    m.sync(cfg)
    client = sim(cfg)
    client.set_foreign_pending()
    activations = client.store.load()["activations"]
    set_state(cfg, tmp_path, "f3002", "retire_requested")
    assert m.sync(cfg) == 1
    assert client.store.load()["activations"] == activations
    assert rules(cfg)[DIRECT]["dest_addresses"] == ["portal.partner-example.com"]
    assert updates(cfg)["f3001"]["u_deploy_status"] == "staged" and "u_state" not in updates(cfg).get("f3002", {})
    client.zia.activate.activate()
    assert m.sync(cfg) == 0
    assert updates(cfg)["f3002"]["u_state"] == "retired"


def test_legacy_auth_fails_config_validation(cfg, tmp_path, monkeypatch):
    cfg["zscaler"]["auth"] = "legacy"
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: pytest.fail("no request may be made"))
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({k: v for k, v in cfg.items() if not k.startswith("_")}), encoding="utf-8")
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "run"]) == 3


def test_dry_run_writes_nothing(cfg, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    sim(cfg).store.path.unlink()  # a dry run must not even create the fake's state
    assert m.sync(cfg, dry_run=True) == 0
    assert not sim(cfg).store.path.exists() and not cfg["_sources"][-1].entry_updates
    assert "[dry-run] forwarding: create demo-partner-direct (2 entries)" in caplog.text


def test_inconsistent_profiles_fail_their_group(cfg, tmp_path):
    def bad(data):
        next(p for p in data["u_fwd_profile"] if p["sys_id"] == "fp-direct")["u_proxy_gateway"] = "PARTNER-EGRESS-GW"
        next(p for p in data["u_fwd_profile"] if p["sys_id"] == "fp-egress")["u_proxy_gateway"] = ""
    edit_snow(cfg, tmp_path, bad)
    assert m.sync(cfg) == 0  # rejected entries do not fail the run; nothing is created
    assert not rules(cfg)
    assert "direct profile must not name a proxy gateway" in updates(cfg)["f3001"]["u_deploy_message"]
    assert "proxychain profile needs a proxy gateway" in updates(cfg)["f3003"]["u_deploy_message"]


def test_group_made_invalid_keeps_its_live_rule_and_fails(cfg, tmp_path):
    m.sync(cfg)
    before = snapshot(cfg)
    base = cfg["servicenow"]["sample_file"]

    def dup(data):
        data["u_fwd_profile"].append({"sys_id": "fp-direct-2", "u_name": "partner-direct", "u_forward_method": "direct",
                                      "u_proxy_gateway": "", "u_active": "true"})
    edit_snow(cfg, tmp_path, dup)
    assert m.sync(cfg) == 1 and snapshot(cfg) == before  # whole fake ZIA byte-identical
    assert updates(cfg)["f3001"]["u_deploy_status"] == "failed"
    # f3007 (partner-egress) wanted portal.partner-example.com, still held by the kept demo-partner-direct
    assert updates(cfg)["f3007"]["u_deploy_status"] == "failed"
    assert "conflicts with demo-partner-direct (left unchanged)" in updates(cfg)["f3007"]["u_deploy_message"]
    assert "left unchanged" in ustate(cfg)["last_error"]

    cfg["servicenow"]["sample_file"] = base

    def bad_gateway(data):
        next(p for p in data["u_fwd_profile"] if p["sys_id"] == "fp-egress")["u_proxy_gateway"] = 'BAD"GW'
    edit_snow(cfg, tmp_path, bad_gateway)
    assert m.sync(cfg) == 1 and snapshot(cfg) == before
    assert rules(cfg)[EGRESS]["proxy_gateway"] == {"id": 2, "name": "PARTNER-EGRESS-GW"}
    assert updates(cfg)["f3003"]["u_deploy_status"] == "failed"


def test_gateway_name_is_stripped(cfg, tmp_path):
    def pad(data):
        next(p for p in data["u_fwd_profile"] if p["sys_id"] == "fp-egress")["u_proxy_gateway"] = "  PARTNER-EGRESS-GW "
    edit_snow(cfg, tmp_path, pad)
    assert m.sync(cfg) == 0
    assert rules(cfg)[EGRESS]["proxy_gateway"]["name"] == "PARTNER-EGRESS-GW"
