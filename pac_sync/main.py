"""PAC Sync entry point.

    pac-sync run      [--config FILE] [--dry-run] [--force]   scheduled job
    pac-sync render   --region EMEA                            print the PAC, no side effects
    pac-sync promote  --region EMEA                            deploy the reviewed STAGE version
    pac-sync rollback --region EMEA --version 41               redeploy an older ZIA version, pause the region
    pac-sync resume   --region EMEA                            clear a rollback pause
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
from typing import Dict, List, Optional

import requests
import yaml

from .deployer import ZiaDeployer, make_zscaler_client
from .history import GitHistory
from .models import ProxyProfile, Region
from .normalizer import NormalizeResult, normalize
from .renderer import PacRenderer
from .snow_client import make_source
from .writeback import compute_rule_updates

log = logging.getLogger("pac_sync")
MODES = ("stage", "auto", "paused")


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
    error: Optional[str] = None


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def region_mode(cfg: dict, code: str) -> str:
    mode = (cfg.get("regions") or {}).get(code, {}).get("mode") or cfg["deploy"].get("default_mode", "stage")
    if mode not in MODES:
        raise ValueError(f"region {code}: unknown mode '{mode}'")
    return mode


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
    history = GitHistory(hcfg["repo_path"], hcfg["author_name"], hcfg["author_email"], hcfg.get("remote", "origin"),
                         bool(hcfg.get("push")) and not dry_run)
    renderer = PacRenderer(cfg["render"]["template"], cfg["render"].get("direct_plain_hostnames", True),
                           cfg["render"].get("direct_networks") or [])
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

        deployer = None
        if any(m != "paused" for m in modes.values()):
            deployer = ZiaDeployer(make_zscaler_client(cfg["zscaler"]))
        activate = cfg["deploy"].get("activate", True)

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
            try:
                st["pac_id"] = st.get("pac_id") or deployer.find_pac_id(p.region.zia_pac_name)
                short = f"PAC Sync {content_commit[:10]} {message.splitlines()[0]}"
                version = deployer.stage(st["pac_id"], p.content, short)
                st["staged"] = {"version": version, "hash": p.hash, "commit": content_commit,
                                "at": now.isoformat(timespec="seconds")}
                p.staged_now = True
            except Exception as exc:  # one region failing must not stop the others
                p.error = f"stage failed: {exc}"
        if activate and any(p.staged_now for p in plans):
            deployer.activate()

        # Phase 2: deploy staged versions of regions in auto mode.
        for p in plans:
            code, st = p.region.code, states[p.region.code]
            staged = st.get("staged", {})
            if p.error or modes[code] != "auto" or st.get("paused_reason") or staged.get("hash") != p.hash:
                continue
            try:
                deployer.deploy(st["pac_id"], staged["version"])
                st["deployed"] = dict(staged, at=now.isoformat(timespec="seconds"))
                st.pop("staged", None)
                p.deployed_now = True
            except Exception as exc:
                p.error = f"deploy failed: {exc}"
        if activate and any(p.deployed_now for p in plans):
            deployer.activate()

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
        history.push()

        _write_back(cfg, source, result, raw_rules, states, plans, content_commit, now)

        failures = [p for p in plans if p.error or p.blocked]
        changes = [p for p in plans if p.staged_now or p.deployed_now]
        if failures or changes or result.rejected:
            notify(cfg, "PAC Sync\n" + "\n".join(_summary(p) for p in plans)
                   + (f"\nRejected rules: {len(result.rejected)}" if result.rejected else ""))
        for p in plans:
            log.info(_summary(p))
        return 1 if failures else 0


def _summary(p: RegionPlan) -> str:
    what = "deployed" if p.deployed_now else "staged" if p.staged_now else "unchanged"
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


def render_only(cfg: dict, code: str) -> int:
    source = make_source(cfg["servicenow"])
    regions, profiles, raw_rules = load_source_data(source)
    region = next((r for r in regions.values() if r.code == code), None)
    if region is None:
        log.error("unknown or inactive region %s", code)
        return 2
    result = normalize(raw_rules, regions, profiles)
    renderer = PacRenderer(cfg["render"]["template"], cfg["render"].get("direct_plain_hostnames", True),
                           cfg["render"].get("direct_networks") or [])
    content, _ = renderer.render(region, result.rules_by_region[code])
    sys.stdout.write(content)
    return 0


def rollback(cfg: dict, code: str, version: int) -> int:
    hcfg = cfg["history"]
    history = GitHistory(hcfg["repo_path"], hcfg["author_name"], hcfg["author_email"], hcfg.get("remote", "origin"),
                         bool(hcfg.get("push")))
    with run_lock(hcfg["repo_path"]):
        st = history.read_state(code)
        if not st.get("pac_id"):
            log.error("no ZIA state recorded for region %s", code)
            return 2
        deployer = ZiaDeployer(make_zscaler_client(cfg["zscaler"]))
        deployer.deploy(st["pac_id"], version)
        deployer.activate()
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pac-sync")
    parser.add_argument("--config", default=os.environ.get("PAC_SYNC_CONFIG", "config/config.yaml"))
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
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
        return resume(cfg, args.region)
    except Exception as exc:
        log.exception("PAC Sync failed")
        notify(cfg, f"PAC Sync failed: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
