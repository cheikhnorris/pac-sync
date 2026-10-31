"""Render one PAC file per region with Jinja2."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from typing import List, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import PacRule, Region


def js(value: str) -> str:
    """Quote a value as a JavaScript string literal."""
    return json.dumps(value)


def _net_mask(cidr: str) -> Tuple[str, str]:
    net = ipaddress.IPv4Network(cidr, strict=True)
    return str(net.network_address), str(net.netmask)


def condition(rule: PacRule) -> str:
    v = rule.value
    if rule.match_type == "host_exact":
        return f"h === {js(v)}"
    if rule.match_type == "domain_suffix":
        return f'h === {js(v)} || dnsDomainIs(h, {js("." + v)})'
    if rule.match_type == "url_pattern":
        return f"shExpMatch(url, {js(v)})"
    if rule.match_type == "ip_cidr":
        net, mask = _net_mask(v)
        return f"isInNetSafe({js(net)}, {js(mask)})"
    raise ValueError(f"unknown match type {rule.match_type}")


class PacRenderer:
    def __init__(self, template_path: str, direct_plain_hostnames: bool = True, direct_networks: List[str] = ()):
        path = Path(template_path)
        self._env = Environment(
            loader=FileSystemLoader(str(path.parent)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            autoescape=False,
        )
        self._template = self._env.get_template(path.name)
        self._plain = direct_plain_hostnames
        self._networks = [dict(zip(("net", "mask"), map(js, _net_mask(n)))) for n in direct_networks]

    def render(self, region: Region, rules: List[PacRule]) -> Tuple[str, str]:
        """Return (pac_content, sha256_hex). ``rules`` must already be sorted."""
        content = self._template.render(
            region=region,
            rules=[
                {"condition": condition(r), "chain": js(r.chain), "number": r.number, "action": r.action}
                for r in rules
            ],
            direct_plain_hostnames=self._plain,
            direct_networks=self._networks,
            default_chain=js(region.default_chain),
        )
        return content, hashlib.sha256(content.encode("utf-8")).hexdigest()
