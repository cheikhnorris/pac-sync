"""history.mode: both (git + zia-archive/) and zia with archive_dir: a version ZIA is about to delete is kept first."""

import json
import subprocess

from pac_sync import main as m

from .helpers import PAC, SEED_MESSAGE, add_rule, demo_cfg, sim, statuses


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _fill_to_the_cap(cfg, name="demo-emea-proxy.pac"):
    """PAC file with v1 DEPLOYED and v2..v10 UNSTAGED (ZIA's 10-version cap)."""
    client = sim(cfg)
    api = client.zia.pac_files
    pac_id = api.add_pac_file(name=name, domain="example.com", pac_content=PAC, pac_commit_message=SEED_MESSAGE)[0].id
    client.zia.activate.activate()
    for n in range(2, 11):
        api.clone_pac_file(pac_id, "1", PAC + f"// old {n}\n", "UNSTAGED", pac_commit_message=f"PAC Sync 2026010{n % 10}T000000Z EMEA sha256:{n:012d}")
    client.zia.activate.activate()
    return client, pac_id


def test_both_mode_archives_the_version_before_zia_deletes_it(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch, history="both")
    cfg["zscaler"]["bootstrap"] = "off"
    client, pac_id = _fill_to_the_cap(cfg)
    calls = []
    real = client.zia.pac_files.clone_pac_file

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    client.zia.pac_files.clone_pac_file = spy  # same client object is rebuilt by make_zscaler_client: patch the factory
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    cfg["regions"]["NA"]["mode"] = "paused"
    cfg["regions"]["APAC"]["mode"] = "paused"
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0

    clone = calls[-1]
    assert clone["delete_version"] == 2  # explicit: lowest version that is not DEPLOYED / LKG / STAGE
    got = dict(statuses(client, "demo-emea-proxy.pac"))
    assert 2 not in got and got[1] == "LKG" and got[11] == "DEPLOYED"  # v1 stayed (protected) and is LKG after the auto deploy of v11
    assert 'evicting v2 (UNSTAGED, "PAC Sync 20260102T000000Z EMEA sha256:000000000002")' in caplog.text

    repo = str(tmp_path / "history")
    archive = tmp_path / "history" / "regions" / "EMEA" / "zia-archive"
    assert (archive / "v2.pac").read_text(encoding="utf-8") == PAC + "// old 2\n"
    meta = json.loads((archive / "v2.json").read_text(encoding="utf-8"))
    assert meta["version"] == 2 and meta["status"] == "UNSTAGED" and meta["message"].startswith("PAC Sync 20260102T")
    assert meta["modified_by"] == "pac-sync-sim" and meta["name"] == "demo-emea-proxy.pac"
    committed = _git(repo, "log", "--name-only", "--format=")
    assert "regions/EMEA/zia-archive/v2.pac" in committed and "regions/EMEA/zia-archive/v2.json" in committed
    assert _git(repo, "status", "--porcelain").strip() == ""  # committed, nothing left over


def test_git_mode_only_logs_the_eviction(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch, history="git")
    cfg["zscaler"]["bootstrap"] = "off"
    client, _ = _fill_to_the_cap(cfg)
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    cfg["regions"]["NA"]["mode"] = cfg["regions"]["APAC"]["mode"] = "paused"
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0
    assert "evicting v2" in caplog.text
    assert not (tmp_path / "history" / "regions" / "EMEA" / "zia-archive").exists()


def test_zia_mode_archives_to_archive_dir_when_set(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch, history="zia")
    cfg["zscaler"]["bootstrap"] = "off"
    cfg["history"]["archive_dir"] = str(tmp_path / "archive")
    client, _ = _fill_to_the_cap(cfg)
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    cfg["regions"]["NA"]["mode"] = cfg["regions"]["APAC"]["mode"] = "paused"
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0
    assert (tmp_path / "archive" / "EMEA" / "v2.pac").read_text(encoding="utf-8") == PAC + "// old 2\n"
    assert json.loads((tmp_path / "archive" / "EMEA" / "v2.json").read_text(encoding="utf-8"))["version"] == 2


def test_zia_mode_without_archive_dir_only_warns(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch, history="zia")
    cfg["zscaler"]["bootstrap"] = "off"
    client, _ = _fill_to_the_cap(cfg)
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    cfg["regions"]["NA"]["mode"] = cfg["regions"]["APAC"]["mode"] = "paused"
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0
    assert "not archived" in caplog.text and "evicting v2" in caplog.text
