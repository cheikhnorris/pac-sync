from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Tuple

ACTIONS = ("direct", "proxy")
MATCH_TYPES = ("host_exact", "domain_suffix", "ip_cidr", "url_pattern")

ACTIVE_STATES = ("approved", "deployed")
RETIRE_STATE = "retire_requested"


@dataclass(frozen=True)
class Region:
    sys_id: str
    code: str
    zia_pac_name: str
    default_chain: str


@dataclass(frozen=True)
class ProxyProfile:
    sys_id: str
    name: str
    chain: str


@dataclass(frozen=True)
class PacRule:
    """One validated ServiceNow rule, ready to render."""

    sys_id: str
    number: str
    action: str
    match_type: str
    value: str
    chain: str  # PAC return value: "DIRECT" or the proxy profile chain
    profile: str  # proxy profile name, "" for DIRECT
    regions: Tuple[str, ...]  # region codes the rule lands in
    priority: int
    state: str

    def sort_key(self) -> tuple:
        # Deterministic order: same input -> same PAC -> clean git diffs and stable hash.
        return (
            self.priority,
            ACTIONS.index(self.action),
            MATCH_TYPES.index(self.match_type),
            self.value,
            self.number,
        )

    def to_json(self) -> dict:
        data = asdict(self)
        data["regions"] = list(self.regions)
        return data
