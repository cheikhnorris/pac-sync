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

    def __init__(self, client):
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


# --- Phase 1: fake ZIA, bootstrap, live cap, push failure, foreign files -----------------------------------

from pac_sync.deployer import ZiaDeployer  # noqa: E402
from pac_sync.history import GitHistory  # noqa: E402
from .fake_zia import SimulatedZscalerClient, install_fake_zia  # noqa: E402
from pac_sync.snow_client import make_source as real_make_source  # noqa: E402


@pytest.fixture
def sim_cfg(tmp_path, monkeypatch):
    """Example config, file source, fake ZIA in tmp; the real ZiaDeployer and client factory."""
    monkeypatch.chdir(ROOT)
    data = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    data["history"]["repo_path"] = str(tmp_path / "history")
    data["zscaler"].update(pac_domain="example.com")
    data["_sim"] = str(tmp_path / "sim")
    install_fake_zia(monkeypatch, tmp_path / "sim")
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
    data["regions"] = {"EMEA": {"mode": "auto"}, "NA": {"mode": "stage"}, "APAC": {"mode": "paused"}}
    sources = []

    def capture(cfg):
        src = real_make_source(cfg)
        sources.append(src)
        return src

    monkeypatch.setattr(m, "make_source", capture)
    data["_sources"] = sources
    return data


SEED_MESSAGE = "PAC Sync 20260101T000000Z seed sha256:000000000000"


def _sim(cfg):
    return SimulatedZscalerClient(cfg["_sim"])


def _read(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def _state(repo, code):
    return json.loads(_read(f"{repo}/regions/{code}/state.json"))


def _add_rule(cfg, tmp_path, value, regions="reg-emea"):
    sample = json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))
    sample["u_pac_rule"].append({
        "sys_id": "rnew", "number": "PACR0009001", "u_action": "direct", "u_match_type": "host_exact",
        "u_value": value, "u_proxy_profile": "", "u_scope": "regional", "u_regions": regions,
        "u_priority": "700", "u_state": "approved", "u_valid_from": "", "u_valid_to": "",
        "u_deploy_status": "pending", "u_deploy_version": ""})
    path = tmp_path / "snow-changed.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    cfg["servicenow"]["sample_file"] = str(path)


def test_simulated_full_loop(sim_cfg, tmp_path):
    seed = ZiaDeployer(_sim(sim_cfg))
    for name in ("emea-proxy.pac", "na-proxy.pac"):
        seed.bootstrap(name, 'function FindProxyForURL(url, host) { return "DIRECT"; }\n', "example.com", SEED_MESSAGE)
    assert m.sync(sim_cfg) == 0

    repo = sim_cfg["history"]["repo_path"]
    sim_dir = tmp_path / "sim"
    emea, na = _state(repo, "EMEA"), _state(repo, "NA")
    assert emea["deployed"]["version"] == 2 and "staged" not in emea
    assert na["staged"]["version"] == 2 and "deployed" not in na
    assert (sim_dir / "emea-proxy.pac" / "deployed.pac").read_bytes() == _read(f"{repo}/regions/EMEA/proxy.pac", "rb")
    assert "EMEA-v2" in _git(repo, "tag")
    updates = dict(sim_cfg["_sources"][-1].rule_updates)
    assert updates["r1002"]["u_state"] == "deployed" and updates["r1002"]["u_deploy_status"] == "deployed"
    assert updates["r1043"]["u_deploy_status"] == "staged"

    # Promote NA through the same simulated tenant.
    assert m.sync(sim_cfg, promote="NA") == 0
    assert _state(repo, "NA")["deployed"]["version"] == 2
    assert (sim_dir / "na-proxy.pac" / "deployed.pac").read_bytes() == _read(f"{repo}/regions/NA/proxy.pac", "rb")


def test_bootstrap_missing_creates_pac(sim_cfg, tmp_path):
    sim_cfg["zscaler"]["bootstrap"] = "off"
    assert m.sync(sim_cfg) == 1  # EMEA and NA have no PAC file yet
    repo = sim_cfg["history"]["repo_path"]
    assert "no ZIA PAC file named 'emea-proxy.pac'" in _state(repo, "EMEA")["last_error"]

    sim_cfg["zscaler"]["bootstrap"] = "missing"
    assert m.sync(sim_cfg) == 1  # EMEA (auto) is created; NA (stage) is refused: its PAC would go live as DEPLOYED
    st = _state(repo, "EMEA")
    assert st["deployed"]["version"] == 1 and st["pac_id"] and st["last_error"] is None
    assert (tmp_path / "sim" / "emea-proxy.pac" / "deployed.pac").read_bytes() == _read(f"{repo}/regions/EMEA/proxy.pac", "rb")
    assert "pac-sync bootstrap --region NA" in _state(repo, "NA")["last_error"]
    assert not (tmp_path / "sim" / "na-proxy.pac").exists() and not (tmp_path / "sim" / "apac-proxy.pac").exists()

    assert m.bootstrap_region(sim_cfg, "NA", yes=True) == 0  # the explicit command creates it
    assert m.sync(sim_cfg) == 0
    assert _state(repo, "NA")["deployed"]["version"] == 1
    assert {"EMEA-v1", "NA-v1"} <= set(_git(repo, "tag").split())

    # Nothing left to do on the next run.
    commits = _git(repo, "rev-list", "--count", "HEAD")
    assert m.sync(sim_cfg) == 0
    assert _git(repo, "rev-list", "--count", "HEAD") == commits


def test_live_max_mode_caps_auto(cfg):
    cfg["deploy"]["live_max_mode"] = "stage"
    assert m.region_mode(cfg, "EMEA") == "stage"
    assert m.region_mode(cfg, "EMEA") == "stage"
    assert m.sync(cfg) == 0
    calls = FakeDeployer.instances[-1].calls
    assert ("stage", 11) in calls and not any(c[0] == "deploy" for c in calls)


def test_push_failure_still_writes_back(sim_cfg, monkeypatch):
    sim_cfg["zscaler"]["bootstrap"] = "missing"
    sim_cfg["regions"]["NA"]["mode"] = "auto"
    sim_cfg["history"]["push"] = True
    messages = []
    monkeypatch.setattr(m, "notify", lambda cfg, text: messages.append(text))

    def boom(self):
        raise RuntimeError("git push origin HEAD failed: fatal: unable to access the remote")

    monkeypatch.setattr(GitHistory, "push", boom)
    assert m.sync(sim_cfg) == 1
    assert sim_cfg["_sources"][-1].rule_updates  # write-back happened anyway
    assert sim_cfg["_sources"][-1].deployments
    assert any("Git push failed" in text for text in messages)


def test_dry_run_keeps_foreign_files(sim_cfg, tmp_path):
    sim_cfg["zscaler"]["bootstrap"] = "missing"
    sim_cfg["regions"]["NA"]["mode"] = "auto"
    assert m.sync(sim_cfg) == 0
    repo = sim_cfg["history"]["repo_path"]
    head = _git(repo, "rev-parse", "HEAD")
    foreign = tmp_path / "history" / "backups" / "x.txt"
    foreign.parent.mkdir()
    foreign.write_text("snow-mock backup\n", encoding="utf-8")

    _add_rule(sim_cfg, tmp_path, "new-app.example.com")
    assert m.sync(sim_cfg, dry_run=True) == 0
    assert foreign.read_text(encoding="utf-8") == "snow-mock backup\n"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain").strip() == "?? backups/"

    assert m.sync(sim_cfg) == 0  # a real run commits regions/ only
    assert "new-app.example.com" in _read(f"{repo}/regions/EMEA/proxy.pac")
    assert "backups" not in _git(repo, "log", "--name-only", "--format=")
    assert _git(repo, "status", "--porcelain").strip() == "?? backups/"


def test_bootstrap_command_live_needs_confirmation(cfg, monkeypatch):
    cfg["zscaler"].update(target="live", pac_domain="example.com")
    built = []
    monkeypatch.setattr(m, "make_zscaler_client", lambda c: built.append(c))
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: False, raising=False)
    assert m.bootstrap_region(cfg, "EMEA", yes=False, confirm=lambda prompt: "EMEA") == 2  # no TTY: refused
    monkeypatch.setattr(m.sys.stdin, "isatty", lambda: True, raising=False)
    assert m.bootstrap_region(cfg, "EMEA", yes=False, confirm=lambda prompt: "no") == 2  # wrong answer: refused
    assert built == [] and not FakeDeployer.instances


def test_bootstrap_command_on_sim_records_state(sim_cfg, tmp_path, capsys):
    assert m.bootstrap_region(sim_cfg, "EMEA", yes=True) == 0
    repo = sim_cfg["history"]["repo_path"]
    st = _state(repo, "EMEA")
    assert st["deployed"]["version"] == 1 and st["pac_id"] == 1
    assert (tmp_path / "sim" / "emea-proxy.pac" / "deployed.pac").read_bytes() == _read(f"{repo}/regions/EMEA/proxy.pac", "rb")
    assert "EMEA-v1" in _git(repo, "tag")
    assert m.bootstrap_region(sim_cfg, "EMEA", yes=True) == 1  # already exists: refused
    # The next run sees EMEA deployed with the current render: no new version.
    sim_cfg["regions"]["NA"] = {"mode": "paused"}
    assert m.sync(sim_cfg) == 0
    assert _state(repo, "EMEA")["deployed"]["version"] == 1

    assert m.status(sim_cfg) == 0
    out = capsys.readouterr().out
    assert "EMEA: mode auto, pac_id 1" in out and "deployed: v1" in out


def test_main_env_file_loaded_and_values_never_logged(sim_cfg, tmp_path, monkeypatch, capsys, caplog):
    import logging
    import os

    secret = "Run-Secret-789"
    sim_cfg["zscaler"]["bootstrap"] = "missing"
    sim_cfg["regions"]["NA"]["mode"] = "auto"
    sim_cfg.pop("_sources")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(sim_cfg), encoding="utf-8")
    env_file = tmp_path / "creds.env"
    env_file.write_text(f"PS_RUN_SECRET={secret}\nPAC_SYNC_WEBHOOK_URL=\n", encoding="utf-8")
    monkeypatch.delenv("PS_RUN_SECRET", raising=False)
    with caplog.at_level(logging.DEBUG):
        assert m.main(["--config", str(path), "--env-file", str(env_file), "-v", "run"]) == 0
    assert os.environ["PS_RUN_SECRET"] == secret
    captured = capsys.readouterr()
    assert "PS_RUN_SECRET" in caplog.text  # the name may be logged
    assert secret not in caplog.text + captured.out + captured.err
