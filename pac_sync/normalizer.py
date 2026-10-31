"""Turn raw ServiceNow records into validated PacRule objects, per region.

ServiceNow enforces the same rules (V1-V12 in the ServiceNow requirements doc);
they are re-checked here so a bad record can never reach a PAC file.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .models import ACTIONS, ACTIVE_STATES, MATCH_TYPES, RETIRE_STATE, PacRule, ProxyProfile, Region

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
HOST_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
URL_RE = re.compile(r"^https?://[a-z0-9.*\-]+(:\d{1,5})?(/[^\s\"';\\]*)?$")
FORBIDDEN_CHARS = set("\"';\\\r\n\t ")
# Suffixes that must never be sent DIRECT or PROXY as a whole (V3). Extend as needed.
PUBLIC_SUFFIXES = {
    "com", "net", "org", "io", "info", "biz", "local", "corp",
    "co.uk", "org.uk", "com.au", "co.jp", "com.br", "co.in",
    "de", "fr", "es", "it", "nl", "be", "ch", "uk", "us", "ma",
}
SNOW_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class NormalizeResult:
    rules_by_region: Dict[str, List[PacRule]]
    all_rules: List[PacRule] = field(default_factory=list)
    rejected: List[Tuple[dict, str]] = field(default_factory=list)  # (raw record, reason)
    expired: List[dict] = field(default_factory=list)  # valid_to passed
    retiring: List[dict] = field(default_factory=list)  # state retire_requested
    future: List[dict] = field(default_factory=list)  # valid_from not reached yet


def parse_snow_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, SNOW_DT_FORMAT).replace(tzinfo=timezone.utc)


def validate_value(match_type: str, value: str) -> Optional[str]:
    """Return an error message, or None when the value is valid for its match type."""
    if not value:
        return "empty value"
    if FORBIDDEN_CHARS & set(value):
        return "value contains a forbidden character (quote, semicolon, backslash or whitespace)"

    if match_type == "host_exact":
        if not HOST_RE.match(value):
            return "host_exact must be a plain host name (no scheme, port, path or wildcard)"
    elif match_type == "domain_suffix":
        if not HOST_RE.match(value) or "." not in value:
            return "domain_suffix must be a domain with at least two labels, without '*' or leading '.'"
        if value in PUBLIC_SUFFIXES:
            return f"domain_suffix '{value}' is a public suffix"
    elif match_type == "ip_cidr":
        try:
            net = ipaddress.IPv4Network(value, strict=True)
        except ValueError as exc:
            return f"invalid IPv4 CIDR: {exc}"
        if not 8 <= net.prefixlen <= 32:
            return "ip_cidr prefix must be between /8 and /32"
    elif match_type == "url_pattern":
        if not URL_RE.match(value):
            return "url_pattern must start with http:// or https:// and contain no quotes, ';' or spaces"
        if value.count("*") > 3:
            return "url_pattern allows at most 3 '*'"
    else:
        return f"unknown match type '{match_type}'"
    return None


def normalize(
    raw_rules: List[dict],
    regions: Dict[str, Region],
    profiles: Dict[str, ProxyProfile],
    now: Optional[datetime] = None,
) -> NormalizeResult:
    """Validate records and group them per region code.

    ``regions`` and ``profiles`` are keyed by ServiceNow sys_id and contain active records only.
    """
    now = now or datetime.now(timezone.utc)
    result = NormalizeResult(rules_by_region={r.code: [] for r in regions.values()})
    candidates: List[Tuple[PacRule, dict]] = []

    for rec in raw_rules:
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

        rule, error = _build_rule(rec, regions, profiles)
        if error:
            result.rejected.append((rec, error))
        else:
            candidates.append((rule, rec))

    # Duplicates and conflicts (V8, V9): within a region, the same match type + value
    # may appear once. The lowest (priority, number) wins; the others are rejected everywhere.
    candidates.sort(key=lambda c: (c[0].priority, c[0].number))
    winners: Dict[Tuple[str, str, str], PacRule] = {}
    accepted: List[PacRule] = []
    for rule, rec in candidates:
        clash = None
        for code in rule.regions:
            other = winners.get((code, rule.match_type, rule.value))
            if other:
                clash = other
                break
        if clash:
            kind = "duplicate of" if clash.chain == rule.chain else "conflicts with"
            result.rejected.append((rec, f"{kind} {clash.number} ({rule.match_type} {rule.value})"))
            continue
        for code in rule.regions:
            winners[(code, rule.match_type, rule.value)] = rule
        accepted.append(rule)

    for rule in sorted(accepted, key=PacRule.sort_key):
        result.all_rules.append(rule)
        for code in rule.regions:
            result.rules_by_region[code].append(rule)
    return result


def _build_rule(
    rec: dict, regions: Dict[str, Region], profiles: Dict[str, ProxyProfile]
) -> Tuple[Optional[PacRule], Optional[str]]:
    action = (rec.get("u_action") or "").strip().lower()
    match_type = (rec.get("u_match_type") or "").strip().lower()
    value = (rec.get("u_value") or "").strip().lower()

    if action not in ACTIONS:
        return None, f"unknown action '{action}'"
    if match_type not in MATCH_TYPES:
        return None, f"unknown match type '{match_type}'"
    error = validate_value(match_type, value)
    if error:
        return None, error

    profile_id = rec.get("u_proxy_profile") or ""
    if action == "proxy":
        profile = profiles.get(profile_id)
        if not profile:
            return None, "proxy rule without an active proxy profile"
        chain, profile_name = profile.chain, profile.name
    else:
        if profile_id:
            return None, "direct rule must not reference a proxy profile"
        chain, profile_name = "DIRECT", ""

    try:
        priority = int(rec.get("u_priority") or 500)
    except ValueError:
        return None, f"invalid priority '{rec.get('u_priority')}'"
    if not 1 <= priority <= 999:
        return None, "priority must be between 1 and 999"

    scope = (rec.get("u_scope") or "").strip().lower()
    if scope == "global":
        codes = sorted(r.code for r in regions.values())
    elif scope == "regional":
        ids = [s.strip() for s in (rec.get("u_regions") or "").split(",") if s.strip()]
        codes = sorted({regions[i].code for i in ids if i in regions})
        if not codes:
            return None, "regional rule without any active region"
    else:
        return None, f"unknown scope '{scope}'"

    return (
        PacRule(
            sys_id=rec.get("sys_id", ""),
            number=rec.get("number", ""),
            action=action,
            match_type=match_type,
            value=value,
            chain=chain,
            profile=profile_name,
            regions=tuple(codes),
            priority=priority,
            state=rec.get("u_state", ""),
        ),
        None,
    )
