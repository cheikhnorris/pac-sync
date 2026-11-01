"""The commit message PAC Sync stores on every ZIA PAC version, and the parser that reads it back.

    PAC Sync <run-id> <CODE> [+<PACR>,<PACR>] [-<PACR>] sha256:<12>[ git:<10>]
    PAC Sync <run-id> <CODE> bootstrap|probe|rollback-to v<N> sha256:<12>

run-id is the UTC start of the run (YYYYMMDDTHHMMSSZ). The message is at most 255 characters (assumed ZIA limit);
long PACR lists collapse to `+12 (PACR0001001...)`. Messages written before Phase 1b (`PAC Sync <git10> PAC update...`)
parse as `legacy`; anything not starting with `PAC Sync` was written by a person in the ZIA portal.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional

MAX_LEN = 255
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
KINDS = ("bootstrap", "probe")
_HEAD = re.compile(r"^PAC Sync (\d{8}T\d{6}Z) ([A-Za-z0-9_-]+)(.*)$", re.S)
_LIST = re.compile(r"([+-])(?:(\d+) \(([^)]*)\)|([A-Za-z0-9_,-]+))")


def run_id(now: datetime) -> str:
    return now.strftime(RUN_ID_FORMAT)


def _fmt(sign: str, numbers: List[str], collapse: bool) -> str:
    if not numbers:
        return ""
    if collapse and len(numbers) > 1:
        return f" {sign}{len(numbers)} ({numbers[0]}...)"
    return f" {sign}{','.join(numbers)}"


def build_message(rid: str, code: str, added: List[str] = (), removed: List[str] = (), digest: str = "",
                  git: str = "", kind: str = "", rollback_to: Optional[int] = None) -> str:
    """Build the ZIA commit message. ``kind`` is bootstrap or probe; ``rollback_to`` makes a rollback message."""
    tail = f" sha256:{digest[:12]}" if digest else ""
    tail += f" git:{git[:10]}" if git else ""
    if rollback_to is not None:
        return f"PAC Sync {rid} {code} rollback-to v{rollback_to}{tail}"[:MAX_LEN]
    if kind in KINDS:
        return f"PAC Sync {rid} {code} {kind}{tail}"[:MAX_LEN]
    for collapse in (False, True):
        msg = f"PAC Sync {rid} {code}{_fmt('+', list(added), collapse)}{_fmt('-', list(removed), collapse)}{tail}"
        if len(msg) <= MAX_LEN:
            return msg
    return msg[:MAX_LEN]


def parse_commit_message(message: Optional[str]) -> Dict[str, object]:
    """Read a message back. Keys: pac_sync, legacy, run_id, code, added, removed, added_count, removed_count, kind,
    rollback_to, sha256, git. A message that is not PAC Sync's has pac_sync False."""
    text = (message or "").strip()
    out: Dict[str, object] = {"pac_sync": False, "legacy": False, "run_id": None, "code": None, "added": [],
                              "removed": [], "added_count": 0, "removed_count": 0, "kind": "", "rollback_to": None,
                              "sha256": None, "git": None}
    if not text.startswith("PAC Sync"):
        return out
    out["pac_sync"] = True
    head = _HEAD.match(text)
    if not head:
        out["legacy"] = True
        return out
    out["run_id"], out["code"] = head.group(1), head.group(2)
    rest = head.group(3)
    for tok in re.finditer(r"(?:^| )(sha256:([0-9a-f]+)|git:([0-9a-f]+)|bootstrap|probe|rollback-to v(\d+))(?= |$)", rest):
        if tok.group(2):
            out["sha256"] = tok.group(2)
        elif tok.group(3):
            out["git"] = tok.group(3)
        elif tok.group(4):
            out["rollback_to"] = int(tok.group(4))
        else:
            out["kind"] = tok.group(1)
    for m in _LIST.finditer(re.sub(r"sha256:\S+|git:\S+|rollback-to v\d+", "", rest)):
        key = "added" if m.group(1) == "+" else "removed"
        if m.group(2):  # collapsed list: count plus the first number
            out[key + "_count"] = int(m.group(2))
            out[key] = [m.group(3).rstrip(".")]
        else:
            out[key] = m.group(4).split(",")
            out[key + "_count"] = len(out[key])
    return out
