"""URL filtering use case end to end: file source -> engine -> fake ZIA (url_categories, url_filtering)."""

import json
import logging

import pytest
import yaml

from pac_sync import main as m
from pac_sync import zia_cli
from pac_sync.snow_client import FileServiceNowSource
from pac_sync.zia_objects import UseCaseState

from .fake_zia import SimUrlCategoriesAPI
from .helpers import demo_cfg, sim

CAT = "demo-partner-sites"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = demo_cfg(tmp_path, monkeypatch)
    c["regions"] = {"EMEA": {"mode": "paused"}, "NA": {"mode": "paused"}, "APAC": {"mode": "paused"}}  # PAC stays out
    c["use_cases"] = {"state_file": str(tmp_path / "pac-state-usecases.json"),
                      "url_filtering": {"enabled": True, "mode": "auto", "rule_rank": 7}}
    c["guardrails"] = {"max_removed_ratio": 0.2, "min_previous_rules_for_ratio": 10}
    return c


def edit_snow(cfg, tmp_path, fn):
    path = cfg["servicenow"]["sample_file"]
    data = json.loads(open(path, encoding="utf-8").read())
    fn(data)
    out = tmp_path / f"snow-{len(list(tmp_path.glob('snow-*.json')))}.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    cfg["servicenow"]["sample_file"] = str(out)


def set_state(cfg, tmp_path, sys_id, state, table="u_url_rule"):
    def fn(data):
        next(r for r in data[table] if r["sys_id"] == sys_id)["u_state"] = state
    edit_snow(cfg, tmp_path, fn)


def zia(cfg):
    return sim(cfg).store.load()


def by_name(items):
    return {o["configured_name"] if "configured_name" in o else o["name"]: o for o in items.values()}


def snapshot(cfg):
    return sim(cfg).store.path.read_bytes()


def updates(cfg):
    return {sys_id: body for _t, sys_id, body in cfg["_sources"][-1].entry_updates}


def ustate(cfg):
    return UseCaseState(cfg["use_cases"]["state_file"]).get("url_filtering")


def test_creates_prefixed_category_and_rule_then_is_idempotent(cfg):
    assert m.sync(cfg) == 0
    state = zia(cfg)
    cats, rules = by_name(state["url_categories"]), by_name(state["url_rules"])
    assert sorted(cats) == ["demo-partner-sites", "demo-review-sites", "demo-risky-tools"]
    assert sorted(cats[CAT]["urls"]) == [".partner-example.com", "tools.example.net/upload"]
    assert cats[CAT]["super_category"] == "USER_DEFINED" and cats[CAT]["custom_category"] is True
    assert cats[CAT]["description"].startswith("PAC Sync ") and " partner-sites " in cats[CAT]["description"]
    assert sorted(rules) == sorted(cats)
    assert rules[CAT]["action"] == "ALLOW" and rules["demo-risky-tools"]["action"] == "BLOCK"
    assert rules["demo-review-sites"]["action"] == "CAUTION"
    assert rules[CAT]["url_categories"] == [cats[CAT]["id"]] and rules[CAT]["state"] == "ENABLED"
    assert state["unactivated"] is False  # activated

    before = snapshot(cfg)
    assert m.sync(cfg) == 0
    assert snapshot(cfg) == before  # zero writes on the second run


def test_new_entry_updates_category_urls_only(cfg, tmp_path):
    m.sync(cfg)
    before = zia(cfg)
    rule_before = by_name(before["url_rules"])[CAT]

    def add(data):
        data["u_url_rule"].append({"sys_id": "u3001", "number": "URLR0003001", "u_category": "cat-partner",
                                   "u_value": "NEW.partner-example.com ", "u_state": "approved", "u_valid_from": "",
                                   "u_valid_to": "", "u_deploy_status": "pending", "u_deploy_version": ""})
    edit_snow(cfg, tmp_path, add)
    assert m.sync(cfg) == 0
    after = zia(cfg)
    assert sorted(by_name(after["url_categories"])[CAT]["urls"]) == [
        ".partner-example.com", "new.partner-example.com", "tools.example.net/upload"]
    rule = by_name(after["url_rules"])[CAT]
    assert (rule["id"], rule["order"], rule["rank"], rule["action"], rule["url_categories"]) == (
        rule_before["id"], rule_before["order"], rule_before["rank"], rule_before["action"], rule_before["url_categories"])
    assert by_name(after["url_categories"])["demo-risky-tools"] == by_name(before["url_categories"])["demo-risky-tools"]


def test_retire_removes_entry_and_retires_after_activation(cfg, tmp_path):
    m.sync(cfg)
    set_state(cfg, tmp_path, "u2002", "retire_requested")
    assert m.sync(cfg) == 0
    assert by_name(zia(cfg)["url_categories"])[CAT]["urls"] == [".partner-example.com"]
    assert updates(cfg)["u2002"]["u_state"] == "retired" and updates(cfg)["u2002"]["u_deploy_status"] == "removed"


def test_empty_group_deletes_rule_before_category(cfg, tmp_path):
    m.sync(cfg)
    set_state(cfg, tmp_path, "u2004", "retire_requested")  # the only live entry of review-sites
    assert m.sync(cfg) == 0  # the fake refuses to delete a category a rule still references
    state = zia(cfg)
    assert "demo-review-sites" not in by_name(state["url_categories"]) and "demo-review-sites" not in by_name(state["url_rules"])
    assert updates(cfg)["u2004"]["u_state"] == "retired"


def test_unprefixed_objects_are_never_touched(cfg, tmp_path):
    client = sim(cfg)
    client.seed_object("category", {"configured_name": "corp-block", "urls": ["bad.example.org"], "description": "mine"})
    client.seed_object("rule", {"name": "corp-rule", "action": "BLOCK", "url_categories": ["CUSTOM_90"]})
    mine = lambda: {k: v for k, v in json.loads(snapshot(cfg)).items() if k in ("url_categories", "url_rules")}  # noqa: E731
    keep = lambda s: ({k: v for k, v in s["url_categories"].items() if not v["configured_name"].startswith("demo-")},  # noqa: E731
                      {k: v for k, v in s["url_rules"].items() if not v["name"].startswith("demo-")})
    first = keep(mine())
    m.sync(cfg)  # create
    set_state(cfg, tmp_path, "u2001", "retire_requested")
    m.sync(cfg)  # update
    set_state(cfg, tmp_path, "u2004", "retire_requested")
    m.sync(cfg)  # delete
    assert keep(mine()) == first
    assert zia_cli.demo_cleanup(cfg, yes=True, out=lambda s: None) == 0
    assert keep(mine()) == first and all(not v["configured_name"].startswith("demo-") for v in mine()["url_categories"].values())


def test_prefixed_foreign_object_fails_group_and_is_not_modified(cfg):
    client = sim(cfg)
    client.seed_object("category", {"configured_name": CAT, "urls": ["hand.example.com"], "description": "made by hand"})
    foreign = by_name(client.store.load()["url_categories"])[CAT]
    for force in (False, True):
        assert m.sync(cfg, force=force) == 1
        assert by_name(zia(cfg)["url_categories"])[CAT] == foreign  # never modified, even with --force
    names = by_name(zia(cfg)["url_categories"])
    assert "demo-risky-tools" in names and "demo-review-sites" in names  # the other groups went through
    assert "foreign" not in ustate(cfg).get("paused_reason", "")
    assert updates(cfg)["u2001"]["u_deploy_status"] == "failed" and "not created by PAC Sync" in updates(cfg)["u2001"]["u_deploy_message"]


def _portal_edit(cfg):
    client = sim(cfg)
    cat = by_name(client.store.load()["url_categories"])[CAT]
    client.zia.url_categories.update_url_category(cat["id"], urls=cat["urls"] + ["sneaky.example.com"])
    client.zia.activate.activate()
    return cat


def test_portal_edit_pauses_use_case_until_resume(cfg):
    m.sync(cfg)
    cat = _portal_edit(cfg)
    before = snapshot(cfg)
    assert m.sync(cfg) == 1
    assert snapshot(cfg) == before  # nothing written
    assert ustate(cfg)["paused_reason"].startswith("manual change in ZIA: " + CAT)
    assert m.sync(cfg) == 1 and snapshot(cfg) == before  # still paused
    assert m.resume_use(cfg, "url_filtering") == 0
    assert "paused_reason" not in ustate(cfg) and ustate(cfg)["drift_ack"] is True
    assert m.sync(cfg) == 0
    assert by_name(zia(cfg)["url_categories"])[CAT]["urls"] == cat["urls"]  # desired state restored
    assert "drift_ack" not in ustate(cfg)


def test_force_overrides_drift(cfg):
    m.sync(cfg)
    cat = _portal_edit(cfg)
    assert m.sync(cfg, force=True) == 0
    assert by_name(zia(cfg)["url_categories"])[CAT]["urls"] == cat["urls"]
    assert "paused_reason" not in ustate(cfg)


def test_pending_activation_saves_not_activates_then_confirms(cfg, tmp_path):
    m.sync(cfg)
    client = sim(cfg)
    client.set_foreign_pending()
    activations = client.store.load()["activations"]
    set_state(cfg, tmp_path, "u2002", "retire_requested")
    assert m.sync(cfg) == 1
    assert client.store.load()["activations"] == activations  # activate() never called
    assert by_name(zia(cfg)["url_categories"])[CAT]["urls"] == [".partner-example.com"]  # saved
    assert ustate(cfg)["not_activated"] == ["partner-sites"]
    ups = updates(cfg)
    assert ups["u2001"]["u_deploy_status"] == "staged" and "not activated" in ups["u2001"]["u_deploy_message"]
    assert "u_state" not in ups.get("u2002", {})  # the retirement waits for the activation
    client.zia.activate.activate()
    assert m.sync(cfg) == 0
    assert "not_activated" not in ustate(cfg) or ustate(cfg)["not_activated"] == []
    assert updates(cfg)["u2001"]["u_deploy_status"] == "deployed" and updates(cfg)["u2002"]["u_state"] == "retired"
    assert any("activation confirmed" in d["u_summary"] and d["u_object"] == CAT for d in cfg["_sources"][-1].deployments)


def test_removed_ratio_guardrail_blocks_and_force_passes(cfg, tmp_path):
    cfg["guardrails"]["min_previous_rules_for_ratio"] = 2
    m.sync(cfg)
    set_state(cfg, tmp_path, "u2001", "retire_requested")
    set_state(cfg, tmp_path, "u2002", "retire_requested")
    before = snapshot(cfg)
    assert m.sync(cfg) == 1
    assert snapshot(cfg) == before and "would be removed" in ustate(cfg)["last_error"]
    assert m.sync(cfg, force=True) == 0
    assert CAT not in by_name(zia(cfg)["url_categories"])


def test_zero_records_with_managed_objects_blocks(cfg, tmp_path):
    m.sync(cfg)
    edit_snow(cfg, tmp_path, lambda d: d.update(u_url_rule=[]))
    before = snapshot(cfg)
    assert m.sync(cfg) == 1
    assert snapshot(cfg) == before and "0 entries" in ustate(cfg)["last_error"]
    assert m.sync(cfg, force=True) == 0
    assert not zia(cfg)["url_categories"] and not zia(cfg)["url_rules"]


def test_dry_run_writes_nothing(cfg, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    assert m.sync(cfg, dry_run=True) == 0
    assert not sim(cfg).store.path.exists()
    assert not (tmp_path / "pac-state-usecases.json").exists()
    assert "[dry-run] url_filtering: create demo-partner-sites (2 entries)" in caplog.text
    src = cfg["_sources"][-1]
    assert not src.entry_updates and not src.deployments


def test_disabled_by_default_makes_no_url_calls(cfg, monkeypatch):
    calls = []
    for name in ("fetch_groups", "fetch_entries"):
        monkeypatch.setattr(FileServiceNowSource, name, lambda *a, **k: calls.append("snow"))
    monkeypatch.setattr(SimUrlCategoriesAPI, "list_categories", lambda *a, **k: calls.append("zia"))
    for variant in (None, {"url_filtering": {"enabled": False}}):
        if variant is None:
            cfg.pop("use_cases")
        else:
            cfg["use_cases"] = variant
        assert m.sync(cfg) == 0
        assert m.sync(cfg, dry_run=True) == 0
    assert calls == []


def test_requires_only_prefixed(cfg, tmp_path, monkeypatch, caplog):
    cfg["zscaler"]["only_prefixed"] = False
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: pytest.fail("no request may be made"))
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({k: v for k, v in cfg.items() if not k.startswith("_")}), encoding="utf-8")
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "run"]) == 3
    assert "use_cases.url_filtering.enabled needs zscaler.only_prefixed: true" in caplog.text


def test_live_max_mode_stage_pauses_use_case(cfg):
    cfg["deploy"]["live_max_mode"] = "stage"
    assert m.sync(cfg) == 0
    assert not zia(cfg)["url_categories"] and not cfg["_sources"][-1].entry_updates


def test_writeback_bodies_and_deployment_rows(cfg):
    assert m.sync(cfg) == 0
    ups, src = updates(cfg), cfg["_sources"][-1]
    cats = by_name(zia(cfg)["url_categories"])
    version = cats[CAT]["description"].split("sha256:")[1][:12]
    first = ups["u2001"]
    assert first["u_deploy_status"] == "deployed" and first["u_state"] == "deployed" and first["u_deploy_message"] == ""
    assert first["u_deploy_version"] == f"partner-sites:v{version}" and first["u_deployed_on"]
    assert ups["u2003"]["u_deploy_version"].startswith("risky-tools:v")
    assert ups["u2008"]["u_deploy_status"] == "failed" and "conflicts with URLR0001001" in ups["u2008"]["u_deploy_message"]
    assert ups["u2007"]["u_deploy_status"] == "failed"
    assert ups["u2005"] == {"u_deploy_status": "removed", "u_state": "retired", "u_deployed_on": first["u_deployed_on"]}
    assert ups["u2006"]["u_state"] == "retired"
    assert {t for t, _s, _b in src.entry_updates} == {"url_rule"}
    rows = {d["u_object"]: d for d in src.deployments}
    assert sorted(rows) == ["demo-partner-sites", "demo-review-sites", "demo-risky-tools"]
    row = rows[CAT]
    assert row["u_use_case"] == "url_filtering" and row["u_region"] == "" and row["u_status"] == "deployed"
    assert row["u_rules_added"] == 2 and row["u_rules_removed"] == 0 and row["u_content_hash"]
    assert "URLR0001001" in row["u_summary"]
    assert set(row) == {"u_use_case", "u_object", "u_region", "u_status", "u_git_commit", "u_content_hash", "u_rules_added",
                        "u_rules_removed", "u_summary", "u_started", "u_finished"}


def test_one_group_failing_does_not_stop_others(cfg, monkeypatch):
    real = SimUrlCategoriesAPI.add_url_category

    def flaky(self, super_category, urls=None, configured_name=None, **kw):
        if configured_name == "demo-risky-tools":
            return None, None, "400 limit reached"
        return real(self, super_category, urls, configured_name, **kw)

    monkeypatch.setattr(SimUrlCategoriesAPI, "add_url_category", flaky)
    assert m.sync(cfg) == 1
    assert sorted(by_name(zia(cfg)["url_categories"])) == ["demo-partner-sites", "demo-review-sites"]
    assert updates(cfg)["u2003"]["u_deploy_status"] == "failed" and "limit reached" in updates(cfg)["u2003"]["u_deploy_message"]
    assert updates(cfg)["u2001"]["u_deploy_status"] == "deployed"
    rows = {d["u_object"]: d["u_status"] for d in cfg["_sources"][-1].deployments}
    assert rows["demo-risky-tools"] == "failed" and rows[CAT] == "deployed"


def test_demo_cleanup_deletes_only_managed(cfg):
    m.sync(cfg)
    client = sim(cfg)
    client.seed_object("category", {"configured_name": "corp-block", "urls": ["x.example.org"], "description": "mine"})
    client.seed_object("category", {"configured_name": "demo-by-hand", "urls": ["y.example.org"], "description": "hand"})
    lines = []
    assert zia_cli.demo_cleanup(cfg, yes=True, out=lines.append) == 0
    left = sorted(by_name(zia(cfg)["url_categories"]))
    assert left == ["corp-block", "demo-by-hand"] and not zia(cfg)["url_rules"]
    assert any("demo-partner-sites" in line for line in lines)


def test_duplicate_group_names_reject_their_entries(cfg, tmp_path):
    def dup(data):
        data["u_url_category"].append({"sys_id": "cat-partner-2", "u_name": "partner-sites", "u_action": "block",
                                       "u_active": "true"})
    edit_snow(cfg, tmp_path, dup)
    assert m.sync(cfg) == 0
    names = by_name(zia(cfg)["url_categories"])
    assert CAT not in names and "demo-risky-tools" in names  # nothing is merged or created for the ambiguous name
    assert "more than one active group" in updates(cfg)["u2001"]["u_deploy_message"]
    assert updates(cfg)["u2001"]["u_deploy_status"] == "failed"


def test_group_made_invalid_keeps_its_live_objects_and_fails(cfg, tmp_path):
    m.sync(cfg)
    before = snapshot(cfg)

    def dup(data):
        data["u_url_category"].append({"sys_id": "cat-partner-2", "u_name": "partner-sites", "u_action": "block",
                                       "u_active": "true"})
    edit_snow(cfg, tmp_path, dup)
    assert m.sync(cfg) == 1
    assert snapshot(cfg) == before  # the whole fake ZIA is byte-identical: nothing deleted, rewritten or gained
    assert updates(cfg)["u2001"]["u_deploy_status"] == "failed"
    # u2008 (risky-tools) wanted the value the kept demo-partner-sites still holds: it must not be deployed
    assert updates(cfg)["u2008"]["u_deploy_status"] == "failed"
    assert "conflicts with demo-partner-sites (left unchanged)" in updates(cfg)["u2008"]["u_deploy_message"]
    assert "left unchanged" in ustate(cfg)["last_error"]
    # a deactivated group (no active group of that name) is still deleted
    cfg["servicenow"]["sample_file"] = str(tmp_path / "snow-0.json")
    edit_snow(cfg, tmp_path, lambda d: [c.update(u_active="false") for c in d["u_url_category"] if c["sys_id"] == "cat-partner"])
    assert m.sync(cfg) == 0
    assert CAT not in by_name(zia(cfg)["url_categories"])
