"""Compute the status updates PAC Sync writes back to ServiceNow u_pac_rule."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .normalizer import NormalizeResult

NOT_ACTIVATED = "saved in ZIA, not activated: other pending changes exist; review and activate in the portal"
NOT_ACTIVATED_RULE = "saved in ZIA as {versions}, not activated: activate in the portal; the next run confirms"


def region_status(state: dict, current_hash: str) -> str:
    if state.get("deployed", {}).get("hash") == current_hash:
        return "deployed"
    if state.get("staged", {}).get("hash") == current_hash:
        return "staged"
    return "pending"


def compute_rule_updates(
    result: NormalizeResult,
    raw_by_id: Dict[str, dict],
    states: Dict[str, dict],
    hashes: Dict[str, str],
    now_str: str,
    failed: Optional[Dict[str, str]] = None,
    not_activated: Optional[Iterable[str]] = None,
) -> List[Tuple[str, dict]]:
    """Return (sys_id, PATCH body) pairs, only for records whose status actually changes.

    ``failed`` maps region code -> error of a stage/deploy that failed in this run: every rule landing in such a
    region becomes failed with that message (rules deployed elsewhere stay coherent: failed if ANY region failed).
    ``not_activated``: region codes whose version is saved in ZIA but not activated (deploy.skip_activate_if_pending).
    Their rules stay "staged" with a message and keep u_state (the request stays open) until a later run finds ZIA
    active and confirms the version is live; only rules of those regions get the note.
    """
    failed = failed or {}
    pending = set(not_activated or ())
    status = {code: region_status(states.get(code, {}), h) for code, h in hashes.items()}
    updates: List[Tuple[str, dict]] = []

    for rule in result.all_rules:
        rec = raw_by_id.get(rule.sys_id, {})
        bad = [c for c in rule.regions if c in failed]
        if bad:
            body = {"u_deploy_status": "failed",
                    "u_deploy_message": "; ".join(f"{c}: {failed[c]}" for c in bad)[:1000]}
            if _differs(rec, body):
                updates.append((rule.sys_id, body))
            continue
        per_region = [status.get(c, "pending") for c in rule.regions]
        waiting = [c for c, s in zip(rule.regions, per_region) if c in pending and s in ("staged", "deployed")]
        if waiting and all(s in ("staged", "deployed") for s in per_region):
            versions = ", ".join(f"{c} v{(states[c].get('deployed') if status[c] == 'deployed' else states[c].get('staged'))['version']}"
                                 for c in waiting)
            body = {"u_deploy_status": "staged", "u_deploy_message": NOT_ACTIVATED_RULE.format(versions=versions)}
        elif all(s == "deployed" for s in per_region):
            version = ",".join(f"{c}:v{states[c]['deployed']['version']}" for c in rule.regions)
            body = {"u_deploy_status": "deployed", "u_deploy_version": version, "u_deploy_message": ""}
            if rec.get("u_state") == "approved":
                body["u_state"] = "deployed"
        elif all(s in ("staged", "deployed") for s in per_region):
            staged = [f"{c} v{states[c]['staged']['version']}" for c, s in zip(rule.regions, per_region) if s == "staged"]
            message = f"Staged as {', '.join(staged)}; waiting for promotion"
            body = {"u_deploy_status": "staged", "u_deploy_message": message}
        else:
            continue
        if _differs(rec, body):
            body["u_deployed_on"] = now_str
            updates.append((rule.sys_id, body))

    for rec, reason in result.rejected:
        if rec.get("u_deploy_status") != "failed":
            updates.append((rec["sys_id"], {"u_deploy_status": "failed", "u_deploy_message": reason[:1000]}))

    # Retired or expired rules are gone once every region's live PAC is the current render, and activated.
    all_live = bool(status) and all(s == "deployed" for s in status.values()) and not pending
    if all_live:
        for rec in result.retiring + result.expired:
            body = {"u_deploy_status": "removed", "u_state": "retired"}
            if _differs(rec, body):
                body.update({"u_deployed_on": now_str})
                updates.append((rec["sys_id"], body))
    return updates


def _differs(rec: dict, body: dict) -> bool:
    return any((rec.get(k) or "") != (v or "") for k, v in body.items())
