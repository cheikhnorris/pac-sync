"""versions / diff / lkg / rollback --to lkg through the CLI functions (simulated ZIA, demo-like config)."""

import json

import pytest

from pac_sync import main as m
from pac_sync import zia_cli
from pac_sync.envfile import ConfigError

from .helpers import add_rule, demo_cfg, sim, statuses


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """EMEA with v1 (bootstrap) LKG and v2 DEPLOYED (a rule was added), NA bootstrapped."""
    cfg = demo_cfg(tmp_path, monkeypatch)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0
    return cfg, sim(cfg), tmp_path


def test_versions_lists_everything(ready):
    cfg, client, _ = ready
    lines = []
    assert zia_cli.versions(cfg, "EMEA", out=lines.append) == 0
    text = "\n".join(lines)
    assert lines[0].startswith("EMEA: PAC file id") and "2 version(s)" in lines[0]
    header = lines[1]
    for column in ("status", "verified", "created", "modified", "by", "sync", "message"):
        assert column in header
    v1, v2 = lines[2], lines[3]
    assert " LKG " in v1 and "bootstrap" in v1 and "pac-sync-sim" in v1 and " yes " in v1
    assert " DEPLOYED " in v2 and "VERIFY_NOERR" in v2 and "+PACR0009001" in v2
    assert "PAC Sync 2" in text and "sha256:" in text
    assert "2026-" in v2 or "2027-" in v2  # human readable times


def test_diff_between_versions_and_render(ready):
    cfg, client, tmp_path = ready
    add_rule(cfg, tmp_path, "later-app.example.com", number="PACR0009002", sys_id="rnew2")
    lines = []
    assert zia_cli.diff(cfg, "EMEA", "deployed", "render", out=lines.append) == 0
    diff = "\n".join(lines)
    assert diff.startswith("--- EMEA deployed") and "+++ EMEA render" in diff
    assert '+    if (h === "later-app.example.com") return "DIRECT"; // PACR0009002' in diff
    assert 'new-app.example.com' not in "\n".join(l for l in diff.splitlines() if l.startswith(("+", "-")))  # already deployed

    lines.clear()
    zia_cli.diff(cfg, "EMEA", "1", "deployed", out=lines.append)
    assert '+    if (h === "new-app.example.com")' in "\n".join(lines)
    lines.clear()
    zia_cli.diff(cfg, "EMEA", "lkg", "2", out=lines.append)  # lkg is v1
    assert '+    if (h === "new-app.example.com")' in "\n".join(lines)
    lines.clear()
    zia_cli.diff(cfg, "EMEA", "deployed", "2", out=lines.append)
    assert "identical" in lines[0]
    assert zia_cli.diff(cfg, "EMEA", "staged", "2", out=lines.append) == 2  # no staged version
    with pytest.raises(ConfigError):
        zia_cli.diff(cfg, "EMEA", "bogus", "2", out=lines.append)


def test_rollback_to_lkg_deploys_it_pauses_and_logs(ready):
    cfg, client, tmp_path = ready
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "LKG"), (2, "DEPLOYED")]
    assert m.rollback(cfg, "EMEA", to_lkg=True) == 0
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "DEPLOYED"), (2, "LKG")]
    assert "new-app" not in (tmp_path / "sim" / "demo-emea-proxy.pac" / "deployed.pac").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "pac-state.json").read_text(encoding="utf-8"))["EMEA"]
    assert "rollback to v1" in saved["paused_reason"]
    row = cfg["_sources"][-1].deployments[-1]
    assert row["u_status"] == "rolled_back" and row["u_zia_version"] == 1
    assert row["u_summary"].startswith("PAC Sync ") and "rollback-to v1" in row["u_summary"]
    assert client.zia.activate.status()[0].status == "ACTIVE"


def test_rollback_version_still_works_and_needs_one_selector(ready):
    cfg, client, _ = ready
    assert m.rollback(cfg, "EMEA", version=1) == 0
    assert statuses(client, "demo-emea-proxy.pac")[0] == (1, "DEPLOYED")
    assert m.rollback(cfg, "EMEA", version=99) == 2  # no such version
    with pytest.raises(ConfigError):
        m.rollback(cfg, "EMEA")
    with pytest.raises(ConfigError):
        m.rollback(cfg, "EMEA", version=1, to_lkg=True)


def test_rollback_to_lkg_without_an_lkg_is_an_error(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.rollback(cfg, "NA", to_lkg=True) == 2


def test_rollback_respects_the_activation_guard(ready):
    cfg, client, _ = ready
    client.set_foreign_pending()
    activations = client.store.load()["activations"]
    assert m.rollback(cfg, "EMEA", to_lkg=True) == 1
    assert client.store.load()["activations"] == activations
    assert statuses(client, "demo-emea-proxy.pac")[0] == (1, "DEPLOYED")
    assert "not activated" in cfg["_sources"][-1].deployments[-1]["u_summary"]


def test_lkg_commands(ready):
    cfg, client, _ = ready
    assert zia_cli.lkg(cfg, "EMEA", None, remove=True, new_lkg=None) == 0
    assert all(s != "LKG" for _, s in statuses(client, "demo-emea-proxy.pac"))
    assert zia_cli.lkg(cfg, "EMEA", 1, remove=False) == 0
    assert statuses(client, "demo-emea-proxy.pac")[0] == (1, "LKG")
    assert zia_cli.lkg(cfg, "EMEA", None, remove=True, new_lkg=None) == 0
    with pytest.raises(ConfigError):
        zia_cli.lkg(cfg, "EMEA", 1, remove=True)
    with pytest.raises(ConfigError):
        zia_cli.lkg(cfg, "EMEA", 1, remove=False, new_lkg=2)


def test_cli_argument_parsing(ready, capsys, monkeypatch):
    cfg, client, tmp_path = ready
    path = tmp_path / "cfg.yaml"
    import yaml
    path.write_text(yaml.safe_dump({k: v for k, v in cfg.items() if not k.startswith("_")}), encoding="utf-8")
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "versions", "--region", "EMEA"]) == 0
    assert "2 version(s)" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # --version and --to are mutually exclusive, one is required
        m.main(["--config", str(path), "rollback", "--region", "EMEA"])
    with pytest.raises(SystemExit):
        m.main(["--config", str(path), "rollback", "--region", "EMEA", "--version", "1", "--to", "lkg"])
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "rollback", "--region", "EMEA",
                   "--to", "lkg"]) == 0
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "diff", "--region", "EMEA",
                   "--from", "deployed", "--to", "render"]) == 0
