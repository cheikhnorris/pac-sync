"""PAC Sync entry point.

    pac-sync run      [--config FILE] [--dry-run] [--force]   scheduled job
    pac-sync render   --region EMEA                            print the PAC, no side effects
    pac-sync promote  --region EMEA                            deploy the reviewed STAGE version
    pac-sync rollback --region EMEA --version 41               redeploy an older ZIA version, pause the region
    pac-sync resume   --region EMEA                            clear a rollback pause
    pac-sync status                                            each region's recorded ZIA state (offline)
    pac-sync bootstrap --region EMEA [--yes]                   create the region's PAC file in ZIA
    pac-sync doctor                                            read-only checks of config, credentials, ServiceNow, git, ZIA
    pac-sync setup    [--profile mock|custom]                  write config/config.yaml and .env

Global option --env-file (default .env) loads credentials before the config is read; the process environment wins.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
import yaml

from .deployer import PacFileNotFound, ZiaDeployer, close_zscaler_client, make_zscaler_client, zscaler_target
from .envfile import ConfigError, load_env_file
from .history import GitHistory
from .models import ProxyProfile, Region
from .normalizer import NormalizeResult, normalize
from .renderer import PacRenderer
from .snow_client import make_source
from .writeback import compute_rule_updates

log = logging.getLogger("pac_sync")
MODES = ("stage", "auto", "paused")
MODE_RANK = {"paused": 0, "stage": 1, "auto": 2}
BOOTSTRAP_MODES = ("off", "missing")


@dataclass
class RegionPlan:
    region: Region
    content: str
    hash: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    blocked: Optional[str] = None
    staged_now: bool = False
    deployed_now: bool = False
    bootstrapped: bool = False
    error: Optional[str] = None


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def is_live(cfg: dict) -> bool:
    return zscaler_target(cfg.get("zscaler") or {}) == "live"


def live_max_mode(cfg: dict) -> Optional[str]:
    """`deploy.live_max_mode`: the most a region may do when the ZIA target is live (absent = no cap)."""
    cap = (cfg.get("deploy") or {}).get("live_max_mode")
    if cap and cap not in MODES:
        raise ValueError(f"deploy.live_max_mode: unknown mode '{cap}'")
    return cap or None


def region_mode(cfg: dict, code: str) -> str:
    mode = (cfg.get("regions") or {}).get(code, {}).get("mode") or cfg["deploy"].get("default_mode", "stage")
    if mode not in MODES:
        raise ValueError(f"region {code}: unknown mode '{mode}'")
    cap = live_max_mode(cfg)
    if cap and is_live(cfg) and MODE_RANK[mode] > MODE_RANK[cap]:
        log.debug("region %s: mode %s capped to %s (deploy.live_max_mode, live ZIA target)", code, mode, cap)
        return cap
    return mode


def bootstrap_mode(cfg: dict) -> str:
    mode = (cfg.get("zscaler") or {}).get("bootstrap") or "off"
    if mode not in BOOTSTRAP_MODES:
        raise ValueError(f"zscaler.bootstrap must be one of {', '.join(BOOTSTRAP_MODES)}, not '{mode}'")
    return mode


def auto_bootstrap_refusal(cfg: dict) -> Optional[str]:
    """Why a run must not create a missing PAC file itself, or None when it may.

    A bootstrap creates a DEPLOYED version, so on a live tenant capped by deploy.live_max_mode the run refuses and
    asks for the explicit `pac-sync bootstrap --region X --yes`.
    """
    if bootstrap_mode(cfg) != "missing":
        return "zscaler.bootstrap is off"
    if is_live(cfg) and live_max_mode(cfg) in ("stage", "paused"):
        return f"deploy.live_max_mode is {live_max_mode(cfg)} on a live ZIA target"
    return None


def make_history(cfg: dict, push: Optional[bool] = None) -> GitHistory:
    hcfg = cfg["history"]
    return GitHistory(hcfg["repo_path"], hcfg.get("author_name", "PAC Sync"), hcfg.get("author_email", "pac-sync@example.com"),
                      hcfg.get("remote", "origin"), bool(hcfg.get("push")) if push is None else push)


def make_renderer(cfg: dict) -> PacRenderer:
    return PacRenderer(cfg["render"]["template"], cfg["render"].get("direct_plain_hostnames", True),
                       cfg["render"].get("direct_networks") or [])


@contextmanager
def run_lock(repo_path: str, stale_after: int = 7200):
    """Prevent two runs from overlapping (e.g. a slow run and the next scheduled one)."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    lock = Path(repo_path) / ".pac-sync.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < stale_after:
        raise RuntimeError(f"another run holds {lock}; delete it if that run is dead")
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def notify(cfg: dict, text: str) -> None:
    url = os.environ.get((cfg.get("notify") or {}).get("webhook_url_env", ""), "")
    if not url:
        return
    try:
        requests.post(url, json={"text": text}, timeout=10)
    except requests.RequestException as exc:  # a failed notification must not fail the run
        log.warning("notification failed: %s", exc)


def load_source_data(source):
    profiles = {
        p["sys_id"]: ProxyProfile(p["sys_id"], p["u_name"], p["u_proxy_chain"].strip())
        for p in source.fetch_profiles()
    }
    regions: Dict[str, Region] = {}
    for r in source.fetch_regions():
        profile = profiles.get(r.get("u_default_profile", ""))
        if not profile:
            log.error("region %s has no active default proxy profile: skipped", r.get("u_code"))
            continue
        regions[r["sys_id"]] = Region(r["sys_id"], r["u_code"], r["u_zia_pac_name"], profile.chain)
    return regions, profiles, source.fetch_rules()


def build_plans(cfg, regions, result: NormalizeResult, history: GitHistory, renderer: PacRenderer, force: bool):
    guard = cfg.get("guardrails") or {}
    max_ratio = float(guard.get("max_removed_ratio", 0.2))
    min_prev = int(guard.get("min_previous_rules_for_ratio", 10))
    plans = []
    for region in sorted(regions.values(), key=lambda r: r.code):
        rules = result.rules_by_region.get(region.code, [])
        content, digest = renderer.render(region, rules)
        prev = {r["number"] for r in history.read_rules(region.code)}
        new = {r.number for r in rules}
        plan = RegionPlan(region, content, digest, sorted(new - prev), sorted(prev - new))
        if not force and len(prev) >= min_prev and len(plan.removed) / len(prev) > max_ratio:
            plan.blocked = (
                f"{len(plan.removed)} of {len(prev)} rules would be removed (limit {max_ratio:.0%}); "
                "rerun with --force if this is intended"
            )
            log.error("region %s blocked: %s", region.code, plan.blocked)
        else:
            history.write_region(region.code, content, [r.to_json() for r in rules])
        plans.append(plan)
    return plans


def commit_message(plans: List[RegionPlan], result: NormalizeResult) -> str:
    lines = []
    for p in plans:
        if p.added or p.removed:
            lines.append(f"{p.region.code}: +{len(p.added)} -{len(p.removed)}")
    title = "PAC update: " + (", ".join(lines) if lines else "re-render")
    body = []
    for p in plans:
        if p.added:
            body.append(f"{p.region.code} added: {', '.join(p.added)}")
        if p.removed:
            body.append(f"{p.region.code} removed: {', '.join(p.removed)}")
    if result.rejected:
        body.append("Rejected: " + ", ".join(f"{r.get('number')} ({why})" for r, why in result.rejected))
    return title + ("\n\n" + "\n".join(body) if body else "")


def sync(cfg: dict, dry_run: bool = False, force: bool = False, promote: Optional[str] = None) -> int:
    """One full run. Returns the process exit code."""
    hcfg = cfg["history"]
    history = make_history(cfg, push=bool(hcfg.get("push")) and not dry_run)
    renderer = make_renderer(cfg)
    source = make_source(cfg["servicenow"])
    now = datetime.now(timezone.utc)

    with run_lock(hcfg["repo_path"]):
        history.ensure()
        regions, profiles, raw_rules = load_source_data(source)
        if not raw_rules and any(history.read_rules(r.code) for r in regions.values()):
            msg = "ServiceNow returned 0 rules but the PAC history has rules: run aborted"
            log.error(msg)
            notify(cfg, f"PAC Sync: {msg}")
            return 2
        result = normalize(raw_rules, regions, profiles, now)
        for rec, why in result.rejected:
            log.warning("rejected %s: %s", rec.get("number"), why)

        plans = build_plans(cfg, regions, result, history, renderer, force)
        message = commit_message(plans, result)

        if dry_run:
            log.info("[dry-run] %s\n%s", message, history.diff_stat() or "no change")
            history.discard()
            return 0

        content_commit = history.commit(message) or history.head()
        states = {p.region.code: history.read_state(p.region.code) for p in plans}
        modes = {p.region.code: region_mode(cfg, p.region.code) for p in plans}
        if promote:
            modes = {code: ("auto" if code == promote else "paused") for code in modes}

        client = deployer = None
        if any(m != "paused" for m in modes.values()):
            client = make_zscaler_client(cfg["zscaler"])
            deployer = ZiaDeployer(client)
        try:
            _stage_and_deploy(cfg, plans, states, modes, deployer, content_commit, message, now, bool(promote))
        finally:
            close_zscaler_client(client)

        # Record ZIA state in git, tag deployments, write back to ServiceNow.
        for p in plans:
            st = states[p.region.code]
            st["last_error"] = p.error
            history.write_state(p.region.code, st)
        if history.commit("Record ZIA state\n\n" + "\n".join(_summary(p) for p in plans)):
            for p in plans:
                if p.deployed_now:
                    v = states[p.region.code]["deployed"]["version"]
                    history.tag(f"{p.region.code}-v{v}", f"{p.region.code} ZIA version {v} ({content_commit[:10]})")
        push_error = _push(history)

        _write_back(cfg, source, result, raw_rules, states, plans, content_commit, now)

        failures = [p for p in plans if p.error or p.blocked]
        changes = [p for p in plans if p.staged_now or p.deployed_now]
        if failures or changes or result.rejected or push_error:
            notify(cfg, "PAC Sync\n" + "\n".join(_summary(p) for p in plans)
                   + (f"\nRejected rules: {len(result.rejected)}" if result.rejected else "")
                   + (f"\nGit push failed: {push_error}" if push_error else ""))
        for p in plans:
            log.info(_summary(p))
        return 1 if failures or push_error else 0


def _push(history: GitHistory) -> Optional[str]:
    """Push commits and tags. A failure is reported, never fatal: ZIA state is already recorded locally."""
    try:
        history.push()
    except Exception as exc:
        log.error("git push failed (commits stay local and go out with the next successful push): %s", exc)
        return str(exc)
    return None


def _stage_and_deploy(cfg, plans, states, modes, deployer, content_commit, message, now, promote: bool) -> None:
    activate = cfg["deploy"].get("activate", True)
    domain = (cfg.get("zscaler") or {}).get("pac_domain") or ""
    refusal = auto_bootstrap_refusal(cfg)
    stamp = now.isoformat(timespec="seconds")

    # Phase 1: stage every region whose current render is neither staged nor deployed.
    for p in plans:
        code, st = p.region.code, states[p.region.code]
        if p.blocked or modes[code] == "paused" or st.get("paused_reason"):
            continue
        if st.get("deployed", {}).get("hash") == p.hash or st.get("staged", {}).get("hash") == p.hash:
            continue
        if promote:
            p.error = "content changed since it was staged; review the new render before promoting"
            continue
        short = f"PAC Sync {content_commit[:10]} {message.splitlines()[0]}"
        try:
            if not st.get("pac_id"):
                try:
                    st["pac_id"] = deployer.find_pac_id(p.region.zia_pac_name)
                except PacFileNotFound as exc:
                    if refusal:
                        raise PacFileNotFound(f"{exc} (no automatic bootstrap: {refusal})") from exc
                    # First version of a new PAC file: created DEPLOYED with the current render.
                    st["pac_id"] = deployer.bootstrap(p.region.zia_pac_name, p.content, domain, short)
                    version = deployer.deployed_version(st["pac_id"]) or 1
                    st["deployed"] = {"version": version, "hash": p.hash, "commit": content_commit, "at": stamp}
                    st.pop("staged", None)
                    p.deployed_now = p.bootstrapped = True
                    continue
            version = deployer.stage(st["pac_id"], p.content, short)
            st["staged"] = {"version": version, "hash": p.hash, "commit": content_commit, "at": stamp}
            p.staged_now = True
        except Exception as exc:  # one region failing must not stop the others
            p.error = f"stage failed: {exc}"
    if activate and any(p.staged_now for p in plans):
        deployer.activate()

    # Phase 2: deploy staged versions of regions in auto mode.
    for p in plans:
        code, st = p.region.code, states[p.region.code]
        staged = st.get("staged", {})
        if p.error or p.bootstrapped or modes[code] != "auto" or st.get("paused_reason") or staged.get("hash") != p.hash:
            continue
        try:
            deployer.deploy(st["pac_id"], staged["version"])
            st["deployed"] = dict(staged, at=stamp)
            st.pop("staged", None)
            p.deployed_now = True
        except Exception as exc:
            p.error = f"deploy failed: {exc}"
    if activate and any(p.deployed_now for p in plans):
        deployer.activate()


def _summary(p: RegionPlan) -> str:
    what = "deployed" if p.deployed_now else "staged" if p.staged_now else "unchanged"
    if p.bootstrapped:
        what = "bootstrapped (PAC file created in ZIA, first version deployed)"
    if p.blocked:
        what = f"BLOCKED: {p.blocked}"
    if p.error:
        what = f"FAILED: {p.error}"
    return f"{p.region.code}: {what} (+{len(p.added)} -{len(p.removed)}, sha256 {p.hash[:12]})"


def _write_back(cfg, source, result, raw_rules, states, plans, commit, now) -> None:
    if not cfg["servicenow"].get("write_back", True):
        return
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    hashes = {p.region.code: p.hash for p in plans if not p.blocked}
    raw_by_id = {r["sys_id"]: r for r in raw_rules}
    for sys_id, body in compute_rule_updates(result, raw_by_id, states, hashes, now_str):
        try:
            source.update_rule(sys_id, body)
        except Exception as exc:
            log.error("write-back to %s failed: %s", sys_id, exc)
    for p in plans:
        if not (p.staged_now or p.deployed_now or p.error):
            continue
        st = states[p.region.code]
        version = (st.get("deployed") if p.deployed_now else st.get("staged")) or {}
        try:
            source.log_deployment({
                "u_region": p.region.sys_id,
                "u_zia_version": version.get("version"),
                "u_status": "failed" if p.error else "deployed" if p.deployed_now else "staged",
                "u_git_commit": commit,
                "u_content_hash": p.hash,
                "u_rules_added": len(p.added),
                "u_rules_removed": len(p.removed),
                "u_summary": (_summary(p) + "\nAdded: " + ", ".join(p.added) + "\nRemoved: " + ", ".join(p.removed))[:4000],
                "u_started": now_str,
                "u_finished": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            log.error("deployment log for %s failed: %s", p.region.code, exc)


def _render_region(cfg: dict, code: str):
    """Load ServiceNow and render one region. Return (region, rules, content, sha256) or None if unknown."""
    source = make_source(cfg["servicenow"])
    regions, profiles, raw_rules = load_source_data(source)
    region = next((r for r in regions.values() if r.code == code), None)
    if region is None:
        return None
    result = normalize(raw_rules, regions, profiles)
    rules = result.rules_by_region.get(code, [])
    content, digest = make_renderer(cfg).render(region, rules)
    return region, rules, content, digest


def render_only(cfg: dict, code: str) -> int:
    rendered = _render_region(cfg, code)
    if rendered is None:
        log.error("unknown or inactive region %s", code)
        return 2
    sys.stdout.write(rendered[2])
    return 0


def rollback(cfg: dict, code: str, version: int) -> int:
    hcfg = cfg["history"]
    history = make_history(cfg)
    with run_lock(hcfg["repo_path"]):
        st = history.read_state(code)
        if not st.get("pac_id"):
            log.error("no ZIA state recorded for region %s", code)
            return 2
        client = make_zscaler_client(cfg["zscaler"])
        try:
            deployer = ZiaDeployer(client)
            deployer.deploy(st["pac_id"], version)
            deployer.activate()
        finally:
            close_zscaler_client(client)
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        st["deployed"] = {"version": version, "hash": None, "commit": None, "at": when}
        # Without the pause, the next scheduled run would stage (and in auto mode deploy) the bad render again.
        st["paused_reason"] = f"rollback to v{version} at {when}; fix ServiceNow, then `pac-sync resume --region {code}`"
        history.write_state(code, st)
        history.commit(f"Rollback {code} to ZIA version {version}")
        history.tag(f"{code}-v{version}-rollback", f"Rollback {code} to ZIA version {version}")
        history.push()
    notify(cfg, f"PAC Sync: {code} rolled back to ZIA version {version}; region paused")
    return 0


def resume(cfg: dict, code: str) -> int:
    hcfg = cfg["history"]
    history = GitHistory(hcfg["repo_path"], hcfg["author_name"], hcfg["author_email"])
    st = history.read_state(code)
    st.pop("paused_reason", None)
    history.write_state(code, st)
    history.commit(f"Resume {code}")
    return 0


def status(cfg: dict, out: Callable[[str], None] = print) -> int:
    """Print each region's recorded ZIA state from the history repo. Offline: no ServiceNow, no ZIA."""
    history = make_history(cfg, push=False)
    regions_dir = history.root / "regions"
    codes = sorted(d.name for d in regions_dir.iterdir() if d.is_dir()) if regions_dir.is_dir() else []
    target = zscaler_target(cfg.get("zscaler") or {})
    out(f"History {history.root} (ZIA target: {target})")
    if not codes:
        out("No region recorded yet: run `pac-sync run` first.")
        return 0

    def version(v: Optional[dict]) -> str:
        if not v:
            return "-"
        commit = (v.get("commit") or "")[:10] or "-"
        digest = (v.get("hash") or "")[:12] or "-"
        return f"v{v.get('version')} (sha256 {digest}, commit {commit}, {v.get('at') or '-'})"

    for code in codes:
        st = history.read_state(code)
        mode = region_mode(cfg, code)
        out(f"{code}: mode {mode}, pac_id {st.get('pac_id') or '-'}")
        out(f"  deployed: {version(st.get('deployed'))}")
        out(f"  staged:   {version(st.get('staged'))}")
        out(f"  last_error: {st.get('last_error') or '-'}")
        out(f"  paused_reason: {st.get('paused_reason') or '-'}")
    return 0


def bootstrap_region(cfg: dict, code: str, yes: bool = False, confirm: Callable[[str], str] = input) -> int:
    """Create the region's PAC file in ZIA with the current render as its first, DEPLOYED version."""
    rendered = _render_region(cfg, code)
    if rendered is None:
        log.error("unknown or inactive region %s", code)
        return 2
    region, rules, content, digest = rendered
    zcfg = cfg.get("zscaler") or {}
    if is_live(cfg) and not yes:
        prompt = (f"Create PAC file '{region.zia_pac_name}' in the LIVE ZIA tenant and deploy it now? "
                  f"Type the region code ({code}) to confirm: ")
        try:
            answer = confirm(prompt) if sys.stdin is not None and sys.stdin.isatty() else ""
        except EOFError:
            answer = ""
        if answer.strip() != code:
            log.error("bootstrap of %s refused: live ZIA target needs --yes or an interactive confirmation", code)
            return 2
    domain = zcfg.get("pac_domain") or ""
    if not domain:
        log.error("zscaler.pac_domain is not set: ZIA needs a domain to create a PAC file")
        return 2

    hcfg = cfg["history"]
    history = make_history(cfg)
    now = datetime.now(timezone.utc)
    with run_lock(hcfg["repo_path"]):
        history.ensure()
        st = history.read_state(code)
        client = make_zscaler_client(zcfg)
        try:
            deployer = ZiaDeployer(client)
            try:
                existing = deployer.find_pac_id(region.zia_pac_name)
            except PacFileNotFound:
                existing = None
            if existing is not None:
                log.error("ZIA already has a PAC file named '%s' (id %s): nothing to bootstrap", region.zia_pac_name, existing)
                return 1
            history.write_region(code, content, [r.to_json() for r in rules])
            content_commit = history.commit(f"Bootstrap {code}: render for new ZIA PAC file {region.zia_pac_name}") \
                or history.head()
            pac_id = deployer.bootstrap(region.zia_pac_name, content, domain,
                                        f"PAC Sync {content_commit[:10]} bootstrap {code}")
            if cfg["deploy"].get("activate", True):
                deployer.activate()
            version = deployer.deployed_version(pac_id) or 1
        finally:
            close_zscaler_client(client)
        st.update(pac_id=pac_id, last_error=None,
                  deployed={"version": version, "hash": digest, "commit": content_commit,
                            "at": now.isoformat(timespec="seconds")})
        st.pop("staged", None)
        history.write_state(code, st)
        history.commit(f"Bootstrap {code}: ZIA PAC file {region.zia_pac_name} id {pac_id} version {version}")
        history.tag(f"{code}-v{version}", f"{code} ZIA version {version} (bootstrap, {content_commit[:10]})")
        push_error = _push(history)
    log.info("%s: PAC file '%s' created (id %s), version %s deployed", code, region.zia_pac_name, pac_id, version)
    notify(cfg, f"PAC Sync: {code} bootstrapped, ZIA PAC file {region.zia_pac_name} version {version}"
           + (f"\nGit push failed: {push_error}" if push_error else ""))
    return 1 if push_error else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pac-sync")
    parser.add_argument("--config", default=os.environ.get("PAC_SYNC_CONFIG", "config/config.yaml"))
    parser.add_argument("--env-file", default=".env", help="credentials file loaded before the config (default .env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true", help="render and diff only: no commit, no ZIA, no write-back")
    p_run.add_argument("--force", action="store_true", help="bypass the removed-rules guardrail")
    for name in ("render", "promote", "resume"):
        sub.add_parser(name).add_argument("--region", required=True)
    p_rb = sub.add_parser("rollback")
    p_rb.add_argument("--region", required=True)
    p_rb.add_argument("--version", required=True, type=int)
    sub.add_parser("status", help="each region's recorded ZIA state (offline)")
    p_bs = sub.add_parser("bootstrap", help="create the region's PAC file in ZIA (first version deployed)")
    p_bs.add_argument("--region", required=True)
    p_bs.add_argument("--yes", action="store_true", help="do not ask for confirmation on a live ZIA target")
    sub.add_parser("doctor", help="read-only checks; exit 1 if any check fails")
    p_setup = sub.add_parser("setup", help="write config/config.yaml and .env")
    p_setup.add_argument("--profile", choices=("mock", "custom"))
    p_setup.add_argument("--non-interactive", action="store_true", help="use the profile defaults, ask nothing")
    p_setup.add_argument("--force", action="store_true", help="overwrite an existing config and .env keys")
    p_setup.add_argument("--config-out", help="config file to write (default: --config)")
    p_setup.add_argument("--env-out", help="env file to write (default: --env-file)")
    p_setup.add_argument("--no-doctor", action="store_true", help="do not run doctor at the end")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if args.command == "setup":
        from .setup_wizard import run_setup

        return run_setup(profile=args.profile, non_interactive=args.non_interactive, force=args.force,
                         config_out=args.config_out or args.config, env_out=args.env_out or args.env_file,
                         doctor=not args.no_doctor)

    if Path(args.env_file).is_file():
        loaded = load_env_file(args.env_file)
        log.debug("loaded %d variable(s) from %s: %s", len(loaded), args.env_file, ", ".join(loaded) or "-")
    elif args.env_file != ".env":
        log.warning("env file %s not found", args.env_file)
    if args.command == "doctor":
        from .doctor import run_doctor

        return run_doctor(args.config, args.env_file)

    cfg = load_config(args.config)
    try:
        if args.command == "run":
            return sync(cfg, dry_run=args.dry_run, force=args.force)
        if args.command == "promote":
            return sync(cfg, promote=args.region)
        if args.command == "render":
            return render_only(cfg, args.region)
        if args.command == "rollback":
            return rollback(cfg, args.region, args.version)
        if args.command == "status":
            return status(cfg)
        if args.command == "bootstrap":
            return bootstrap_region(cfg, args.region, yes=args.yes)
        return resume(cfg, args.region)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        notify(cfg, f"PAC Sync failed: {exc}")
        return 3
    except Exception as exc:
        log.exception("PAC Sync failed")
        notify(cfg, f"PAC Sync failed: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
