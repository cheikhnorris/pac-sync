"""End-to-end run: sample ServiceNow file -> git history -> fake ZIA."""

import copy
import json
import subprocess

import pytest
import yaml

from pac_sync import main as m

from .conftest import ROOT


class FakeDeployer:
    instances = []

    def __init__(self, client, dry_run=False):
        self.next_version = 2
        self.calls = []
        FakeDeployer.instances.append(self)

    def find_pac_id(self, name):
        return {"emea-proxy.pac": 11, "na-proxy.pac": 12, "apac-proxy.pac": 13}[name]

    def stage(self, pac_id, content, message):
        self.calls.append(("stage", pac_id))
        self.next_version += 1
        return self.next_version

    def deploy(self, pac_id, version):
        self.calls.append(("deploy", pac_id, version))

    def activate(self):
        self.calls.append(("activate",))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(m, "ZiaDeployer", FakeDeployer)
    monkeypatch.setattr(m, "make_zscaler_client", lambda c: None)
    FakeDeployer.instances.clear()
    data = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    data["history"]["repo_path"] = str(tmp_path / "history")
    data["regions"] = {"EMEA": {"mode": "auto"}, "NA": {"mode": "stage"}, "APAC": {"mode": "paused"}}
    return data


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_full_run_modes_and_history(cfg):
    assert m.sync(cfg) == 0
    repo = cfg["history"]["repo_path"]
    calls = FakeDeployer.instances[-1].calls
    assert ("stage", 11) in calls and ("stage", 12) in calls and ("stage", 13) not in calls
    assert ("deploy", 11, 3) in calls  # EMEA auto -> deployed
    assert not any(c[0] == "deploy" and c[1] == 12 for c in calls)  # NA stage only

    emea = json.loads(open(f"{repo}/regions/EMEA/state.json").read())
    na = json.loads(open(f"{repo}/regions/NA/state.json").read())
    assert emea["deployed"]["version"] == 3 and "staged" not in emea
    assert na["staged"]["version"] == 4 and "deployed" not in na
    assert "EMEA-v3" in _git(repo, "tag")
    assert "PAC update" in _git(repo, "log", "--format=%s")

    # Second run with the same data: nothing to stage or deploy, no new content commit.
    commits = _git(repo, "rev-list", "--count", "HEAD")
    assert m.sync(cfg) == 0
    assert FakeDeployer.instances[-1].calls == []
    assert _git(repo, "rev-list", "--count", "HEAD") == commits


def test_promote_deploys_reviewed_stage(cfg):
    m.sync(cfg)
    assert m.sync(cfg, promote="NA") == 0
    calls = FakeDeployer.instances[-1].calls
    assert calls == [("deploy", 12, 4), ("activate",)]


def test_guardrail_blocks_mass_removal(cfg, tmp_path):
    sample = json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))
    many = copy.deepcopy(sample)
    for i in range(20):
        many["u_pac_rule"].append({
            "sys_id": f"bulk{i}", "number": f"PACR00020{i:02d}", "u_action": "direct", "u_match_type": "host_exact",
            "u_value": f"app{i}.example.com", "u_proxy_profile": "", "u_scope": "global", "u_regions": "",
            "u_priority": "700", "u_state": "approved", "u_valid_from": "", "u_valid_to": "",
        })
    path = tmp_path / "snow.json"
    path.write_text(json.dumps(many), encoding="utf-8")
    cfg["servicenow"]["sample_file"] = str(path)
    assert m.sync(cfg) == 0

    path.write_text(json.dumps(sample), encoding="utf-8")  # 20 rules vanish
    assert m.sync(cfg) == 1
    assert not any(c[0] == "stage" for c in FakeDeployer.instances[-1].calls)


def test_dry_run_leaves_no_trace(cfg):
    assert m.sync(cfg, dry_run=True) == 0
    repo = cfg["history"]["repo_path"]
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"  # only the init commit
    assert _git(repo, "status", "--porcelain").strip() == ""
