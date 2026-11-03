"""Shared engine of the ZIA use cases built on named groups of entries (URL filtering; forwarding follows the same shape).

A use case (`UseCase`, one small module each) describes one kind of group: ServiceNow holds admin-owned groups plus
requested entries; ZIA holds the objects of one group under `zscaler.pac_name_prefix` + group name. The engine does what
every use case shares: read ServiceNow, validate (`normalize_entries`), read the managed ZIA objects, plan per group
(create / update / delete / unchanged / foreign), check drift and guardrails, apply, activate, write back.

- Ownership marker, no state file: every managed object's description is a PAC Sync message whose sha256 is the hash of the
  group's canonical managed fields. ZIA objects whose canonical hash differs from their description are DRIFT (a portal
  edit) and pause the use case; prefixed objects without a PAC Sync description are FOREIGN and are never modified.
- Pauses, `drift_ack`, the not-activated groups and the last error live in the git-ignored `use_cases.state_file`.
- Write-back reuses `writeback.compute_rule_updates`: a group name plays the part of the region code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .deployer import assert_prefixed
from .envfile import ConfigError
from .normalizer import NUMBER_RE, NormalizeResult, parse_snow_datetime, validate_object_name
from .models import ACTIVE_STATES, RETIRE_STATE
from .writeback import compute_rule_updates
from .zia_messages import build_message, parse_commit_message

log = logging.getLogger(__name__)

KEYS = ("url_filtering", "forwarding")
USE_CASE_MODES = ("auto", "paused")
MAX_NAME = 31  # ZIA object names (forwarding rules are the tightest): prefix + group name
DELETED = "deleted"  # hash of a group that is being removed: its retirements wait until the delete succeeded
DEFAULT_STATE_FILE = "pac-state-usecases.json"


@dataclass(frozen=True)
class Entry:
    """One validated ServiceNow entry. `regions` lets writeback.compute_rule_updates treat the group as a region."""

    sys_id: str
    number: str
    group: str
    value: str
    match_type: str = ""
    state: str = ""

    @property
    def regions(self) -> Tuple[str, ...]:
        return (self.group,)


@dataclass
class ZGroup:
    """What ZIA holds for one group. `canonical` is None while one of its objects is missing (a half-created group)."""

    name: str  # ZIA name, with the prefix
    values: List[str]
    canonical: Optional[dict]
    descriptions: List[str]
    handles: tuple = ()  # the SDK objects, for the adapter's update / delete


@dataclass(frozen=True)
class UseCase:
    key: str
    label: str  # "URL category", in messages
    group_table: str  # keys of servicenow.tables
    entry_table: str
    group_fields: str
    entry_fields: str
    group_ref: str  # entry field that references its group
    validate_group: Callable[[dict], Optional[str]]
    validate_entry: Callable[[dict], Tuple[str, Optional[str]]]  # record -> (normalized value, error)
    canonical: Callable[[str, dict, List[str]], dict]  # (ZIA name, group record, sorted values) -> managed fields
    make_api: Callable  # (client, guard, prefix, use-case config) -> adapter with read / create / update / delete


def get_use_case(key: str) -> UseCase:
    if key == "url_filtering":
        from .url_filtering import URL_FILTERING

        return URL_FILTERING
    if key == "forwarding":
        from .forwarding import FORWARDING

        return FORWARDING
    raise ConfigError(f"unknown use case '{key}' (known: {', '.join(KEYS)})")


def use_case_config(cfg: dict, key: str) -> dict:
    return ((cfg.get("use_cases") or {}).get(key)) or {}


def enabled_keys(cfg: dict) -> List[str]:
    return [k for k in KEYS if use_case_config(cfg, k).get("enabled")]


def state_path(cfg: dict) -> str:
    return (cfg.get("use_cases") or {}).get("state_file") or DEFAULT_STATE_FILE


def digest(canonical: dict) -> str:
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_use_case_config(cfg: dict) -> None:
    """An enabled use case needs the prefix guard, a valid mode and its ServiceNow table keys; ConfigError otherwise."""
    block = cfg.get("use_cases") or {}
    z = cfg.get("zscaler") or {}
    for key, ucfg in block.items():
        if key == "state_file" or not (ucfg or {}).get("enabled"):
            continue
        if key not in KEYS:
            raise ConfigError(f"use_cases.{key}: unknown or unavailable use case (available: {', '.join(KEYS)})")
        if not (z.get("only_prefixed") and z.get("pac_name_prefix")):
            raise ConfigError(f"use_cases.{key}.enabled needs zscaler.only_prefixed: true and a non-empty "
                              "zscaler.pac_name_prefix (every managed ZIA object is named with the prefix)")
        mode = ucfg.get("mode") or "auto"
        if mode not in USE_CASE_MODES:
            raise ConfigError(f"use_cases.{key}.mode must be one of {', '.join(USE_CASE_MODES)}, not '{mode}'")
        if key == "forwarding" and (z.get("auth") or "oneapi") == "legacy":
            raise ConfigError("use_cases.forwarding needs zscaler.auth: oneapi (the legacy client has no forwarding_control)")
        spec = get_use_case(key)
        tables = (cfg.get("servicenow") or {}).get("tables") or {}
        missing = [k for k in (spec.group_table, spec.entry_table, "deployment") if not tables.get(k)]
        if missing:
            raise ConfigError(f"use_cases.{key}.enabled needs servicenow.tables: {', '.join(missing)}")


class UseCaseState:
    """Pauses, drift acknowledgement, not-activated groups and the last error, per use case (JSON, atomic writes)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._data: Dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}

    def get(self, key: str) -> dict:
        return self._data.setdefault(key, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pac-state-", suffix=".json", dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)


def group_errors(groups: Dict[str, dict], spec: UseCase, prefix: str) -> Dict[str, Optional[str]]:
    """sys_id -> why an active group cannot be deployed (None when it can)."""
    names = [g.get("u_name") for g in groups.values()]
    out: Dict[str, Optional[str]] = {}
    for sys_id, g in groups.items():
        name = g.get("u_name") or ""
        out[sys_id] = (validate_object_name(name)
                       or ("name is used by more than one active group" if names.count(g.get("u_name")) > 1 else None)
                       or (f"name '{prefix}{name}' is longer than {MAX_NAME} characters" if len(prefix + name) > MAX_NAME
                           else None)
                       or spec.validate_group(g))
    return out


def normalize_entries(raw: List[dict], groups: Dict[str, dict], spec: UseCase, prefix: str = "",
                      now: Optional[datetime] = None, reserved: Optional[Dict[str, str]] = None) -> NormalizeResult:
    """Validate entries and group them per group name. Same state, validity and duplicate rules as normalizer.normalize.

    ``groups`` maps sys_id -> active group record. Duplicates (V8/V9): the same value in any two live entries of the use
    case; the lowest number wins; same group = "duplicate of", another group = "conflicts with". ``reserved`` maps a value
    to the ZIA object that keeps holding it (an invalid group left unchanged): entries claiming it are rejected.
    """
    now = now or datetime.now(timezone.utc)
    result = NormalizeResult(rules_by_region={})
    bad_group = group_errors(groups, spec, prefix)
    candidates: List[Tuple[Entry, dict]] = []
    for rec in raw:
        state = rec.get("u_state", "")
        if state == RETIRE_STATE:
            result.retiring.append(rec)
            continue
        if state not in ACTIVE_STATES:
            continue
        try:
            valid_from = parse_snow_datetime(rec.get("u_valid_from", ""))
            valid_to = parse_snow_datetime(rec.get("u_valid_to", ""))
        except ValueError as exc:
            result.rejected.append((rec, f"invalid date: {exc}"))
            continue
        if valid_to and valid_to <= now:
            result.expired.append(rec)
            continue
        if valid_from and valid_from > now:
            result.future.append(rec)
            continue
        entry, error = _build_entry(rec, groups, bad_group, spec)
        if error:
            result.rejected.append((rec, error))
        else:
            candidates.append((entry, rec))

    candidates.sort(key=lambda c: c[0].number)
    winners: Dict[str, Entry] = {}
    for entry, rec in candidates:
        if entry.value in (reserved or {}):
            result.rejected.append((rec, f"conflicts with {reserved[entry.value]} (left unchanged)"))
            continue
        clash = winners.get(entry.value)
        if clash:
            kind = "duplicate of" if clash.group == entry.group else "conflicts with"
            result.rejected.append((rec, f"{kind} {clash.number} ({entry.value})"))
            continue
        winners[entry.value] = entry
    for entry in sorted(winners.values(), key=lambda e: (e.group, e.value, e.number)):
        result.all_rules.append(entry)
        result.rules_by_region.setdefault(entry.group, []).append(entry)
    return result


def _build_entry(rec: dict, groups: Dict[str, dict], bad_group: Dict[str, Optional[str]],
                 spec: UseCase) -> Tuple[Optional[Entry], Optional[str]]:
    if not NUMBER_RE.fullmatch(rec.get("number") or ""):
        return None, "invalid record number (letters, digits, _ and - only, 1 to 40 characters)"
    ref = rec.get(spec.group_ref) or ""
    group = groups.get(ref)
    if group is None:
        return None, f"{spec.label} is unknown or inactive"
    if bad_group.get(ref):
        return None, f"invalid {spec.label} '{group.get('u_name')}': {bad_group[ref]}"
    value, error = spec.validate_entry(rec)
    if error:
        return None, error
    return Entry(rec.get("sys_id", ""), rec.get("number", ""), group["u_name"], value,
            (rec.get("u_match_type") or "").strip().lower(), rec.get("u_state", "")), None


@dataclass
class UseCaseResult:
    key: str
    done: Dict[str, str] = field(default_factory=dict)  # group -> create | update | delete
    unchanged: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    blocked: Optional[str] = None
    paused: Optional[str] = None  # a manual change in ZIA, or a pause left by an earlier run
    error: Optional[str] = None  # the whole use case failed (ServiceNow or ZIA unreadable)
    note: str = ""
    not_activated: bool = False
    writeback_error: str = ""
    rejected: int = 0
    confirmed: List[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors or self.blocked or self.paused or self.error or self.writeback_error)

    def summary(self) -> str:
        if self.error:
            return f"{self.key}: FAILED: {self.error}"
        if self.paused:
            return f"{self.key}: PAUSED: {self.paused}"
        if self.blocked:
            return f"{self.key}: BLOCKED: {self.blocked}"
        parts = [f"{a} {n}" for n, a in sorted(self.done.items())] + [f"FAILED {n}: {e}" for n, e in sorted(self.errors.items())]
        text = f"{self.key}: " + ("; ".join(parts) if parts else self.note or f"unchanged ({len(self.unchanged)} group(s))")
        return text + (" (saved in ZIA, not activated)" if self.not_activated else "")


def classify(zia: Dict[str, ZGroup]) -> Tuple[set, Dict[str, str]]:
    """(foreign names, {drifted name: reason}) of the ZIA groups, keyed like ``zia``.

    Foreign: a description that is not a PAC Sync message. Drift: the sha256 in a description is not the hash of the
    canonical fields ZIA returns now (someone edited the object in the portal)."""
    foreign, drift = set(), {}
    for name, zg in zia.items():
        infos = [parse_commit_message(d) for d in zg.descriptions]
        if not infos or not all(i["pac_sync"] for i in infos):
            foreign.add(name)
        elif zg.canonical is not None and any(i["sha256"] != digest(zg.canonical)[:12] for i in infos):
            drift[name] = f"{zg.name} was changed in ZIA"
    return foreign, drift


def _guard(deployer) -> Callable[[str, str], None]:
    return lambda name, what: assert_prefixed(deployer.prefix, deployer.only_prefixed, name, what)


def make_api(spec: UseCase, deployer, ucfg: dict):
    return spec.make_api(deployer.client, _guard(deployer), deployer.prefix, ucfg)


def read_zia(api, prefix: str) -> Dict[str, ZGroup]:
    return {zg.name[len(prefix):]: zg for zg in api.read().values()}


def managed_groups(cfg: dict, deployer) -> List[Tuple[object, ZGroup]]:
    """(adapter, ZGroup) of every PAC Sync object of the enabled use cases: prefixed AND a PAC Sync description."""
    out = []
    for key in enabled_keys(cfg):
        api = make_api(get_use_case(key), deployer, use_case_config(cfg, key))
        zia = read_zia(api, deployer.prefix)
        foreign, _ = classify(zia)
        out += [(api, zg) for n, zg in sorted(zia.items()) if n not in foreign]
    return out


def run_use_cases(cfg: dict, source, deployer, rid: str, now: datetime, dry_run: bool = False,
                  force: bool = False) -> List[UseCaseResult]:
    """Run every enabled use case. One use case failing never stops the others. ``dry_run`` reads and plans only."""
    results: List[UseCaseResult] = []
    keys = enabled_keys(cfg)
    if not keys:
        return results
    state = UseCaseState(state_path(cfg))
    for key in keys:
        try:
            res = _run_one(cfg, get_use_case(key), source, deployer, rid, now, dry_run, force, state)
        except Exception as exc:
            log.error("use case %s failed: %s", key, exc)
            res = UseCaseResult(key, error=str(exc))
            if not dry_run:
                state.get(key)["last_error"] = str(exc)
                state.save()
        results.append(res)
        log.info(res.summary())
    return results


def _run_one(cfg, spec: UseCase, source, deployer, rid: str, now: datetime, dry_run: bool, force: bool,
             state: UseCaseState) -> UseCaseResult:
    from .main import DRIFT, MODE_RANK, _unreachable, live_max_mode, skip_activate_if_pending

    res = UseCaseResult(spec.key)
    ucfg = use_case_config(cfg, spec.key)
    st = state.get(spec.key)
    prefix = deployer.prefix
    cap = live_max_mode(cfg)
    if (ucfg.get("mode") or "auto") == "paused" or (cap and MODE_RANK[cap] < MODE_RANK["auto"]):
        res.note = "paused by configuration (use_cases mode or deploy.live_max_mode)"
        return res
    if force and str(st.get("paused_reason") or "").startswith(DRIFT):
        st.pop("paused_reason")  # --force overrides a manual-change pause
    if st.get("paused_reason") and not force:
        res.paused = st["paused_reason"]
        return res

    # (3) ServiceNow
    groups = {g["sys_id"]: g for g in source.fetch_groups(spec.group_table, spec.group_fields)}
    raw = source.fetch_entries(spec.entry_table, spec.entry_fields)
    by_name = {g["u_name"]: g for g in groups.values()}
    # An active group that became invalid keeps its ZIA object untouched and fails: only a name with no active group
    # (deactivated, or every entry retired) is deleted.
    invalid = {g["u_name"]: why for sys_id, why in group_errors(groups, spec, prefix).items() if why
               for g in [groups[sys_id]]}

    # (4) ZIA (read before the entries are validated: values held by a kept invalid group stay reserved)
    api = make_api(spec, deployer, ucfg)
    zia = read_zia(api, prefix)
    foreign, drift = classify(zia)
    reserved = {v: zia[n].name for n in invalid if n in zia and n not in foreign for v in zia[n].values}
    result = normalize_entries(raw, groups, spec, prefix, now, reserved)
    res.rejected = len(result.rejected)
    for rec, why in result.rejected:
        log.warning("rejected %r: %s", rec.get("number"), why)

    # (5) plan per group
    want: Dict[str, tuple] = {}  # name -> (values, canonical, hash, entries)
    for name, entries in result.rules_by_region.items():
        values = [e.value for e in entries]
        canon = spec.canonical(prefix + name, by_name[name], values)
        want[name] = (values, canon, digest(canon), entries)
    plan: Dict[str, str] = {}
    for name in sorted(set(want) | set(zia)):
        zg = zia.get(name)
        if name in foreign:
            plan[name] = "foreign"
        elif name in invalid and name not in want:
            plan[name] = "invalid"
        elif name in want:
            plan[name] = "create" if zg is None else "unchanged" if zg.canonical == want[name][1] else "update"
        else:
            plan[name] = "delete"
    for name in sorted(n for n, a in plan.items() if a == "invalid" and n in zia):
        res.errors[name] = f"{spec.label} '{name}' is invalid ({invalid[name]}): {zia[name].name} in ZIA is left unchanged"
    for name in sorted(foreign):
        msg = f"{zia[name].name} exists in ZIA but was not created by PAC Sync: never modified or deleted"
        if name in want:
            res.errors[name] = msg
        else:
            log.warning(msg)

    # (6) drift, (7) guardrails
    ack = st.get("drift_ack")
    if drift and not force and not ack:
        res.paused = f"{DRIFT}: " + ", ".join(zia[n].name for n in sorted(drift))
    previous = sum(len(zia[n].values) for n in zia if n not in foreign)
    removed = {(n, v) for n, zg in zia.items() if n not in foreign and plan.get(n) != "invalid" for v in zg.values if v not in (want.get(n, ([],))[0])}
    gr = cfg.get("guardrails") or {}
    max_ratio, min_prev = float(gr.get("max_removed_ratio", 0.2)), int(gr.get("min_previous_rules_for_ratio", 10))
    if not force and not res.paused:
        if not raw and previous:
            res.blocked = f"ServiceNow returned 0 entries but ZIA holds {previous}: run aborted"
        elif previous >= min_prev and len(removed) / previous > max_ratio:
            res.blocked = (f"{len(removed)} of {previous} entries would be removed (limit {max_ratio:.0%}); "
                           "rerun with --force if this is intended")

    # (8) dry run
    if dry_run:
        for name, action in plan.items():
            log.info("[dry-run] %s: %s %s%s", spec.key, action, prefix + name,
                     f" ({len(want[name][0])} entries)" if name in want else "")
        for why in (res.paused, res.blocked):
            if why:
                log.info("[dry-run] %s would not write: %s", spec.key, why)
        return res
    if res.paused or res.blocked:
        if res.paused:
            st["paused_reason"] = res.paused
        st["last_error"] = res.paused or res.blocked
        state.save()
        return res
    st.pop("drift_ack", None)  # one use

    # (9) activation guard
    activate = cfg["deploy"].get("activate", True)
    todo = [n for n, a in plan.items() if a in ("create", "update", "delete") and n not in res.errors]
    flagged = set(st.get("not_activated") or [])
    skipped = bool((todo or flagged) and activate and skip_activate_if_pending(cfg) and deployer.activation_pending())
    if skipped:
        log.warning("ZIA has pending changes that are not PAC Sync's: writes are saved but not activated")

    def activate_now(names, why: str) -> bool:
        try:
            deployer.activate()
            return True
        except Exception as exc:  # the objects are saved: mark them, or the next run reads them as drift
            log.error("activation after %s failed, the objects are saved in ZIA but not activated: %s", why, exc)
            flagged.update(names)
            return False

    if flagged and not skipped and (not activate or activate_now(sorted(flagged), "an earlier run")):
        res.confirmed, flagged = sorted(flagged), set()

    # (10) apply: deletes first (they free ZIA's category quota), each group in its own try
    messages: Dict[str, str] = {}
    added_by: Dict[str, List[str]] = {}
    removed_by: Dict[str, List[str]] = {}
    gone = [r for r in result.retiring + result.expired]
    for name in sorted(todo, key=lambda n: plan[n] != "delete"):
        zg, w = zia.get(name), want.get(name)
        old = set(zg.values) if zg else set()
        new = set(w[0]) if w else set()
        added_by[name] = [e.number for e in (w[3] if w else []) if e.value not in old]
        removed_by[name] = sorted(rec["number"] for rec in gone if (by_name.get(name) or {}).get("sys_id") ==
                                  rec.get(spec.group_ref) and (rec.get("u_value") or "").strip().lower() in old - new)
        messages[name] = build_message(rid, name, added_by[name], removed_by[name], w[2] if w else DELETED)
        try:
            if plan[name] == "create":
                api.create(prefix + name, by_name[name], w[0], messages[name])
            elif plan[name] == "update":
                api.update(zg, by_name[name], w[0], messages[name])
            else:
                api.delete(zg)
            res.done[name] = plan[name]
        except Exception as exc:
            res.errors[name] = str(exc)
            log.error("%s %s failed: %s", plan[name], prefix + name, exc)
    res.unchanged = [n for n, a in plan.items() if a == "unchanged"]

    # (11) activate
    written = sorted(res.done)
    if written and activate:
        if skipped:
            flagged.update(written)
        else:
            activate_now(written, "writing")
    res.not_activated = bool(flagged)
    st["not_activated"] = sorted(flagged)
    st["last_error"] = "; ".join(f"{n}: {e}" for n, e in sorted(res.errors.items())) or None
    state.save()

    # (12) write back
    if cfg["servicenow"].get("write_back", True):
        live = {n: (DELETED if plan[n] == "delete" else want[n][2]) for n in plan if n in want or plan[n] == "delete"}
        states = {n: {"deployed": {"hash": h, "version": h[:12]}} for n, h in live.items()
                  if n not in res.errors and (plan[n] in ("unchanged",) or n in res.done)}
        res.writeback_error = _write_back(cfg, spec, source, result, raw, states, live, res, plan, want, messages,
                                          added_by, removed_by, flagged, now, _unreachable)
    return res


def _write_back(cfg, spec, source, result, raw, states, hashes, res: UseCaseResult, plan, want, messages, added_by,
                removed_by, flagged, now, unreachable) -> str:
    """Entry statuses (compute_rule_updates) and one u_pac_deployment row per changed, failed or confirmed group."""
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    prefix = cfg["zscaler"].get("pac_name_prefix") or ""
    updates = compute_rule_updates(result, {r["sys_id"]: r for r in raw}, states, hashes, now_str,
                                   dict(res.errors), flagged)
    errors = 0
    for i, (sys_id, body) in enumerate(updates):
        try:
            source.update_entry(spec.entry_table, sys_id, body)
        except Exception as exc:
            errors += 1
            log.error("write-back to %s failed: %s", sys_id, exc)
            if unreachable(exc):
                log.error("ServiceNow unreachable: %d more update(s) and the deployment rows not sent", len(updates) - i - 1)
                return f"{len(updates) - i} update(s) and the deployment rows ({exc})"
    for name in sorted(set(res.done) | set(res.errors) | set(res.confirmed)):
        w = want.get(name)
        if name in res.errors:
            status = "failed"
        else:
            status = "staged" if name in flagged else "deployed"
        summary = (messages.get(name, "") + "\n" if name in messages else "") \
            + ("activation confirmed: the saved objects are live\n" if name in res.confirmed else "") \
            + f"{spec.key} {prefix + name}: " + (res.errors.get(name) or res.done.get(name) or "confirmed") \
            + ("" if name not in flagged else " (saved in ZIA, not activated)") \
            + "\nAdded: " + ", ".join(added_by.get(name, [])) + "\nRemoved: " + ", ".join(removed_by.get(name, []))
        try:
            source.log_deployment({
                "u_use_case": spec.key, "u_object": prefix + name, "u_region": "", "u_status": status,
                "u_git_commit": "", "u_content_hash": w[2] if w else "",
                "u_rules_added": len(added_by.get(name, [])), "u_rules_removed": len(removed_by.get(name, [])),
                "u_summary": summary[:4000], "u_started": now_str,
                "u_finished": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as exc:
            errors += 1
            log.error("deployment log for %s failed: %s", name, exc)
    return f"{errors} write(s), see the log" if errors else ""


def resume_use_case(cfg: dict, key: str) -> None:
    """Clear the pause and acknowledge the current ZIA state once (the next run overwrites it with the desired state)."""
    state = UseCaseState(state_path(cfg))
    st = state.get(key)
    st.pop("paused_reason", None)
    st["drift_ack"] = True
    state.save()


def status_lines(cfg: dict) -> List[str]:
    if not enabled_keys(cfg):
        return []
    state = UseCaseState(state_path(cfg))
    lines = []
    for key in enabled_keys(cfg):
        st = state.get(key)
        mode = use_case_config(cfg, key).get("mode") or "auto"
        lines += [f"{key}: mode {mode}", f"  last_error: {st.get('last_error') or '-'}",
                  f"  paused_reason: {st.get('paused_reason') or '-'}"]
        if st.get("not_activated"):
            lines.append(f"  not activated: {', '.join(st['not_activated'])} saved in ZIA, waiting for activation in the portal")
    return lines

