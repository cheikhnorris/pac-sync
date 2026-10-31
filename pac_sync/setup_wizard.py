"""`pac-sync setup`: write config/config.yaml and .env from a profile, asking only what the profile does not know.

Profiles:
    mock    local ServiceNow mock (snow-mock) + simulated ZIA, history in ./pac-history-mock, no push.
            Writes the mock's public demo OAuth client to .env (SNOW_CLIENT_ID / SNOW_CLIENT_SECRET).
    custom  starts from config/config.example.yaml (real ServiceNow, live ZIA).

Secrets are read with getpass and written only to the .env file (git-ignored); the config keeps env-var names.
Nothing secret is printed. An existing config is never overwritten without --force; existing .env keys are kept
unless --force.
"""

from __future__ import annotations

import getpass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

from .envfile import load_env_file, parse_env_file, write_env_file

PROFILES = {"mock": "config.mock.yaml", "custom": "config.example.yaml"}
MOCK_ENV = {"SNOW_CLIENT_ID": "pac-sync", "SNOW_CLIENT_SECRET": "pac-sync-secret"}  # public demo client of snow-mock
ZIA_CHOICES = {
    "1": "simulated",
    "2": "oneapi-secret",
    "3": "oneapi-private-key",
    "4": "legacy",
}


def profile_path(profile: str) -> Path:
    """The shipped profile file: next to the package (editable install) or under ./config."""
    name = PROFILES[profile]
    for base in (Path(__file__).resolve().parents[1] / "config", Path("config")):
        if (base / name).is_file():
            return base / name
    raise FileNotFoundError(f"profile file {name} not found")


class Prompter:
    def __init__(self, ask: Callable[[str], str], ask_secret: Callable[[str], str]):
        self.ask = ask
        self.ask_secret = ask_secret

    def text(self, label: str, default: str = "") -> str:
        answer = self.ask(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
        return answer or default

    def yes(self, label: str, default: bool = False) -> bool:
        answer = self.ask(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer in ("y", "yes")

    def choice(self, label: str, options: Dict[str, str], default: str) -> str:
        shown = ", ".join(f"{k}={v}" for k, v in options.items())
        while True:
            answer = self.ask(f"{label} ({shown}) [{default}]: ").strip() or default
            if answer in options:
                return answer
            if answer in options.values():
                return next(k for k, v in options.items() if v == answer)

    def secret(self, label: str) -> str:
        return self.ask_secret(f"{label} (input hidden, empty = keep current): ").strip()


def _ask_zia(p: Prompter, z: dict, env: Dict[str, str], default_choice: str) -> None:
    kind = ZIA_CHOICES[p.choice("ZIA target", ZIA_CHOICES, default_choice)]
    if kind == "simulated":
        z["target"] = "simulated"
        z.setdefault("simulated", {})["state_dir"] = p.text("Simulated ZIA state folder",
                                                           (z.get("simulated") or {}).get("state_dir") or "./sim-zia")
        return
    z["target"] = "live"
    if kind == "legacy":
        z["auth"] = "legacy"
        names = [("username_env", "ZIA_USERNAME", "ZIA admin user name", False),
                 ("password_env", "ZIA_PASSWORD", "ZIA admin password", True),
                 ("api_key_env", "ZIA_API_KEY", "ZIA API key", True),
                 ("legacy_cloud_env", "ZIA_CLOUD", "ZIA cloud (e.g. zscalertwo)", False)]
        for key, default, label, hidden in names:
            var = z.setdefault(key, default)
            value = p.secret(label) if hidden else p.text(label)
            if value:
                env[var] = value
    else:
        z["auth"] = "oneapi"
        cid = p.text("OneAPI client ID")
        if cid:
            env[z.setdefault("client_id_env", "ZSCALER_CLIENT_ID")] = cid
        if kind == "oneapi-private-key":
            var = z.setdefault("private_key_env", "ZSCALER_PRIVATE_KEY")
            path = p.text("Path to the private key file (PEM)")
            if path:
                env[var] = path
        else:
            z.pop("private_key_env", None)
            secret = p.secret("OneAPI client secret")
            if secret:
                env[z.setdefault("client_secret_env", "ZSCALER_CLIENT_SECRET")] = secret
        vanity = p.text("Vanity domain (the <name> in <name>.zslogin.net)")
        if vanity:
            env[z.setdefault("vanity_domain_env", "ZSCALER_VANITY_DOMAIN")] = vanity
        z["cloud"] = p.text("Zscaler cloud (empty = production, e.g. beta)", z.get("cloud") or "")
    z["pac_domain"] = p.text("Domain for new PAC files (zscaler.pac_domain)", z.get("pac_domain") or "")


def _ask_servicenow(p: Prompter, s: dict, env: Dict[str, str]) -> None:
    s["source"] = p.text("ServiceNow source (api | file)", s.get("source") or "api")
    if s["source"] != "api":
        return
    s["instance_url"] = p.text("ServiceNow instance URL", s.get("instance_url") or "")
    s["auth"] = p.text("ServiceNow auth (oauth | basic)", s.get("auth") or "oauth")
    if s["auth"] == "basic":
        user = p.text("ServiceNow user name")
        if user:
            env[s.setdefault("username_env", "SNOW_USERNAME")] = user
        pw = p.secret("ServiceNow password")
        if pw:
            env[s.setdefault("password_env", "SNOW_PASSWORD")] = pw
    else:
        cid = p.text("ServiceNow OAuth client ID")
        if cid:
            env[s.setdefault("client_id_env", "SNOW_CLIENT_ID")] = cid
        secret = p.secret("ServiceNow OAuth client secret")
        if secret:
            env[s.setdefault("client_secret_env", "SNOW_CLIENT_SECRET")] = secret


def run_setup(profile: Optional[str] = None, non_interactive: bool = False, force: bool = False,
              config_out: str = "config/config.yaml", env_out: str = ".env", doctor: bool = True,
              ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] = getpass.getpass,
              out: Callable[[str], None] = print) -> int:
    p = Prompter(ask, ask_secret)
    config_path, env_path = Path(config_out), Path(env_out)
    if config_path.exists() and not force:
        out(f"{config_path} already exists: not overwritten (use --force to replace it).")
        return 2
    if profile is None:
        profile = "mock" if non_interactive else p.choice("Profile", {"mock": "local demo", "custom": "real tenant"}, "mock")

    cfg = yaml.safe_load(profile_path(profile).read_text(encoding="utf-8"))
    env: Dict[str, str] = dict(MOCK_ENV) if profile == "mock" else {}
    if not non_interactive:
        if profile == "custom":
            _ask_servicenow(p, cfg["servicenow"], env)
        _ask_zia(p, cfg.setdefault("zscaler", {}), env, "1" if profile == "mock" else "2")
        h = cfg["history"]
        h["repo_path"] = p.text("History repository folder", h.get("repo_path") or "./pac-history")
        h["push"] = p.yes("Push the history to a git remote after each run?", bool(h.get("push")))
        if h["push"]:
            h["remote"] = p.text("Remote name", h.get("remote") or "origin")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (f"# PAC Sync configuration written by `pac-sync setup` (profile {profile}, {stamp}).\n"
              f"# Based on config/{PROFILES[profile]}: see that file for every setting with comments.\n"
              "# Secrets are never stored here: every *_env key names a variable set in .env or the environment.\n\n")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(header + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False), encoding="utf-8")
    out(f"Wrote {config_path} (profile {profile}).")

    existing = parse_env_file(str(env_path))
    to_write = {k: v for k, v in env.items() if force or not existing.get(k)}
    kept: List[str] = sorted(k for k in env if k not in to_write)
    if to_write:
        write_env_file(str(env_path), to_write)
        out(f"Wrote {env_path}: {', '.join(sorted(to_write))}" + (f" (kept existing {', '.join(kept)})" if kept else ""))
    elif kept:
        out(f"{env_path}: kept existing {', '.join(kept)}")
    if (cfg.get("history") or {}).get("push"):
        out(f"History push is on: make sure `git -C {cfg['history']['repo_path']} remote get-url "
            f"{cfg['history'].get('remote', 'origin')}` points to your remote.")

    if not doctor:
        return 0
    load_env_file(str(env_path))
    from .doctor import run_doctor

    out("Running doctor:")
    return run_doctor(str(config_path), str(env_path), out=out)

