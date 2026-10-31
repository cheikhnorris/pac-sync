"""setup wizard: profiles, no overwrite without --force, secrets only in .env."""

import yaml

from pac_sync.envfile import parse_env_file
from pac_sync.setup_wizard import run_setup


def _run(tmp_path, **kw):
    lines = []
    code = run_setup(config_out=str(tmp_path / "config.yaml"), env_out=str(tmp_path / ".env"), doctor=False,
                     out=lines.append, **kw)
    return code, "\n".join(lines)


def test_mock_profile_non_interactive(tmp_path):
    code, out = _run(tmp_path, profile="mock", non_interactive=True)
    assert code == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["zscaler"]["target"] == "simulated" and cfg["zscaler"]["bootstrap"] == "missing"
    assert cfg["history"] == {**cfg["history"], "repo_path": "./pac-history-mock", "push": False}
    assert cfg["deploy"]["live_max_mode"] == "stage"
    assert {k: v["mode"] for k, v in cfg["regions"].items()} == {"EMEA": "auto", "NA": "stage", "APAC": "auto"}
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8").startswith("# PAC Sync configuration written by")
    assert parse_env_file(str(tmp_path / ".env")) == {"SNOW_CLIENT_ID": "pac-sync", "SNOW_CLIENT_SECRET": "pac-sync-secret"}
    assert "pac-sync-secret" not in out


def test_refuses_overwrite_without_force(tmp_path):
    assert _run(tmp_path, profile="mock", non_interactive=True)[0] == 0
    (tmp_path / "config.yaml").write_text("# mine\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SNOW_CLIENT_ID=my-own\nOTHER=1\n", encoding="utf-8")
    code, out = _run(tmp_path, profile="mock", non_interactive=True)
    assert code == 2 and "--force" in out
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "# mine\n"

    code, out = _run(tmp_path, profile="mock", non_interactive=True, force=True)
    assert code == 0
    assert parse_env_file(str(tmp_path / ".env")) == {"SNOW_CLIENT_ID": "pac-sync", "OTHER": "1",
                                                     "SNOW_CLIENT_SECRET": "pac-sync-secret"}


def test_existing_env_keys_kept_without_force(tmp_path):
    (tmp_path / ".env").write_text("SNOW_CLIENT_ID=my-own\n", encoding="utf-8")
    code, out = _run(tmp_path, profile="mock", non_interactive=True)
    assert code == 0 and "kept existing SNOW_CLIENT_ID" in out
    assert parse_env_file(str(tmp_path / ".env"))["SNOW_CLIENT_ID"] == "my-own"


def test_custom_legacy_secrets_go_to_env_only(tmp_path):
    answers = iter([
        "api", "https://acme.service-now.com", "oauth", "snow-client",  # ServiceNow
        "4", "api-admin@acme.com", "zscalertwo", "acme.com",            # ZIA legacy: user, cloud, pac_domain
        "./pac-history", "y", "origin",                                 # history
    ])
    secrets = iter(["snow-Secret-1", "zia-Password-2", "zia-ApiKey-3"])
    code, out = _run(tmp_path, profile="custom", ask=lambda prompt: next(answers), ask_secret=lambda prompt: next(secrets))
    assert code == 0
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert cfg["zscaler"]["target"] == "live" and cfg["zscaler"]["auth"] == "legacy"
    assert cfg["zscaler"]["pac_domain"] == "acme.com" and cfg["history"]["push"] is True
    env = parse_env_file(str(tmp_path / ".env"))
    assert env == {"SNOW_CLIENT_ID": "snow-client", "SNOW_CLIENT_SECRET": "snow-Secret-1", "ZIA_USERNAME": "api-admin@acme.com",
                   "ZIA_PASSWORD": "zia-Password-2", "ZIA_API_KEY": "zia-ApiKey-3", "ZIA_CLOUD": "zscalertwo"}
    for secret in ("snow-Secret-1", "zia-Password-2", "zia-ApiKey-3"):
        assert secret not in text and secret not in out
