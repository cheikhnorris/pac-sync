"""Traffic forwarding use case: one `u_fwd_profile` becomes one ZIA forwarding control rule.

The rule is named `zscaler.pac_name_prefix` + profile name (at most 31 characters in all, ZIA's limit), forwards DIRECT or
through a PROXYCHAIN proxy gateway, and its `dest_addresses` are the approved `u_fwd_rule` entries (hosts or IPv4 CIDRs).
The proxy gateway is only read, never created: a profile naming a gateway ZIA does not have fails its own group.
Needs zscaler.auth: oneapi (the legacy client has no forwarding_control).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .deployer import DeployError, _unwrap
from .normalizer import validate_gateway_name, validate_value
from .zia_objects import UseCase, ZGroup

METHODS = ("direct", "proxychain")
MATCH_TYPES = ("host_exact", "ip_cidr")


def _validate_group(group: dict) -> Optional[str]:
    method = (group.get("u_forward_method") or "").strip().lower()
    if method not in METHODS:
        return f"unknown forward method '{method}'"
    gateway = (group.get("u_proxy_gateway") or "").strip()
    if method == "direct":
        return "a direct profile must not name a proxy gateway" if gateway else None
    return validate_gateway_name(gateway) if gateway else "a proxychain profile needs a proxy gateway"


def _validate_entry(rec: dict) -> Tuple[str, Optional[str]]:
    match_type = (rec.get("u_match_type") or "").strip().lower()
    value = (rec.get("u_value") or "").strip().lower()
    if match_type not in MATCH_TYPES:
        return value, f"unknown match type '{match_type}'"
    return value, validate_value(match_type, value)


def _gateway(group: dict) -> str:
    proxychain = (group.get("u_forward_method") or "").strip().lower() == "proxychain"
    return (group.get("u_proxy_gateway") or "").strip() if proxychain else ""


def _canonical(name: str, group: dict, values: List[str]) -> dict:
    return {"forward_method": group["u_forward_method"].strip().upper(), "proxy_gateway": _gateway(group),
            "state": "ENABLED", "dest_addresses": sorted(values)}


class ForwardingApi:
    """The SDK calls (client.zia.forwarding_control, client.zia.proxies). Every write calls ``guard(name, what)`` first."""

    def __init__(self, client, guard: Callable[[str, str], None], prefix: str = "", ucfg: Optional[dict] = None):
        self.zia = client.zia
        self.guard = guard
        self.prefix = prefix
        self.ucfg = ucfg or {}
        self._gateways: Optional[Dict[str, object]] = None

    def read(self) -> Dict[str, ZGroup]:
        rules = _unwrap(self.zia.forwarding_control.list_rules(), "list_rules") or []
        out = {}
        for rule in sorted((r for r in rules if str(r.name).startswith(self.prefix)), key=lambda r: r.name):
            gateway = getattr(rule.proxy_gateway, "name", None) or ""
            addresses = sorted(rule.dest_addresses or [])
            out[rule.name] = ZGroup(rule.name, addresses, {
                "forward_method": rule.forward_method, "proxy_gateway": gateway, "state": rule.state,
                "dest_addresses": addresses}, [rule.description or ""], (rule,))
        return out

    def gateway(self, name: str):
        """The proxy gateway with exactly this name; DeployError when ZIA has none."""
        if self._gateways is None:
            found = _unwrap(self.zia.proxies.list_proxy_gateways(), "list_proxy_gateways") or []
            self._gateways = {g.name: g for g in found}
        if name not in self._gateways:
            raise DeployError(f"ZIA has no proxy gateway named '{name}'")
        return self._gateways[name]

    def check(self, groups: List[dict]) -> List[Tuple[str, str]]:
        """Doctor: (label, error) for every proxychain profile whose gateway ZIA does not have."""
        out = []
        for g in groups:
            if _gateway(g):
                try:
                    self.gateway(_gateway(g))
                except DeployError as exc:
                    out.append((f"{g.get('u_name')}", str(exc)))
        return out

    def _fields(self, group: dict, values: List[str], message: str) -> dict:
        fields = dict(type="FORWARDING", forward_method=group["u_forward_method"].strip().upper(), state="ENABLED",
                      dest_addresses=sorted(values), description=message)
        if _gateway(group):
            gw = self.gateway(_gateway(group))
            fields["proxy_gateway"] = {"id": gw.id, "name": gw.name}
        return fields

    def create(self, name: str, group: dict, values: List[str], message: str) -> None:
        self.guard(name, "forwarding rule")
        extra = {"order": self.ucfg["rule_order"]} if self.ucfg.get("rule_order") else {}
        _unwrap(self.zia.forwarding_control.add_rule(
            name=name, rank=self.ucfg.get("rule_rank", 7), **self._fields(group, values, message), **extra), "add_rule")

    def update(self, zg: ZGroup, group: dict, values: List[str], message: str) -> None:
        (rule,) = zg.handles
        self.guard(rule.name, "forwarding rule")
        keep = {k: v for k, v in (("order", rule.order), ("rank", rule.rank)) if v is not None}
        _unwrap(self.zia.forwarding_control.update_rule(
            rule.id, name=rule.name, **keep, **self._fields(group, values, message)), "update_rule")

    def delete(self, zg: ZGroup) -> None:
        (rule,) = zg.handles
        self.guard(rule.name, "forwarding rule")
        _unwrap(self.zia.forwarding_control.delete_rule(rule.id), "delete_rule")


FORWARDING = UseCase(
    key="forwarding", label="forwarding profile", group_table="fwd_profile", entry_table="fwd_rule",
    group_fields="sys_id,u_name,u_forward_method,u_proxy_gateway,u_active",
    entry_fields=("sys_id,number,u_profile,u_match_type,u_value,u_state,u_valid_from,u_valid_to,u_deploy_status,"
                  "u_deploy_version,u_deploy_message,sys_updated_on"),
    group_ref="u_profile", validate_group=_validate_group, validate_entry=_validate_entry, canonical=_canonical,
    make_api=ForwardingApi)
