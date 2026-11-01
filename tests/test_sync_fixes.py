"""Fixes after Phase 1: failed write-back (A1), atomic run lock (A4), default .env location (A6), bootstrap prompt (A8)."""

import os
import socket

import pytest
import yaml

from pac_sync import main as m
from pac_sync.deployer import ZiaDeployer

from .conftest import ROOT
from .helpers import PAC, SEED_MESSAGE, demo_cfg, sim

# --- A1: a ZIA failure marks the rules of the failing region failed --------------------------------------------


def test_a_failing_region_marks_its_rules_failed_in_servicenow(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["regions"]["NA"]["mode"] = "auto"
    client = sim(cfg)
    # NA exists, EMEA does not and bootstrap is off: EMEA's stage fails, NA's works
    ZiaDeployer(client).bootstrap("demo-na-proxy.pac", PAC, "example.com", SEED_MESSAGE)
    ZiaDeployer(client).bootstrap("demo-apac-proxy.pac", PAC, "example.com", SEED_MESSAGE)
    client.zia.activate.activate()
    cfg["zscaler"]["bootstrap"] = "off"
    assert m.sync(cfg) == 1

    updates = dict(cfg["_sources"][-1].rule_updates)
    msg = "EMEA: stage failed: no ZIA PAC file named 'demo-emea-proxy.pac'"
    assert updates["r1002"]["u_deploy_status"] == "failed" and updates["r1002"]["u_deploy_message"].startswith(msg)  # EMEA only
    # EMEA + NA: deployed in NA but EMEA failed -> failed, coherent for the requester
    assert updates["r1042"]["u_deploy_status"] == "failed" and "EMEA:" in updates["r1042"]["u_deploy_message"]
    assert "u_state" not in updates["r1002"]  # the lifecycle state is not touched
    # NA only: fine
    assert updates["r1043"]["u_deploy_status"] == "deployed"
    # a global rule lands in every region, so it fails too
    assert updates["r1001"]["u_deploy_status"] == "failed"
    row = next(d for d in cfg["_sources"][-1].deployments if d["u_status"] == "failed")
    assert "demo-emea-proxy.pac" in row["u_summary"]


def test_a_later_good_run_replaces_the_failure(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["zscaler"]["bootstrap"] = "off"
    assert m.sync(cfg) == 1
    assert dict(cfg["_sources"][-1].rule_updates)["r1002"]["u_deploy_status"] == "failed"
    cfg["zscaler"]["bootstrap"] = "missing"
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    m.sync(cfg)
    assert dict(cfg["_sources"][-1].rule_updates)["r1002"]["u_deploy_status"] == "deployed"


def test_compute_rule_updates_keeps_unaffected_rules_alone():
    from datetime import datetime, timezone

    from pac_sync.normalizer import normalize
    from pac_sync.writeback import compute_rule_updates

    import json
    sample = json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))
    from pac_sync.models import ProxyProfile, Region
    profiles = {p["sys_id"]: ProxyProfile(p["sys_id"], p["u_name"], p["u_proxy_chain"]) for p in sample["u_pac_proxy_profile"]}
    regions = {r["sys_id"]: Region(r["sys_id"], r["u_code"], r["u_zia_pac_name"], profiles[r["u_default_profile"]].chain)
               for r in sample["u_pac_region"]}
    res = normalize(sample["u_pac_rule"], regions, profiles, datetime(2026, 11, 1, tzinfo=timezone.utc))
    raw = {r["sys_id"]: r for r in sample["u_pac_rule"]}
    states = {c: {"deployed": {"hash": "h", "version": 3}} for c in ("EMEA", "NA", "APAC")}
    hashes = {c: "h" for c in states}
    ups = dict(compute_rule_updates(res, raw, states, hashes, "2026-11-01 00:00:00", failed={"NA": "boom"}))
    assert ups["r1043"]["u_deploy_status"] == "failed" and ups["r1043"]["u_deploy_message"] == "NA: boom"  # NA only
    assert ups["r1042"]["u_deploy_status"] == "failed"  # EMEA + NA
    assert ups["r1002"]["u_deploy_status"] == "deployed" and ups["r1002"]["u_deploy_message"] == ""  # EMEA only


# --- A4: the run lock is atomic and only removed by its owner ---------------------------------------------------


def test_lock_is_exclusive_and_records_pid_and_host(tmp_path):
    with m.run_lock(tmp_path):
        lock = tmp_path / ".pac-sync.lock"
        assert lock.read_text(encoding="utf-8") == f"{os.getpid()}@{socket.gethostname()}"
        with pytest.raises(RuntimeError, match="another run holds"):
            with m.run_lock(tmp_path):
                pytest.fail("a second run must not get the lock")
        assert lock.exists()  # the failed attempt did not remove the first run's lock
    assert not lock.exists()


def test_lock_is_not_removed_when_it_is_no_longer_ours(tmp_path):
    with m.run_lock(tmp_path):
        (tmp_path / ".pac-sync.lock").write_text("999@otherhost", encoding="utf-8")  # taken over (e.g. after a stale break)
    assert (tmp_path / ".pac-sync.lock").read_text(encoding="utf-8") == "999@otherhost"


def test_stale_lock_is_broken_once(tmp_path):
    lock = tmp_path / ".pac-sync.lock"
    lock.write_text("1@dead", encoding="utf-8")
    os.utime(lock, (1, 1))
    with m.run_lock(tmp_path, stale_after=60):
        assert lock.read_text(encoding="utf-8").startswith(str(os.getpid()))
    assert not lock.exists()


def test_lock_released_after_an_exception(tmp_path):
    with pytest.raises(ValueError):
        with m.run_lock(tmp_path):
            raise ValueError("boom")
    assert not (tmp_path / ".pac-sync.lock").exists()
    with m.run_lock(tmp_path):
        pass


# --- A6: the default .env is the project's ------------------------------------------------------------------------


def test_main_loads_the_env_file_next_to_the_config_folder(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    cfg = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["render"]["template"] = str(ROOT / "templates" / "proxy.pac.j2")
    cfg["servicenow"]["sample_file"] = str(ROOT / "config" / "servicenow_sample_data.json")
    (project / "config" / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (project / ".env").write_text("PS_PROJECT_ENV=from-project\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PS_CWD_ENV=from-cwd\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("PS_PROJECT_ENV", raising=False)
    monkeypatch.delenv("PS_CWD_ENV", raising=False)

    assert m.main(["--config", str(project / "config" / "config.yaml"), "render", "--region", "EMEA"]) == 0
    assert os.environ["PS_PROJECT_ENV"] == "from-project"  # the project's .env, not the cwd's
    assert "PS_CWD_ENV" not in os.environ
    assert "FindProxyForURL" in capsys.readouterr().out

    monkeypatch.delenv("PS_PROJECT_ENV")
    (project / ".env").unlink()  # without a project file the cwd's is the fallback
    assert m.main(["--config", str(project / "config" / "config.yaml"), "render", "--region", "EMEA"]) == 0
    assert os.environ["PS_CWD_ENV"] == "from-cwd"


# --- A8: no prompt when there is no console ------------------------------------------------------------------------


def test_live_bootstrap_does_not_prompt_without_a_console(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["zscaler"]["target"] = "live"
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: pytest.fail("no client must be built"))
    monkeypatch.setattr(m, "stdin_is_interactive", lambda: False)

    def no_prompt(text):
        pytest.fail("the prompt must not be shown")

    assert m.bootstrap_region(cfg, "EMEA", yes=False, confirm=no_prompt) == 2
    assert "needs --yes (stdin is not interactive)" in caplog.text
