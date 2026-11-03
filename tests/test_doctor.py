"""doctor: read-only checks with the file source and the fake ZIA (no network)."""

import pytest
import yaml

from pac_sync import doctor as d
from pac_sync.deployer import ZiaDeployer
from pac_sync import deployer

from .conftest import ROOT
from .fake_zia import SimulatedZscalerClient, install_fake_zia

SEED_MESSAGE = "PAC Sync 20260101T000000Z seed sha256:000000000000"
SECRETS = {"SNOW_CLIENT_SECRET": "snow-Secret-123", "ZSCALER_CLIENT_SECRET": "zs-Secret-456"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    data = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    data["history"]["repo_path"] = str(tmp_path / "history")
    data["zscaler"].update(pac_domain="example.com")
    install_fake_zia(monkeypatch, tmp_path / "sim")
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
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
    for name in ("ZSCALER_CLIENT_ID", "ZSCALER_CLIENT_SECRET", "ZSCALER_VANITY_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    monkeypatch.setattr(d, "make_zscaler_client", deployer.make_zscaler_client)  # the real factory, not the fake
    code, out = run(data)
    assert code == 1
    assert "[FAIL] env ZSCALER_CLIENT_SECRET: not set" in out
    assert "[FAIL] zia client: environment variable ZSCALER_CLIENT_SECRET is not set (zscaler.auth=oneapi)" in out


def test_secret_values_never_printed(setup, monkeypatch):
    data, run, tmp_path = setup
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in SECRETS.items()), encoding="utf-8")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    # The client is the fake (installed by the fixture): no tenant is called.
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


# -- proxy / CA bundle lines ---------------------------------------------------------------------------------------

@pytest.fixture
def proxy_env(monkeypatch):
    """No proxy variables; the caller sets what it needs (a registry proxy on the machine is only read when none are set)."""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
              "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_proxy_shown_masked_never_the_value(setup, proxy_env):
    data, run, tmp_path = setup
    _create_pacs(tmp_path, "emea-proxy.pac", "na-proxy.pac")
    value = "http://alice:S3cr%2Ft@proxy.corp:8080"
    proxy_env.setenv("HTTPS_PROXY", value)
    proxy_env.setenv("NO_PROXY", "localhost,127.0.0.1")
    code, out = run()
    assert f"[ OK ] proxy zia: http://***@proxy.corp:8080 ({len(value)} chars), with credentials" in out
    assert "alice" not in out and "S3cr" not in out
    assert code == 0, out


def test_proxy_malformed_credentials_warn_without_value(setup, proxy_env):
    data, run, tmp_path = setup
    proxy_env.setenv("HTTPS_PROXY", "http://alice:pa/ss@proxy.corp:8080")
    _code, out = run()
    assert "[WARN] proxy zia: set (" in out and "malformed" in out
    assert "pa/ss" not in out and "alice" not in out


def test_proxy_direct(setup, proxy_env):
    data, run, tmp_path = setup
    proxy_env.setattr("requests.utils.get_environ_proxies", lambda url, no_proxy=None: {})
    _code, out = run()
    assert "[ OK ] proxy zia: direct" in out


def test_ca_bundle_missing_fails(setup, proxy_env):
    data, run, tmp_path = setup
    missing = str(tmp_path / "nope.pem")
    proxy_env.setenv("REQUESTS_CA_BUNDLE", missing)
    code, out = run()
    assert f"[FAIL] ca bundle: {missing} not found" in out
    assert code == 1


def test_ca_bundle_present_ok(setup, proxy_env):
    data, run, tmp_path = setup
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    proxy_env.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    _code, out = run()
    assert f"[ OK ] ca bundle: {bundle}" in out


def test_no_ca_bundle_line_when_unset(setup, proxy_env):
    data, run, tmp_path = setup
    _code, out = run()
    assert "ca bundle" not in out


def test_proxy_servicenow_direct_via_no_proxy(proxy_env):
    proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    proxy_env.setenv("HTTP_PROXY", "http://proxy.corp:8080")  # the mock is http://, so this is what NO_PROXY must beat
    ctx = d.Context(config_path="", env_file="",
                    cfg={"servicenow": {"source": "api", "instance_url": "http://127.0.0.1:8090"}})
    checks = {label: text for _s, label, text in d.check_proxy(ctx)}
    assert checks["proxy servicenow"].startswith("http://proxy.corp:8080")
    proxy_env.setenv("NO_PROXY", "localhost,127.0.0.1")
    checks = {label: text for _s, label, text in d.check_proxy(ctx)}
    assert checks["proxy servicenow"] == "direct"
    assert checks["proxy zia"].startswith("http://proxy.corp:8080")


def test_describe_proxy_ipv6_host():
    assert d._describe_proxy("http://[::1]:8080")[1].startswith("http://[::1]:8080 ")
