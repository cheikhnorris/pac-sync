"""Compute the status updates PAC Sync writes back to ServiceNow u_pac_rule."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .normalizer import NormalizeResult


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
) -> List[Tuple[str, dict]]:
    """Return (sys_id, PATCH body) pairs, only for records whose status actually changes."""
    status = {code: region_status(states.get(code, {}), h) for code, h in hashes.items()}
    updates: List[Tuple[str, dict]] = []

    for rule in result.all_rules:
        rec = raw_by_id.get(rule.sys_id, {})
        per_region = [status.get(c, "pending") for c in rule.regions]
        if all(s == "deployed" for s in per_region):
            version = ",".join(f"{c}:v{states[c]['deployed']['version']}" for c in rule.regions)
            body = {"u_deploy_status": "deployed", "u_deploy_version": version}
            if rec.get("u_state") == "approved":
                body["u_state"] = "deployed"
        elif all(s in ("staged", "deployed") for s in per_region):
            body = {"u_deploy_status": "staged"}
        else:
            continue
        if _differs(rec, body):
            body.update({"u_deployed_on": now_str, "u_deploy_message": ""})
            updates.append((rule.sys_id, body))

    for rec, reason in result.rejected:
        if rec.get("u_deploy_status") != "failed":
            updates.append((rec["sys_id"], {"u_deploy_status": "failed", "u_deploy_message": reason[:1000]}))

    # Retired or expired rules are gone once every region's live PAC is the current render.
    all_live = bool(status) and all(s == "deployed" for s in status.values())
    if all_live:
        for rec in result.retiring + result.expired:
            body = {"u_deploy_status": "removed", "u_state": "retired"}
            if _differs(rec, body):
                body.update({"u_deployed_on": now_str})
                updates.append((rec["sys_id"], body))
    return updates


def _differs(rec: dict, body: dict) -> bool:
    return any(rec.get(k) != v for k, v in body.items())
