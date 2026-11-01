"""history.mode: zia. No git at all: the ZIA versions are the history, history.state_file holds the rest."""

import copy
import json

import pytest

from pac_sync import main as m
from pac_sync.zia_history import normalized_hash, parse_rules

from .conftest import ROOT
from .helpers import add_rule, demo_cfg, sim, statuses

NAMES = ("demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")


def _ready(tmp_path, monkeypatch):
    """Demo config on the simulator with all three PAC files in place (NA is stage mode: explicit bootstrap)."""
    cfg = demo_cfg(tmp_path, monkeypatch)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    return cfg, sim(cfg)


def _file(client, name):
    return next(f for f in client.pac_files_summary() if f["name"] == name)


def _state_file(tmp_path):
    path = tmp_path / "pac-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _deployed_content(tmp_path, name):
    return (tmp_path / "sim" / name / "deployed.pac").read_text(encoding="utf-8")


def test_full_loop_without_any_git_repository(tmp_path, monkeypatch):
    cfg, client = _ready(tmp_path, monkeypatch)
    assert not list(tmp_path.rglob(".git"))  # no repository was created anywhere
    assert sorted(f["name"] for f in client.pac_files_summary()) == sorted(NAMES)
    emea = _deployed_content(tmp_path, "demo-emea-proxy.pac")
    assert "PACR0001002 DIRECT" in emea
    # every version carries a PAC Sync message with the sha256 of what it holds
    msg = _file(client, "demo-emea-proxy.pac")["versions"][0]["pac_commit_message"]
    assert msg.startswith("PAC Sync ") and "bootstrap" in msg and "sha256:" in msg and "git:" not in msg
    assert msg.split("sha256:")[1][:12] == normalized_hash(emea)[:12]
    row = [d for d in cfg["_sources"][-1].deployments if d["u_zia_version"]][0]
    assert row["u_git_commit"] == ""  # no git in this mode
    assert row["u_summary"].startswith("PAC Sync ")
    updates = dict(cfg["_sources"][-1].rule_updates)
    assert updates["r1002"]["u_deploy_status"] == "deployed"

    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "LKG"), (2, "DEPLOYED")]
    assert "+PACR0009001" in _file(client, "demo-emea-proxy.pac")["versions"][1]["pac_commit_message"]
    assert not list(tmp_path.rglob(".git"))


def test_second_run_changes_nothing(tmp_path, monkeypatch):
    cfg, client = _ready(tmp_path, monkeypatch)
    before = copy.deepcopy(client.pac_files_summary())
    assert m.sync(cfg) == 0
    assert client.pac_files_summary() == before  # state is derived from ZIA: same hashes, no new version
    assert _state_file(tmp_path) == {}


def test_dry_run_prints_a_diff_and_writes_nothing(tmp_path, monkeypatch, caplog):
    cfg, client = _ready(tmp_path, monkeypatch)
    caplog.set_level("INFO")
    add_rule(cfg, tmp_path, "new-app.example.com")
    before = copy.deepcopy(client.pac_files_summary())
    assert m.sync(cfg, dry_run=True) == 0
    assert client.pac_files_summary() == before
    assert "EMEA render" in caplog.text and '+    if (h === "new-app.example.com")' in caplog.text


def test_guardrail_uses_the_pacr_comments_of_the_deployed_pac(tmp_path, monkeypatch):
    sample = json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))
    many = copy.deepcopy(sample)
    for i in range(20):
        many["u_pac_rule"].append({
            "sys_id": f"bulk{i}", "number": f"PACR00020{i:02d}", "u_action": "direct", "u_match_type": "host_exact",
            "u_value": f"app{i}.example.com", "u_proxy_profile": "", "u_scope": "global", "u_regions": "",
            "u_priority": "700", "u_state": "approved", "u_valid_from": "", "u_valid_to": ""})
    big = tmp_path / "big.json"
    big.write_text(json.dumps(many), encoding="utf-8")
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["servicenow"]["sample_file"] = str(big)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    deployed = _deployed_content(tmp_path, "demo-emea-proxy.pac")
    assert len(parse_rules(deployed)) >= 20  # what the guardrail will compare against

    client = sim(cfg)
    before = statuses(client, "demo-emea-proxy.pac")
    cfg["servicenow"]["sample_file"] = str(ROOT / "config" / "servicenow_sample_data.json")  # 20 rules vanish
    assert m.sync(cfg) == 1
    assert statuses(client, "demo-emea-proxy.pac") == before  # blocked: nothing new in ZIA
    assert m.sync(cfg, force=True) == 0  # --force overrides the guardrail
    assert statuses(client, "demo-emea-proxy.pac")[-1][0] == before[-1][0] + 1


def test_rollback_and_resume_persist_in_the_state_file(tmp_path, monkeypatch):
    cfg, client = _ready(tmp_path, monkeypatch)
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0  # EMEA v2
    assert m.rollback(cfg, "EMEA", version=1) == 0
    assert statuses(client, "demo-emea-proxy.pac")[-1] == (2, "LKG") and (1, "DEPLOYED") in statuses(client, "demo-emea-proxy.pac")
    saved = _state_file(tmp_path)["EMEA"]
    assert "rollback to v1" in saved["paused_reason"] and saved["drift_ack"] is True

    assert m.sync(cfg) == 0  # paused: the bad render is not deployed again
    assert (1, "DEPLOYED") in statuses(client, "demo-emea-proxy.pac")
    assert m.status(cfg, out=lambda s: None) == 0

    assert m.resume(cfg, "EMEA") == 0
    assert "paused_reason" not in _state_file(tmp_path).get("EMEA", {})
    assert m.sync(cfg) == 0
    assert statuses(client, "demo-emea-proxy.pac")[-1] == (3, "DEPLOYED")


def test_a_manual_deploy_pauses_the_region_until_resume(tmp_path, monkeypatch):
    cfg, client = _ready(tmp_path, monkeypatch)
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    new = api.clone_pac_file(pac_id, "1", 'function FindProxyForURL(u, h) { return "DIRECT"; } // hand edit', "STAGE",
                             pac_commit_message="quick fix from the portal")[0]
    api.update_pac_file(pac_id, new.pac_version, "DEPLOY")
    client.zia.activate.activate()
    versions = len(statuses(client, "demo-emea-proxy.pac"))

    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 1
    assert len(statuses(client, "demo-emea-proxy.pac")) == versions  # nothing written over the manual change
    assert "manual change in ZIA" in _state_file(tmp_path)["EMEA"]["paused_reason"]
    m.sync(cfg)  # still paused on later runs: silently skipped
    assert len(statuses(client, "demo-emea-proxy.pac")) == versions

    assert m.resume(cfg, "EMEA") == 0  # accept the manual version, then PAC Sync carries on from it
    assert m.sync(cfg) == 0
    assert statuses(client, "demo-emea-proxy.pac")[-1][1] == "DEPLOYED"
    assert len(statuses(client, "demo-emea-proxy.pac")) == versions + 1
    assert m.sync(cfg) == 0
    assert len(statuses(client, "demo-emea-proxy.pac")) == versions + 1  # accepted once, not flagged again


def test_force_overrides_the_manual_change_pause(tmp_path, monkeypatch):
    cfg, client = _ready(tmp_path, monkeypatch)
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    new = api.clone_pac_file(pac_id, "1", 'function FindProxyForURL(u, h) { return "DIRECT"; }', "STAGE",
                             pac_commit_message="portal edit")[0]
    api.update_pac_file(pac_id, new.pac_version, "DEPLOY")
    client.zia.activate.activate()
    assert m.sync(cfg) == 1  # drift is detected even though no rule changed
    assert "manual change in ZIA" in _state_file(tmp_path)["EMEA"]["paused_reason"]
    assert m.sync(cfg, force=True) == 0
    assert "PACR0001002" in _deployed_content(tmp_path, "demo-emea-proxy.pac")  # PAC Sync's render is back
    assert "paused_reason" not in _state_file(tmp_path).get("EMEA", {})


@pytest.mark.parametrize("mode", ["git", "both"])
def test_git_modes_pause_on_a_version_state_json_does_not_know(tmp_path, monkeypatch, mode):
    cfg = demo_cfg(tmp_path, monkeypatch, history=mode)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    client = sim(cfg)
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    # a version that LOOKS like PAC Sync's (copied message) but state.json never recorded
    new = api.clone_pac_file(pac_id, "1", 'function FindProxyForURL(u, h) { return "DIRECT"; }', "STAGE",
                             pac_commit_message="PAC Sync 20260101T000000Z EMEA sha256:000000000000")[0]
    api.update_pac_file(pac_id, new.pac_version, "DEPLOY")
    client.zia.activate.activate()
    before = len(statuses(client, "demo-emea-proxy.pac"))
    assert m.sync(cfg) == 1
    assert len(statuses(client, "demo-emea-proxy.pac")) == before
    state = json.loads((tmp_path / "history" / "regions" / "EMEA" / "state.json").read_text(encoding="utf-8"))
    assert "manual change in ZIA" in state["paused_reason"]
