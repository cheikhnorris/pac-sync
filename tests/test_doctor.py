"""doctor: read-only checks with the file source and the simulated ZIA (no network)."""

import pytest
import yaml

from pac_sync import doctor as d
from pac_sync.deployer import ZiaDeployer
from pac_sync.simzia import SimulatedZscalerClient

from .conftest import ROOT

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
        deployer.bootstrap(name, 'function FindProxyForURL(url, host) { return "DIRECT"; }\n', "example.com", "seed")


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
