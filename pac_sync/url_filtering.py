"""URL filtering use case: one `u_url_category` becomes a custom ZIA URL category plus one URL filtering rule.

Both carry the name `zscaler.pac_name_prefix` + category name; the rule applies the category's action (ALLOW / CAUTION /
BLOCK) to that category only. The entries of the category are the URLs of the approved `u_url_rule` records.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .deployer import _unwrap
from .normalizer import validate_url_entry
from .zia_objects import UseCase, ZGroup

ACTIONS = ("allow", "caution", "block")


def _validate_group(group: dict) -> Optional[str]:
    action = (group.get("u_action") or "").strip().lower()
    return None if action in ACTIONS else f"unknown action '{action}'"


def _validate_entry(rec: dict) -> Tuple[str, Optional[str]]:
    value = (rec.get("u_value") or "").strip().lower()
    return value, validate_url_entry(value)


def _canonical(name: str, group: dict, values: List[str]) -> dict:
    return {"action": group["u_action"].strip().upper(), "state": "ENABLED", "categories": [name], "urls": sorted(values)}


class UrlFilteringApi:
    """The SDK calls (client.zia.url_categories / url_filtering). Every write calls ``guard(name, what)`` first."""

    def __init__(self, client, guard: Callable[[str, str], None], prefix: str = "", ucfg: Optional[dict] = None):
        self.zia = client.zia
        self.guard = guard
        self.prefix = prefix
        self.ucfg = ucfg or {}

    def read(self) -> Dict[str, ZGroup]:
        cats = _unwrap(self.zia.url_categories.list_categories(query_params={"custom_only": True}), "list_categories") or []
        rules = _unwrap(self.zia.url_filtering.list_rules(), "list_rules") or []
        ids = {c.id: c.configured_name for c in cats}
        mine_c = {c.configured_name: c for c in cats if str(c.configured_name).startswith(self.prefix)}
        mine_r = {r.name: r for r in rules if str(r.name).startswith(self.prefix)}
        out = {}
        for name in sorted(set(mine_c) | set(mine_r)):
            cat, rule = mine_c.get(name), mine_r.get(name)
            canonical = None
            if cat is not None and rule is not None:
                canonical = {"action": rule.action, "state": rule.state,
                             "categories": sorted(ids.get(i, i) for i in rule.url_categories or []),
                             "urls": sorted(cat.urls or [])}
            out[name] = ZGroup(name, sorted(cat.urls or []) if cat is not None else [], canonical,
                               [o.description or "" for o in (cat, rule) if o is not None], (cat, rule))
        return out

    def _add_category(self, name: str, urls: List[str], message: str):
        self.guard(name, "URL category")
        return _unwrap(self.zia.url_categories.add_url_category(
            super_category="USER_DEFINED", urls=urls, configured_name=name, custom_category=True,
            description=message), "add_url_category")

    def _rule_fields(self, group: dict, cat_id, message: str) -> dict:
        return dict(action=group["u_action"].strip().upper(), state="ENABLED", protocols=["ANY_RULE"],
                    url_categories=[cat_id], description=message)

    def create(self, name: str, group: dict, urls: List[str], message: str) -> None:
        cat = self._add_category(name, urls, message)
        self.guard(name, "URL filtering rule")
        extra = {"order": self.ucfg["rule_order"]} if self.ucfg.get("rule_order") else {}
        _unwrap(self.zia.url_filtering.add_rule(name=name, rank=self.ucfg.get("rule_rank", 7),
                                                **self._rule_fields(group, cat.id, message), **extra), "add_rule")

    def update(self, zg: ZGroup, group: dict, urls: List[str], message: str) -> None:
        cat, rule = zg.handles
        if cat is None:  # half-created group: complete it
            cat = self._add_category(zg.name, urls, message)
        else:
            self.guard(cat.configured_name, "URL category")
            _unwrap(self.zia.url_categories.update_url_category(
                cat.id, configured_name=cat.configured_name, super_category=cat.super_category or "USER_DEFINED",
                urls=urls, description=message, custom_category=True), "update_url_category")
        if rule is None:
            self.guard(zg.name, "URL filtering rule")
            _unwrap(self.zia.url_filtering.add_rule(name=zg.name, rank=self.ucfg.get("rule_rank", 7),
                                                    **self._rule_fields(group, cat.id, message)), "add_rule")
            return
        self.guard(rule.name, "URL filtering rule")
        keep = {k: v for k, v in (("order", rule.order), ("rank", rule.rank)) if v is not None}
        _unwrap(self.zia.url_filtering.update_rule(rule.id, name=rule.name, **keep,
                                                   **self._rule_fields(group, cat.id, message)), "update_rule")

    def delete(self, zg: ZGroup) -> None:
        cat, rule = zg.handles
        if rule is not None:  # the rule references the category, so it goes first
            self.guard(rule.name, "URL filtering rule")
            _unwrap(self.zia.url_filtering.delete_rule(rule.id), "delete_rule")
        if cat is not None:
            self.guard(cat.configured_name, "URL category")
            _unwrap(self.zia.url_categories.delete_category(cat.id), "delete_category")


URL_FILTERING = UseCase(
    key="url_filtering", label="URL category", group_table="url_category", entry_table="url_rule",
    group_fields="sys_id,u_name,u_action,u_active",
    entry_fields=("sys_id,number,u_category,u_value,u_state,u_valid_from,u_valid_to,u_deploy_status,u_deploy_version,"
                  "u_deploy_message,sys_updated_on"),
    group_ref="u_category", validate_group=_validate_group, validate_entry=_validate_entry, canonical=_canonical,
    make_api=UrlFilteringApi)
