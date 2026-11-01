"""history.state_file (and so the run lock beside it) belongs to the config's project, not to the current directory."""

import os

import yaml

from pac_sync import main as m

from .conftest import ROOT
from .fake_zia import install_fake_zia


def _project(tmp_path, monkeypatch, state_file="pac-state.json"):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load((ROOT / "config" / "config.demo.yaml").read_text(encoding="utf-8"))
    cfg["servicenow"].update(source="file", sample_file=str(ROOT / "config" / "servicenow_sample_data.json"))
    cfg["render"]["template"] = str(ROOT / "templates" / "proxy.pac.j2")
    install_fake_zia(monkeypatch, tmp_path / "sim")
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
    if state_file is None:
        cfg["history"].pop("state_file")
    else:
        cfg["history"]["state_file"] = state_file
    path = project / "config" / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return project, path


def test_relative_state_file_is_resolved_against_the_project(tmp_path, monkeypatch):
    project, path = _project(tmp_path, monkeypatch, "./state/pac-state.json")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = m.load_config(str(path))
    assert cfg["history"]["state_file"] == str(project.resolve() / "state" / "pac-state.json")
    assert "archive_dir" not in cfg["history"] or not cfg["history"]["archive_dir"]


def test_default_and_absolute_values(tmp_path, monkeypatch):
    project, path = _project(tmp_path, monkeypatch, None)
    monkeypatch.chdir(tmp_path)
    assert m.load_config(str(path))["history"]["state_file"] == str(project.resolve() / "pac-state.json")
    absolute = str(tmp_path / "abs" / "s.json")
    project, path = _project(tmp_path, monkeypatch, absolute)
    assert m.load_config(str(path))["history"]["state_file"] == absolute


def test_git_modes_are_left_alone(tmp_path, monkeypatch):
    project, path = _project(tmp_path, monkeypatch)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["history"] = {"mode": "git", "repo_path": "./pac-history-x"}
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert m.load_config(str(path))["history"] == {"mode": "git", "repo_path": "./pac-history-x"}


def test_a_run_from_another_directory_keeps_state_and_lock_in_the_project(tmp_path, monkeypatch):
    project, path = _project(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # ServiceNow data and template are absolute, so only the state file location is under test
    assert m.main(["--config", str(path), "--env-file", str(tmp_path / "none.env"), "bootstrap", "--region", "NA", "--yes"]) == 0
    assert not (elsewhere / "pac-state.json").exists() and not (elsewhere / ".pac-sync.lock").exists()
    assert not (project / ".pac-sync.lock").exists()  # taken in the project, released after the command
    with m.run_lock(project):  # ... and that is where a second run would find it
        assert os.path.exists(project / ".pac-sync.lock")
