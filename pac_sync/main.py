"""PAC Sync entry point.

    pac-sync run      [--config FILE] [--dry-run] [--force]   scheduled job
    pac-sync render   --region EMEA                            print the PAC, no side effects
    pac-sync promote  --region EMEA                            deploy the reviewed STAGE version
    pac-sync rollback --region EMEA (--version 41 | --to lkg)  redeploy an older ZIA version, pause the region
    pac-sync resume   --region EMEA                            clear a pause (rollback, manual change in ZIA)
    pac-sync status                                            each region's recorded ZIA state
    pac-sync versions --region EMEA                            the ZIA versions of the region's PAC file
    pac-sync diff     --region EMEA --from deployed --to render   unified diff of two versions
    pac-sync lkg      --region EMEA (--version N | --remove [--new-lkg N])
    pac-sync bootstrap --region EMEA [--yes]                   create the region's PAC file in ZIA
    pac-sync demo-cleanup --yes                                delete the prefixed demo PAC files
    pac-sync doctor   [--write-probe --region EMEA]            read-only checks of config, credentials, ServiceNow, git, ZIA
    pac-sync setup    [--profile mock|demo|custom]             write config/config.yaml and .env

Global option --env-file loads credentials before the config is read (default: .env of the config's project, else
.env in the current directory); the process environment wins.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
import yaml

from .deployer import (PacFileNotFound, ZiaDeployer, close_zscaler_client, effective_pac_name, make_zscaler_client,
                       validate_prefix_config, zscaler_target)
from .envfile import ConfigError, default_env_file, load_env_file, stdin_is_interactive
from .history import GitHistory
from .models import ProxyProfile, Region
from .normalizer import NormalizeResult, normalize
from .renderer import PacRenderer
from .snow_client import make_source
from .writeback import NOT_ACTIVATED, compute_rule_updates
from .zia_history import ZiaHistory
from .zia_messages import build_message, parse_commit_message, run_id

log = logging.getLogger("pac_sync")
MODES = ("stage", "auto", "paused")
MODE_RANK = {"paused": 0, "stage": 1, "auto": 2}
BOOTSTRAP_MODES = ("off", "missing")
HISTORY_MODES = ("git", "both", "zia")
DRIFT = "manual change in ZIA"


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
    not_activated: bool = False
    confirmed: bool = False  # an earlier run's unactivated versions were found active (or activated) in this run
    error: Optional[str] = None
    note: Optional[str] = None
    zia_message: str = ""
    prev_version: Optional[int] = None


def load_config(path: str) -> dict:
    """Read the config. In history mode zia the state file (and so the run lock beside it) and archive_dir are resolved
    against the project that owns the config (the parent of its folder), like the .env file, not the current directory."""
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    hist = (cfg or {}).get("history")
    if isinstance(hist, dict) and hist.get("mode") == "zia":
        root = Path(path).resolve().parent.parent
        for key, default in (("state_file", "pac-state.json"), ("archive_dir", None)):
            value = hist.get(key) or default
            if value:
                hist[key] = str(root / value) if not Path(value).is_absolute() else value
    return cfg


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


def history_mode(cfg: dict) -> str:
    mode = (cfg.get("history") or {}).get("mode") or "git"
    if mode not in HISTORY_MODES:
        raise ConfigError(f"history.mode must be one of {', '.join(HISTORY_MODES)}, not '{mode}'")
    return mode


def skip_activate_if_pending(cfg: dict) -> bool:
    return bool((cfg.get("deploy") or {}).get("skip_activate_if_pending"))


def auto_bootstrap_refusal(cfg: dict) -> Optional[str]:
    """Why a run must not create a missing PAC file itself, or None when it may.

    A bootstrap creates a DEPLOYED version, so on a live tenant capped by deploy.live_max_mode the run refuses and
    asks for the explicit `pac-sync bootstrap --region X --yes`. Prefix-guarded files (only_prefixed with a prefix)
    can be created automatically: no ZCC profile references them. Regions that are not in auto mode are refused
    separately, in _stage_and_deploy.
    """
    if bootstrap_mode(cfg) != "missing":
        return "zscaler.bootstrap is off"
    z = cfg.get("zscaler") or {}
    if z.get("only_prefixed") and z.get("pac_name_prefix"):
        return None
    if is_live(cfg) and live_max_mode(cfg) in ("stage", "paused"):
        return f"deploy.live_max_mode is {live_max_mode(cfg)} on a live ZIA target"
    return None


def make_deployer(cfg: dict, client, on_evict: Optional[Callable] = None) -> ZiaDeployer:
    deployer = ZiaDeployer(client)
    z = cfg.get("zscaler") or {}
    deployer.prefix = z.get("pac_name_prefix") or ""
    deployer.only_prefixed = bool(z.get("only_prefixed"))
    deployer.on_evict = on_evict
    return deployer


def lazy_names(cfg: dict) -> Callable[[str], str]:
    """code -> effective PAC name, read from ServiceNow on first use (for commands that start without a run)."""
    cache: Dict[str, str] = {}

    def name_for(code: str) -> str:
        if not cache:
            regions, _, _ = load_source_data(make_source(cfg["servicenow"]))
            cache.update({r.code: effective_pac_name(cfg.get("zscaler") or {}, r.zia_pac_name) for r in regions.values()})
        if code not in cache:
            raise ConfigError(f"unknown or inactive region {code}")
        return cache[code]

    return name_for


def make_history(cfg: dict, push: Optional[bool] = None, deployer: Optional[ZiaDeployer] = None,
                 name_for: Optional[Callable[[str], str]] = None):
    hcfg = cfg["history"]
    if history_mode(cfg) == "zia":
        return ZiaHistory(deployer, hcfg.get("state_file") or "./pac-state.json", name_for or lazy_names(cfg),
                          hcfg.get("archive_dir"), list((cfg.get("regions") or {}).keys()))
    return GitHistory(hcfg["repo_path"], hcfg.get("author_name", "PAC Sync"), hcfg.get("author_email", "pac-sync@example.com"),
                      hcfg.get("remote", "origin"), bool(hcfg.get("push")) if push is None else push)


def evict_hook(cfg: dict, history, name_to_code: Dict[str, str]) -> Optional[Callable]:
    """Archive a version before ZIA deletes it: `both` -> regions/<CODE>/zia-archive/, `zia` -> history.archive_dir."""
    if history_mode(cfg) == "git":
        return None

    def archive(version) -> None:
        code = name_to_code.get(getattr(version, "name", ""))
        if code:
            history.archive_version(code, version)

    return archive


@contextmanager
def zia_session(cfg: dict, need_zia: bool = True, push: Optional[bool] = None):
    """History backend plus (when needed, always for mode zia) a ZIA deployer; the client is closed on exit."""
    client = deployer = None
    name_for = lazy_names(cfg)
    try:
        if need_zia or history_mode(cfg) == "zia":
            client = make_zscaler_client(cfg["zscaler"])
            deployer = make_deployer(cfg, client)
        history = make_history(cfg, push, deployer, name_for)
        yield history, deployer, name_for
    finally:
        close_zscaler_client(client)


def make_renderer(cfg: dict) -> PacRenderer:
    return PacRenderer(cfg["render"]["template"], cfg["render"].get("direct_plain_hostnames", True),
                       cfg["render"].get("direct_networks") or [])


@contextmanager
def run_lock(lock_dir, stale_after: int = 7200):
    """Prevent two runs from overlapping. The lock file is created atomically (O_EXCL) and holds pid@host; it is
    removed on exit only if it is still ours."""
    folder = Path(lock_dir)
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".pac-sync.lock"
    token = f"{os.getpid()}@{socket.gethostname()}"
    for attempt in (1, 2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                dead = time.time() - lock.stat().st_mtime >= stale_after
            except FileNotFoundError:  # released between the two calls: try again
                continue
            if attempt == 2 or not dead:
                raise RuntimeError(f"another run holds {lock}; delete it if that run is dead")
            lock.unlink(missing_ok=True)  # stale: drop it and retry once
    else:
        raise RuntimeError(f"cannot take {lock}")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    try:
        yield
    finally:
        try:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


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


def build_plans(cfg, regions, result: NormalizeResult, history, renderer: PacRenderer, force: bool):
    guard = cfg.get("guardrails") or {}
    max_ratio = float(guard.get("max_removed_ratio", 0.2))
    min_prev = int(guard.get("min_previous_rules_for_ratio", 10))
    plans = []
    for region in sorted(regions.values(), key=lambda r: r.code):
        rules = result.rules_by_region.get(region.code, [])
        content, digest = renderer.render(region, rules)
        digest = history.plan_hash(content, digest)
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
        body.append("Rejected: " + ", ".join(f"{str(r.get('number'))[:40]!r} ({why})" for r, why in result.rejected))
    return title + ("\n\n" + "\n".join(body) if body else "")


def sync(cfg: dict, dry_run: bool = False, force: bool = False, promote: Optional[str] = None) -> int:
    """One full run. Returns the process exit code."""
    zia_mode = history_mode(cfg) == "zia"
    source = make_source(cfg["servicenow"])
    renderer = make_renderer(cfg)
    now = datetime.now(timezone.utc)
    rid = run_id(now)
    names: Dict[str, str] = {}  # code -> effective PAC name
    name_to_code: Dict[str, str] = {}
    client = deployer = None
    try:
        if zia_mode:  # the history IS the ZIA versions: the client is needed from the start
            client = make_zscaler_client(cfg["zscaler"])
            deployer = make_deployer(cfg, client)
        history = make_history(cfg, push=bool(cfg["history"].get("push")) and not dry_run, deployer=deployer,
                               name_for=lambda code: names[code])

        with run_lock(history.root):
            history.ensure()
            regions, profiles, raw_rules = load_source_data(source)
            for r in regions.values():
                names[r.code] = effective_pac_name(cfg.get("zscaler") or {}, r.zia_pac_name)
                name_to_code[names[r.code]] = r.code
            if not raw_rules and any(history.read_rules(r.code) for r in regions.values()):
                msg = "ServiceNow returned 0 rules but the PAC history has rules: run aborted"
                log.error(msg)
                notify(cfg, f"PAC Sync: {msg}")
                return 2
            result = normalize(raw_rules, regions, profiles, now)
            for rec, why in result.rejected:
                log.warning("rejected %r: %s", rec.get("number"), why)

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

            if deployer is None and any(m != "paused" for m in modes.values()):
                client = make_zscaler_client(cfg["zscaler"])
                deployer = make_deployer(cfg, client)
            if deployer is not None:
                deployer.on_evict = evict_hook(cfg, history, name_to_code)
            skipped = _stage_and_deploy(cfg, plans, states, modes, deployer, content_commit, rid, bool(promote), force,
                                        names)

            # Record ZIA state, tag deployments, write back to ServiceNow.
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

            # Still unconfirmed after this run: the mark survives until ZIA is active again (or someone activates).
            not_activated_regions = {c for c, st in states.items() if st.get("not_activated")}
            not_activated = bool(not_activated_regions)
            _write_back(cfg, source, result, raw_rules, states, plans, content_commit, now, not_activated_regions)

            failures = [p for p in plans if p.error or p.blocked]
            changes = [p for p in plans if p.staged_now or p.deployed_now]
            if failures or changes or result.rejected or push_error or not_activated:
                notify(cfg, "PAC Sync\n" + "\n".join(_summary(p) for p in plans)
                       + (f"\nRejected rules: {len(result.rejected)}" if result.rejected else "")
                       + (f"\n{NOT_ACTIVATED}" if not_activated else "")
                       + (f"\nGit push failed: {push_error}" if push_error else ""))
            for p in plans:
                log.info(_summary(p))
            if not_activated:
                log.warning(NOT_ACTIVATED)
            return 1 if failures or push_error or not_activated else 0
    finally:
        close_zscaler_client(client)


def _push(history) -> Optional[str]:
    """Push commits and tags. A failure is reported, never fatal: ZIA state is already recorded locally."""
    try:
        history.push()
    except Exception as exc:
        log.error("git push failed (commits stay local and go out with the next successful push): %s", exc)
        return str(exc)
    return None


def drift_reason(deployer, st: dict, history_is_zia: bool) -> Optional[str]:
    """A change made in the ZIA portal: the DEPLOYED version's message is not PAC Sync's, or (git / both) it is not
    the version state.json recorded. `resume` acknowledges the current version once (drift_ack)."""
    info = getattr(deployer, "deployed_info", None)
    if info is None or not st.get("pac_id"):
        return None
    current = info(st["pac_id"])
    if current is None:
        return None
    version = int(current.pac_version)
    if st.get("drift_accepted") == version:
        return None
    if st.get("drift_ack"):
        st["drift_accepted"] = version
        st.pop("drift_ack", None)
        return None
    if not parse_commit_message(getattr(current, "pac_commit_message", ""))["pac_sync"]:
        return f"{DRIFT}: DEPLOYED v{version} was not made by PAC Sync"
    recorded = (st.get("deployed") or {}).get("version")
    if not history_is_zia and recorded is not None and int(recorded) != version:
        return f"{DRIFT}: v{version} is deployed in ZIA but the history recorded v{recorded}"
    return None


def _stage_and_deploy(cfg, plans, states, modes, deployer, content_commit, rid, promote: bool, force: bool,
                      names) -> bool:
    """Stage / deploy / bootstrap the regions. Return True when the activation was skipped for a pending change."""
    activate = cfg["deploy"].get("activate", True)
    domain = (cfg.get("zscaler") or {}).get("pac_domain") or ""
    refusal = auto_bootstrap_refusal(cfg)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    zia_mode = history_mode(cfg) == "zia"
    if deployer is None:
        return False

    # Activation guard: other admins' pending changes would go live with ours, so look BEFORE the first write.
    skipped = bool(activate and skip_activate_if_pending(cfg) and deployer.activation_pending())
    if skipped:
        log.warning("ZIA has pending changes that are not PAC Sync's: writes are saved but not activated")

    def activate_now(affected, why: str) -> bool:
        """activate(); a failure after the versions were saved must not lose the state (drift next run): mark them."""
        try:
            deployer.activate()
            return True
        except Exception as exc:
            log.error("activation after %s failed, the versions are saved in ZIA but not activated: %s", why, exc)
            for q in affected:
                q.not_activated, q.note = True, f"activation failed: {exc}"
                states[q.region.code]["not_activated"] = "created" if q.bootstrapped else "pending"
            return False

    flagged = [p for p in plans if states[p.region.code].get("not_activated")]
    if flagged and not skipped:
        # The guard is off or ZIA is ACTIVE again: confirm what an earlier run (or the portal) left unactivated.
        # A PAC file created without activation must be committed before it is cloned.
        if not activate or activate_now(flagged, "an earlier run"):
            for p in flagged:
                states[p.region.code].pop("not_activated", None)
                p.confirmed = True

    def drift(p, st) -> bool:
        if force:
            return False
        reason = drift_reason(deployer, st, zia_mode)
        if reason:
            st["paused_reason"] = reason
            p.blocked = reason
            log.error("region %s paused: %s", p.region.code, reason)
        return bool(reason)

    # Phase 1: stage every region whose current render is neither staged nor deployed.
    for p in plans:
        code, st = p.region.code, states[p.region.code]
        if force and str(st.get("paused_reason") or "").startswith(DRIFT):
            st.pop("paused_reason")  # --force overrides a manual-change pause
        if p.blocked or modes[code] == "paused" or st.get("paused_reason"):
            continue
        if st.get("pac_id") and getattr(deployer, "only_prefixed", False):
            try:  # the recorded id is only trusted once ZIA says which file carries the effective name
                st["pac_id"] = deployer.find_pac_id(names.get(code) or p.region.zia_pac_name)
            except Exception as exc:
                p.error = f"stage failed: {exc}"
                continue
        if st.get("pac_id") and drift(p, st):
            continue
        if st.get("deployed", {}).get("hash") == p.hash or st.get("staged", {}).get("hash") == p.hash:
            continue
        if promote:
            p.error = "content changed since it was staged; review the new render before promoting"
            continue
        if skipped and st.get("not_activated") == "created":
            p.not_activated = True
            p.note = "waiting for the activation of the newly created PAC file"
            continue
        git_ref = content_commit
        short = build_message(rid, code, p.added, p.removed, p.hash, git_ref)
        p.zia_message = short
        try:
            if not st.get("pac_id"):
                try:
                    st["pac_id"] = deployer.find_pac_id(names.get(code) or p.region.zia_pac_name)
                except PacFileNotFound as exc:
                    if refusal:
                        raise PacFileNotFound(f"{exc} (no automatic bootstrap: {refusal})") from exc
                    if modes[code] != "auto":
                        raise PacFileNotFound(f"{exc} (a {modes[code]}-mode region is not bootstrapped by a run: "
                                              f"use `pac-sync bootstrap --region {code}`)") from exc
                    # First version of a new PAC file: created DEPLOYED with the current render.
                    short = p.zia_message = build_message(rid, code, digest=p.hash, git=git_ref, kind="bootstrap")
                    st["pac_id"] = deployer.bootstrap(names.get(code) or p.region.zia_pac_name, p.content, domain, short)
                    version = deployer.deployed_version(st["pac_id"]) or 1
                    st["deployed"] = {"version": version, "hash": p.hash, "commit": content_commit or None, "at": stamp}
                    st.pop("staged", None)
                    p.deployed_now = p.bootstrapped = True
                    if skipped:
                        st["not_activated"] = "created"
                        p.not_activated = True
                    continue
                if drift(p, st):
                    continue
            version = deployer.stage(st["pac_id"], p.content, short)
            st["staged"] = {"version": version, "hash": p.hash, "commit": content_commit or None, "at": stamp}
            p.staged_now = True
        except Exception as exc:  # one region failing must not stop the others
            p.error = f"stage failed: {exc}"
    if activate and not skipped and any(p.staged_now for p in plans):
        activate_now([p for p in plans if p.staged_now], "staging")

    # Phase 2: deploy staged versions of regions in auto mode.
    for p in plans:
        code, st = p.region.code, states[p.region.code]
        staged = st.get("staged", {})
        if p.error or p.bootstrapped or modes[code] != "auto" or st.get("paused_reason") or staged.get("hash") != p.hash:
            continue
        try:
            p.prev_version = (st.get("deployed") or {}).get("version")
            if not p.zia_message and hasattr(deployer, "versions"):  # a promotion: report the staged version's message
                shown = next((v for v in deployer.versions(st["pac_id"]) if int(v.pac_version) == int(staged["version"])), None)
                p.zia_message = getattr(shown, "pac_commit_message", "") or ""
            deployer.deploy(st["pac_id"], staged["version"])
            st["deployed"] = dict(staged, at=stamp)
            st.pop("staged", None)
            p.deployed_now = True
        except Exception as exc:
            p.error = f"deploy failed: {exc}"
    if activate and not skipped and any(p.deployed_now for p in plans):
        activate_now([p for p in plans if p.deployed_now], "deploying")

    if skipped:
        for p in plans:
            if p.staged_now or p.deployed_now:
                p.not_activated = True
                states[p.region.code].setdefault("not_activated", "pending")
    elif (cfg["deploy"].get("mark_lkg_after_deploy")) and activate:
        marked = False
        for p in plans:
            if p.deployed_now and not p.bootstrapped and p.prev_version:
                try:
                    deployer.set_lkg(states[p.region.code]["pac_id"], int(p.prev_version))
                    marked = True
                except Exception as exc:  # the deployment itself succeeded
                    log.warning("marking v%s of %s as LKG failed: %s", p.prev_version, p.region.code, exc)
        if marked:
            activate_now([], "marking the LKG")
    return skipped


def _summary(p: RegionPlan) -> str:
    what = "deployed" if p.deployed_now else "staged" if p.staged_now else "unchanged"
    if p.bootstrapped:
        what = "bootstrapped (PAC file created in ZIA, first version deployed)"
    if p.not_activated:
        what += " (saved in ZIA, not activated)"
    if p.note:
        what += f" ({p.note})"
    if p.blocked:
        what = f"BLOCKED: {p.blocked}"
    if p.error:
        what = f"FAILED: {p.error}"
    return f"{p.region.code}: {what} (+{len(p.added)} -{len(p.removed)}, sha256 {p.hash[:12]})"


def _write_back(cfg, source, result, raw_rules, states, plans, commit, now, not_activated=frozenset()) -> None:
    if not cfg["servicenow"].get("write_back", True):
        return
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    hashes = {p.region.code: p.hash for p in plans if not p.blocked}
    raw_by_id = {r["sys_id"]: r for r in raw_rules}
    failed = {p.region.code: p.error for p in plans if p.error}
    for sys_id, body in compute_rule_updates(result, raw_by_id, states, hashes, now_str, failed, not_activated):
        try:
            source.update_rule(sys_id, body)
        except Exception as exc:
            log.error("write-back to %s failed: %s", sys_id, exc)
    for p in plans:
        if not (p.staged_now or p.deployed_now or p.error or p.confirmed):
            continue
        st = states[p.region.code]
        live = p.deployed_now or (p.confirmed and bool(st.get("deployed")))
        version = (st.get("deployed") if live else st.get("staged")) or {}
        summary = (p.zia_message + "\n" if p.zia_message else "") \
            + ("activation confirmed: the saved version is live\n" if p.confirmed else "") + _summary(p) \
            + "\nAdded: " + ", ".join(p.added) + "\nRemoved: " + ", ".join(p.removed)
        try:
            source.log_deployment({
                "u_region": p.region.sys_id,
                "u_zia_version": version.get("version"),
                "u_status": "failed" if p.error else "deployed" if live else "staged",
                "u_git_commit": commit or "",
                "u_content_hash": p.hash,
                "u_rules_added": len(p.added),
                "u_rules_removed": len(p.removed),
                "u_summary": summary[:4000],
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


def resolve_pac_id(history, deployer, name_for, code: str) -> int:
    """pac_id from the recorded state, else by the region's effective PAC name (no git state needed)."""
    recorded = history.read_state(code).get("pac_id") if not isinstance(history, ZiaHistory) else None
    if recorded and not getattr(deployer, "only_prefixed", False):
        return int(recorded)
    return deployer.find_pac_id(name_for(code))  # by name: under the prefix guard the id must come with its real name


def rollback(cfg: dict, code: str, version: Optional[int] = None, to_lkg: bool = False) -> int:
    """Redeploy an older ZIA version (``version``, or the LKG with ``to_lkg``), log it, pause the region."""
    if (version is None) == (not to_lkg):
        raise ConfigError("rollback needs exactly one of --version N or --to lkg")
    now = datetime.now(timezone.utc)
    rid = run_id(now)
    with zia_session(cfg) as (history, deployer, name_for):
        with run_lock(history.root):
            pac_id = resolve_pac_id(history, deployer, name_for, code)
            versions = deployer.versions(pac_id, with_content=True)
            if to_lkg:
                lkg = deployer._by_status(versions, "LKG")
                if lkg is None:
                    log.error("PAC file of region %s has no LKG version", code)
                    return 2
                version = int(lkg.pac_version)
            target = next((v for v in versions if int(v.pac_version) == int(version)), None)
            if target is None:
                log.error("PAC file of region %s has no version %s", code, version)
                return 2
            skip = bool(cfg["deploy"].get("activate", True) and skip_activate_if_pending(cfg)
                        and deployer.activation_pending())
            deployer.deploy(pac_id, version)
            if cfg["deploy"].get("activate", True) and not skip:
                deployer.activate()
            when = now.isoformat(timespec="seconds")
            st = history.read_state(code)
            st["pac_id"] = pac_id
            st["deployed"] = {"version": version, "hash": None, "commit": None, "at": when}
            # Without the pause, the next scheduled run would stage (and in auto mode deploy) the bad render again.
            st["paused_reason"] = f"rollback to v{version} at {when}; fix ServiceNow, then `pac-sync resume --region {code}`"
            st["drift_ack"] = True  # the version we just deployed is intended
            if skip:
                st["not_activated"] = "pending"
            history.write_state(code, st)
            history.commit(f"Rollback {code} to ZIA version {version}")
            history.tag(f"{code}-v{version}-rollback", f"Rollback {code} to ZIA version {version}")
            git_ref = history.head()
            _push(history)
            from .zia_history import normalized_hash
            message = build_message(rid, code, digest=normalized_hash(getattr(target, "pac_content", "") or ""),
                                    git=git_ref, rollback_to=version)
            _log_rollback(cfg, code, version, message + ("\n" + NOT_ACTIVATED if skip else ""), git_ref, target, now)
    notify(cfg, f"PAC Sync: {code} rolled back to ZIA version {version}; region paused"
           + (f"\n{NOT_ACTIVATED}" if skip else ""))
    if skip:
        log.warning(NOT_ACTIVATED)
        return 1
    return 0


def _log_rollback(cfg, code, version, summary, git_ref, target, now) -> None:
    """Write the u_pac_deployment row (u_status=rolled_back). ServiceNow being down must not undo the rollback."""
    try:
        source = make_source(cfg["servicenow"])
        regions, _, _ = load_source_data(source)
        region = next(r for r in regions.values() if r.code == code)
        from .zia_history import normalized_hash
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        source.log_deployment({
            "u_region": region.sys_id, "u_zia_version": version, "u_status": "rolled_back", "u_git_commit": git_ref or "",
            "u_content_hash": normalized_hash(getattr(target, "pac_content", "") or ""), "u_rules_added": 0,
            "u_rules_removed": 0, "u_summary": summary[:4000], "u_started": stamp, "u_finished": stamp,
        })
    except Exception as exc:
        log.error("deployment log for the rollback of %s failed: %s", code, exc)


def resume(cfg: dict, code: str) -> int:
    """Clear a pause and acknowledge the current ZIA version (so a rollback or a manual change is not flagged again)."""
    with zia_session(cfg, need_zia=False) as (history, _deployer, _name_for):
        with run_lock(history.root):
            def clear(st: dict) -> None:
                st.pop("paused_reason", None)
                st["drift_ack"] = True

            history.update_state(code, clear)
            if history.commit(f"Resume {code}"):
                _push(history)
    return 0


def status(cfg: dict, out: Callable[[str], None] = print) -> int:
    """Print each region's recorded ZIA state. Offline for git / both; reads ZIA in zia mode."""
    with zia_session(cfg, need_zia=False, push=False) as (history, _deployer, _name_for):
        target = zscaler_target(cfg.get("zscaler") or {})
        where = getattr(history, "state_file", None) or history.root
        out(f"History {where} (mode {history_mode(cfg)}, ZIA target: {target})")
        codes = history.codes()
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
            if st.get("not_activated"):
                out("  not activated: saved in ZIA, waiting for activation in the portal")
    return 0


def bootstrap_region(cfg: dict, code: str, yes: bool = False, confirm: Callable[[str], str] = input) -> int:
    """Create the region's PAC file in ZIA with the current render as its first, DEPLOYED version."""
    rendered = _render_region(cfg, code)
    if rendered is None:
        log.error("unknown or inactive region %s", code)
        return 2
    region, rules, content, digest = rendered
    zcfg = cfg.get("zscaler") or {}
    name = effective_pac_name(zcfg, region.zia_pac_name)
    if is_live(cfg) and not yes:
        if not stdin_is_interactive():  # no console to ask: do not even show the prompt
            log.error("bootstrap of %s refused: live ZIA target needs --yes (stdin is not interactive)", code)
            return 2
        prompt = (f"Create PAC file '{name}' in the LIVE ZIA tenant and deploy it now? "
                  f"Type the region code ({code}) to confirm: ")
        try:
            answer = confirm(prompt)
        except EOFError:
            answer = ""
        if answer.strip() != code:
            log.error("bootstrap of %s refused: live ZIA target needs --yes or an interactive confirmation", code)
            return 2
    domain = zcfg.get("pac_domain") or ""
    if not domain:
        log.error("zscaler.pac_domain is not set: ZIA needs a domain to create a PAC file")
        return 2

    now = datetime.now(timezone.utc)
    with zia_session(cfg) as (history, deployer, name_for):
        deployer.on_evict = None
        digest = history.plan_hash(content, digest)
        with run_lock(history.root):
            history.ensure()
            st = history.read_state(code)
            try:
                existing = deployer.find_pac_id(name)
            except PacFileNotFound:
                existing = None
            if existing is not None:
                log.error("ZIA already has a PAC file named '%s' (id %s): nothing to bootstrap", name, existing)
                return 1
            history.write_region(code, content, [r.to_json() for r in rules])
            content_commit = history.commit(f"Bootstrap {code}: render for new ZIA PAC file {name}") or history.head()
            message = build_message(run_id(now), code, digest=digest, git=content_commit, kind="bootstrap")
            skip = bool(cfg["deploy"].get("activate", True) and skip_activate_if_pending(cfg)
                        and deployer.activation_pending())
            pac_id = deployer.bootstrap(name, content, domain, message)
            if cfg["deploy"].get("activate", True) and not skip:
                deployer.activate()
            version = deployer.deployed_version(pac_id) or 1
            st.update(pac_id=pac_id, last_error=None,
                      deployed={"version": version, "hash": digest, "commit": content_commit or None,
                                "at": now.isoformat(timespec="seconds")})
            st.pop("staged", None)
            if skip:
                st["not_activated"] = "created"
            history.write_state(code, st)
            history.commit(f"Bootstrap {code}: ZIA PAC file {name} id {pac_id} version {version}")
            history.tag(f"{code}-v{version}", f"{code} ZIA version {version} (bootstrap, {content_commit[:10]})")
            push_error = _push(history)
    log.info("%s: PAC file '%s' created (id %s), version %s deployed", code, name, pac_id, version)
    if skip:
        log.warning("%s; the next run activates nothing until ZIA is active again", NOT_ACTIVATED)
    notify(cfg, f"PAC Sync: {code} bootstrapped, ZIA PAC file {name} version {version}"
           + (f"\n{NOT_ACTIVATED}" if skip else "") + (f"\nGit push failed: {push_error}" if push_error else ""))
    return 1 if push_error or skip else 0


def _add_region(sub, name: str, **kw):
    p = sub.add_parser(name, **kw)
    p.add_argument("--region", required=True)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pac-sync")
    parser.add_argument("--config", default=os.environ.get("PAC_SYNC_CONFIG", "config/config.yaml"))
    parser.add_argument("--env-file", default=None,
                        help="credentials file loaded before the config (default: .env of the config's project, else ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true", help="render and diff only: no commit, no ZIA, no write-back")
    p_run.add_argument("--force", action="store_true", help="bypass the removed-rules guardrail and a manual-change pause")
    for name in ("render", "promote", "resume"):
        _add_region(sub, name)
    p_rb = _add_region(sub, "rollback")
    group = p_rb.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", type=int)
    group.add_argument("--to", choices=("lkg",), help="redeploy the last known good version")
    sub.add_parser("status", help="each region's recorded ZIA state")
    p_bs = _add_region(sub, "bootstrap", help="create the region's PAC file in ZIA (first version deployed)")
    p_bs.add_argument("--yes", action="store_true", help="do not ask for confirmation on a live ZIA target")
    _add_region(sub, "versions", help="list the ZIA versions of the region's PAC file")
    p_df = _add_region(sub, "diff", help="unified diff between two versions")
    p_df.add_argument("--from", dest="src", required=True, help="N | deployed | staged | lkg")
    p_df.add_argument("--to", dest="dst", required=True, help="N | deployed | staged | lkg | render")
    p_lk = _add_region(sub, "lkg", help="mark / unmark the last known good version")
    g2 = p_lk.add_mutually_exclusive_group(required=True)
    g2.add_argument("--version", type=int)
    g2.add_argument("--remove", action="store_true")
    p_lk.add_argument("--new-lkg", type=int, help="with --remove: the version that becomes LKG")
    p_dc = sub.add_parser("demo-cleanup", help="delete the PAC files whose name has the demo prefix")
    p_dc.add_argument("--yes", action="store_true")
    p_doc = sub.add_parser("doctor", help="read-only checks; exit 1 if any check fails")
    p_doc.add_argument("--write-probe", action="store_true", help="stage and unstage one prefixed PAC file (no activation)")
    p_doc.add_argument("--region")
    p_setup = sub.add_parser("setup", help="write config/config.yaml and .env")
    p_setup.add_argument("--profile", choices=("mock", "demo", "custom"))
    p_setup.add_argument("--non-interactive", action="store_true", help="use the profile defaults, ask nothing")
    p_setup.add_argument("--force", action="store_true", help="overwrite an existing config and .env keys")
    p_setup.add_argument("--config-out", help="config file to write (default: --config)")
    p_setup.add_argument("--env-out", help="env file to write (default: --env-file)")
    p_setup.add_argument("--no-doctor", action="store_true", help="do not run doctor at the end")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    env_file = args.env_file or default_env_file(args.config)
    if args.command == "setup":
        from .setup_wizard import run_setup

        return run_setup(profile=args.profile, non_interactive=args.non_interactive, force=args.force,
                         config_out=args.config_out or args.config, env_out=args.env_out or env_file,
                         doctor=not args.no_doctor)

    if Path(env_file).is_file():
        loaded = load_env_file(env_file)
        log.debug("loaded %d variable(s) from %s: %s", len(loaded), env_file, ", ".join(loaded) or "-")
    elif args.env_file:
        log.warning("env file %s not found", args.env_file)
    if args.command == "doctor":
        from .doctor import run_doctor

        return run_doctor(args.config, env_file, write_probe=args.write_probe, region=args.region)

    cfg = load_config(args.config)
    try:
        validate_prefix_config(cfg.get("zscaler") or {})
        if args.command == "run":
            return sync(cfg, dry_run=args.dry_run, force=args.force)
        if args.command == "promote":
            return sync(cfg, promote=args.region)
        if args.command == "render":
            return render_only(cfg, args.region)
        if args.command == "rollback":
            return rollback(cfg, args.region, args.version, to_lkg=args.to == "lkg")
        if args.command == "status":
            return status(cfg)
        if args.command == "bootstrap":
            return bootstrap_region(cfg, args.region, yes=args.yes)
        if args.command in ("versions", "diff", "lkg", "demo-cleanup"):
            from . import zia_cli

            if args.command == "versions":
                return zia_cli.versions(cfg, args.region)
            if args.command == "diff":
                return zia_cli.diff(cfg, args.region, args.src, args.dst)
            if args.command == "lkg":
                return zia_cli.lkg(cfg, args.region, args.version, args.remove, args.new_lkg)
            return zia_cli.demo_cleanup(cfg, yes=args.yes)
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
