"""Shared builders for the Phase 1b tests: a demo-like config on the simulated tenant, file source, tmp paths."""

import json

import yaml

from pac_sync import main as m
from pac_sync.simzia import SimulatedZscalerClient
from pac_sync.snow_client import make_source as real_make_source

from .conftest import ROOT

SEED_MESSAGE = "PAC Sync 20260101T000000Z seed sha256:000000000000"
PAC = 'function FindProxyForURL(url, host) { return "DIRECT"; }\n'


def demo_cfg(tmp_path, monkeypatch, history="zia", **zia):
    """config.demo.yaml with a file source and the simulated tenant (strict_commit on): prefix demo-, guard ON,
    bootstrap missing, skip_activate_if_pending ON, EMEA auto / NA stage / APAC auto."""
    monkeypatch.chdir(ROOT)
    cfg = yaml.safe_load((ROOT / "config" / "config.demo.yaml").read_text(encoding="utf-8"))
    cfg["servicenow"].update(source="file", sample_file=str(ROOT / "config" / "servicenow_sample_data.json"))
    cfg["zscaler"].update(simulated={"state_dir": str(tmp_path / "sim"), "strict_commit": True}, **zia)
    cfg["zscaler"]["target"] = "simulated"
    cfg["history"] = ({"mode": "zia", "state_file": str(tmp_path / "pac-state.json"), "push": False}
                      if history == "zia" else
                      {"mode": history, "repo_path": str(tmp_path / "history"), "push": False})
    sources = []

    def capture(scfg):
        src = real_make_source(scfg)
        sources.append(src)
        return src

    monkeypatch.setattr(m, "make_source", capture)
    cfg["_sources"] = sources
    return cfg


def sim(cfg):
    z = cfg["zscaler"]["simulated"]
    return SimulatedZscalerClient(z["state_dir"], bool(z.get("strict_commit")))


def add_rule(cfg, tmp_path, value, regions="reg-emea", number="PACR0009001", sys_id="rnew"):
    path = cfg["servicenow"]["sample_file"]
    data = json.loads(open(path, encoding="utf-8").read())
    data["u_pac_rule"].append({
        "sys_id": sys_id, "number": number, "u_action": "direct", "u_match_type": "host_exact",
        "u_value": value, "u_proxy_profile": "", "u_scope": "regional", "u_regions": regions,
        "u_priority": "700", "u_state": "approved", "u_valid_from": "", "u_valid_to": "",
        "u_deploy_status": "pending", "u_deploy_version": ""})
    out = tmp_path / f"snow-{sys_id}.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    cfg["servicenow"]["sample_file"] = str(out)


def pac_file(client, name):
    """The simulated PAC file summary by name (versions without content)."""
    return next(f for f in client.pac_files_summary() if f["name"] == name)


def statuses(client, name):
    return [(v["pac_version"], v["pac_version_status"]) for v in pac_file(client, name)["versions"]]
