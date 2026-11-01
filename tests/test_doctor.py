"""doctor: read-only checks with the file source and the simulated ZIA (no network)."""

import pytest
import yaml

from pac_sync import doctor as d
from pac_sync.deployer import ZiaDeployer
from pac_sync.simzia import SimulatedZscalerClient

from .conftest import ROOT

SEED_MESSAGE = "PAC Sync 20260101T000000Z seed sha256:000000000000"
SECRETS = {"SNOW_CLIENT_SECRET": "snow-Secret-123", "ZSCALER_CLIENT_SECRET": "zs-Secret-456"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    data = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    data["history"]["repo_path"] = str(tmp_path / "history")
    data["zscaler"].update(target="simulated", simulated={"state_dir": str(tmp_path / "sim")}, pac_domain="example.com")
    data["regions"] = {"EMEA": {"mode": "auto"}, "NA": {"mode": "stage"}, "APAC": {"mode": "paused"}}
    path = tmp_path / "config.yaml"

    def run(cfg=None, env_file=None):
        path.write_text(yaml.safe_dump(cfg or data), encoding="utf-8")
        lines = []
        code = d.run_doctor(str(path), env_file or str(tmp_path / "no.env"), out=lines.append)
        return code, "\n".join(lines)

    return data, run, tmp_path


def _create_pacs(tmp_path, *names):
    deployer = ZiaDeployer(SimulatedZscalerClient(str(tmp_path / "sim")))
    for name in names:
        deployer.bootstrap(name, 'function FindProxyForURL(url, host) { return "DIRECT"; }\n', "example.com", SEED_MESSAGE)
    deployer.activate()  # the simulator reports PENDING after an unactivated write


def test_all_ok_with_file_source_and_sim(setup):
    data, run, tmp_path = setup
    _create_pacs(tmp_path, "emea-proxy.pac", "na-proxy.pac")
    code, out = run()
    assert code == 0, out
    assert "[FAIL]" not in out and "[WARN]" not in out
    assert "zia EMEA: 'emea-proxy.pac' id 1, deployed v1" in out
    assert "zia validate NA: current render passes validate_pac_file" in out
    assert "zia APAC: paused" in out
    assert "doctor:" in out.splitlines()[-1]


def test_missing_pac_fails_naming_region(setup):
    data, run, tmp_path = setup
    _create_pacs(tmp_path, "na-proxy.pac")
    code, out = run()
    assert code == 1
    fail = [line for line in out.splitlines() if line.startswith("[FAIL]")]
    assert len(fail) == 1 and "region EMEA" in fail[0] and "pac-sync bootstrap --region EMEA" in fail[0]

    data["zscaler"]["bootstrap"] = "missing"  # the next run would create it: warning only
    code, out = run(data)
    assert code == 0 and "[WARN] zia EMEA: no PAC file 'emea-proxy.pac' yet" in out


def test_missing_env_var_fails_naming_it(setup, monkeypatch):
    data, run, tmp_path = setup
    data["zscaler"]["target"] = "live"
    for name in ("ZSCALER_CLIENT_ID", "ZSCALER_CLIENT_SECRET", "ZSCALER_VANITY_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    code, out = run(data)
    assert code == 1
    assert "[FAIL] env ZSCALER_CLIENT_SECRET: not set" in out
    assert "[FAIL] zia client: environment variable ZSCALER_CLIENT_SECRET is not set (zscaler.auth=oneapi)" in out


def test_secret_values_never_printed(setup, monkeypatch):
    data, run, tmp_path = setup
    data["zscaler"]["target"] = "live"
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in SECRETS.items()), encoding="utf-8")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    # Live config, but the client is the simulator: no tenant is called.
    monkeypatch.setattr(d, "make_zscaler_client", lambda cfg: SimulatedZscalerClient(str(tmp_path / "sim")))
    _create_pacs(tmp_path, "emea-proxy.pac", "na-proxy.pac")
    code, out = run(data, env_file=str(env_file))
    assert f"env ZSCALER_CLIENT_SECRET: set ({len(SECRETS['ZSCALER_CLIENT_SECRET'])} chars)" in out
    assert not any(v in out for v in SECRETS.values())
    assert code == 0, out


def test_broken_config_is_a_fail_line(tmp_path):
    lines = []
    assert d.run_doctor(str(tmp_path / "missing.yaml"), str(tmp_path / "no.env"), out=lines.append) == 1
    assert lines[0].startswith("[FAIL] config:") and "pac-sync setup" in lines[0]


# --- Phase 1b: prefix, pending activation, legacy + guard, headroom, drift, write probe ---------------------------


from .helpers import PAC, demo_cfg, sim, statuses  # noqa: E402


def _demo(tmp_path, monkeypatch, **kw):
    cfg = demo_cfg(tmp_path, monkeypatch, **kw)
    path = tmp_path / "demo.yaml"
    client = sim(cfg)
    monkeypatch.setattr(d, "make_zscaler_client", lambda z: client)  # live-style config, no tenant: the simulator
    lines = []

    def run(**kwargs):
        lines.clear()
        public = {k: v for k, v in cfg.items() if not k.startswith("_")}
        path.write_text(yaml.safe_dump(public), encoding="utf-8")
        code = d.run_doctor(str(path), str(tmp_path / "no.env"), out=lines.append, **kwargs)
        return code, "\n".join(lines)

    return cfg, client, run


def _seed_demo_pacs(client, *names):
    from pac_sync.deployer import ZiaDeployer

    seed = ZiaDeployer(client)
    for name in names:
        seed.bootstrap(name, PAC, "example.com", SEED_MESSAGE)
    client.zia.activate.activate()


def test_doctor_shows_effective_names_and_guard(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    code, out = run()
    assert code == 0, out
    assert "[ OK ] pac names: prefix 'demo-', guard ON" in out
    assert "EMEA -> demo-emea-proxy.pac" in out and "NA -> demo-na-proxy.pac" in out
    assert "zia EMEA: 'demo-emea-proxy.pac' id" in out
    assert "history mode: zia" in out and "history state_file" in out


def test_doctor_warns_on_pending_activation(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    client.set_foreign_pending()
    code, out = run()
    assert code == 0  # a warning, not a failure
    assert "[WARN] zia activation status: PENDING" in out and "PAC Sync will not activate" in out
    cfg["deploy"]["skip_activate_if_pending"] = False
    assert "would publish them too" in run()[1]


def test_doctor_warns_on_legacy_with_the_guard(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    cfg["zscaler"].update(target="live", auth="legacy")
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    for name in ("ZIA_USERNAME", "ZIA_PASSWORD", "ZIA_API_KEY", "ZIA_CLOUD"):
        monkeypatch.setenv(name, "x")
    assert "[WARN] zia legacy: auth: legacy and skip_activate_if_pending" in run()[1]
    cfg["deploy"]["skip_activate_if_pending"] = False
    assert "zia legacy" not in run()[1]
    cfg["deploy"]["skip_activate_if_pending"] = True
    cfg["zscaler"]["auth"] = "oneapi"
    assert "zia legacy" not in run()[1]


def test_doctor_warns_on_version_headroom_and_drift(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    for n in range(2, 10):  # v1 DEPLOYED + 8 LKG = 9 versions, none deletable
        api.clone_pac_file(pac_id, "1", PAC + f"// {n}\n", "LKG", pac_commit_message="by hand")
    client.zia.activate.activate()
    code, out = run()
    assert code == 0
    assert "[WARN] zia headroom EMEA: 9 versions, none deletable" in out
    new = api.clone_pac_file(pac_id, "1", PAC + "// manual\n", "STAGE", pac_commit_message="portal edit")[0]
    api.update_pac_file(pac_id, new.pac_version, "DEPLOY")
    client.zia.activate.activate()
    assert "[WARN] zia drift EMEA: manual change in ZIA" in run()[1]


def test_write_probe_passes_on_prefixed_files_and_activates_only_itself(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    activations = client.store.load()["activations"]
    code, out = run(write_probe=True, region="EMEA")
    assert code == 0, out
    assert "[ OK ] write probe clone: demo-emea-proxy.pac: ok" in out and "write probe unstage" in out
    assert "no STAGE left; activated (nothing else was pending)" in out
    assert not any(s == "STAGE" for _, s in statuses(client, "demo-emea-proxy.pac"))
    assert client.store.load()["activations"] == activations + 1  # its own, and only after ACTIVE was confirmed
    assert client.zia.activate.status()[0].status == "ACTIVE"  # no PENDING left behind
    versions = [v for f in client.pac_files_summary() for v in f["versions"]]
    assert any(v["pac_version_status"] == "UNSTAGED" and v["pac_commit_message"].endswith(" EMEA probe") for v in versions)


def test_run_after_a_probe_is_not_blocked_by_the_guard(tmp_path, monkeypatch):
    from pac_sync import main as m

    from .helpers import add_rule

    cfg, client, run = _demo(tmp_path, monkeypatch)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    assert run(write_probe=True, region="EMEA")[0] == 0
    add_rule(cfg, tmp_path, "after-probe.example.com")
    assert m.sync(cfg) == 0  # the README demo sequence: probe, then run (exit 1 here would be the guard blocking)
    assert statuses(client, "demo-emea-proxy.pac")[-1][1] == "DEPLOYED"
    assert "not activated" not in " ".join(d["u_summary"] for d in cfg["_sources"][-1].deployments)


def test_write_probe_is_refused_while_zia_is_pending(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    client.set_foreign_pending()
    before = (client.store.load()["activations"], statuses(client, "demo-emea-proxy.pac"))
    code, out = run(write_probe=True, region="EMEA")
    assert code == 1 and "[FAIL] write probe: refused: ZIA is not ACTIVE" in out and "coordinate in the ZIA portal" in out
    assert (client.store.load()["activations"], statuses(client, "demo-emea-proxy.pac")) == before  # not even a clone
    assert client.store.load()["foreign_pending"] is True  # the other admin's change is still pending, unpublished


def test_write_probe_reports_a_failing_activation(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    client.zia.activate.activate = lambda: (_ for _ in ()).throw(RuntimeError("403 no activation permission"))
    code, out = run(write_probe=True, region="EMEA")
    assert code == 1
    assert "[FAIL] write probe activate: the probe's changes are saved in ZIA but NOT activated: 403 no activation permission" in out


def test_write_probe_at_the_cap_deletes_a_safe_version_and_archives_it(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    api.clone_pac_file(pac_id, "1", PAC + "// lkg\n", "LKG", pac_commit_message="PAC Sync 20260101T000000Z EMEA sha256:0")
    for n in range(3, 11):
        api.clone_pac_file(pac_id, "1", PAC + f"// {n}\n", "UNSTAGED", pac_commit_message=f"PAC Sync 2026010{n % 10}T000000Z EMEA sha256:{n}")
    client.zia.activate.activate()
    assert len(statuses(client, "demo-emea-proxy.pac")) == 10 and statuses(client, "demo-emea-proxy.pac")[1] == (2, "LKG")
    cfg["history"]["archive_dir"] = str(tmp_path / "archive")
    code, out = run(write_probe=True, region="EMEA")
    assert code == 0, out
    got = dict(statuses(client, "demo-emea-proxy.pac"))
    assert got[1] == "DEPLOYED" and got[2] == "LKG" and 3 not in got  # the SDK default would have taken v1
    assert (tmp_path / "archive" / "EMEA" / "v3.pac").exists()


def test_write_probe_at_the_cap_with_nothing_deletable_changes_nothing(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    for n in range(2, 11):
        api.clone_pac_file(pac_id, "1", PAC + f"// {n}\n", "LKG")
    client.zia.activate.activate()
    before = statuses(client, "demo-emea-proxy.pac")
    code, out = run(write_probe=True, region="EMEA")
    assert code == 1 and "none can be deleted" in out
    assert statuses(client, "demo-emea-proxy.pac") == before


def test_drift_warning_respects_a_version_accepted_with_resume(tmp_path, monkeypatch):
    from pac_sync import main as m

    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "demo-emea-proxy.pac", "demo-na-proxy.pac", "demo-apac-proxy.pac")
    api = client.zia.pac_files
    pac_id = next(f["id"] for f in client.pac_files_summary() if f["name"] == "demo-emea-proxy.pac")
    new = api.clone_pac_file(pac_id, "1", PAC + "// hand\n", "STAGE", pac_commit_message="portal edit")[0]
    api.update_pac_file(pac_id, new.pac_version, "DEPLOY")
    client.zia.activate.activate()
    assert "[WARN] zia drift EMEA: manual change in ZIA" in run()[1]
    assert m.resume(cfg, "EMEA") == 0  # accept it (drift_ack) ...
    assert "zia drift EMEA" not in run()[1]  # ... doctor honours the acknowledgement before the next run uses it
    m.sync(cfg)  # the run records drift_accepted
    assert "zia drift EMEA" not in run()[1]


def test_write_probe_refused_without_a_prefix_and_without_region(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    _seed_demo_pacs(client, "emea-proxy.pac")
    cfg["zscaler"].update(pac_name_prefix="", only_prefixed=False)
    code, out = run(write_probe=True, region="EMEA")
    assert code == 1 and "[FAIL] write probe: refused" in out and "prefixed files" in out
    assert [s for _, s in statuses(client, "emea-proxy.pac")] == ["DEPLOYED"]  # untouched
    code, out = run(write_probe=True)
    assert "--write-probe needs --region X" in out


def test_write_probe_for_a_missing_file_says_to_bootstrap(tmp_path, monkeypatch):
    cfg, client, run = _demo(tmp_path, monkeypatch)
    code, out = run(write_probe=True, region="EMEA")
    assert code == 1 and "run `pac-sync bootstrap --region EMEA` first" in out
