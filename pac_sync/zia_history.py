"""`history.mode: zia`: no git. The ZIA PAC file versions are the history, `history.state_file` holds the rest.

ZiaHistory has the same methods as GitHistory, so `main.sync` does not care which one it drives:
- `read_state(code)` is derived from ZIA: pac_id by effective PAC name, `deployed` / `staged` from the DEPLOYED / STAGE
  versions (hash = sha256 of the normalized content, at = last modification time), same dict shape as state.json;
- `paused_reason`, `last_error`, `not_activated` and the drift acknowledgement are not in ZIA: they live in the
  git-ignored state file (atomic writes);
- `read_rules(code)` parses the PACR numbers from the `// <number> <ACTION>` comments of the deployed content (a
  contract with templates/proxy.pac.j2, tested in tests/test_renderer.py);
- commit / head / tag / push / discard do nothing; `diff_stat()` shows a unified diff of deployed vs the new render.
Assumption (unverified): ZIA returns pac_content unchanged apart from line endings and trailing whitespace.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .deployer import PacFileNotFound, ZiaDeployer, to_epoch
from .history import write_archive

log = logging.getLogger(__name__)

# State kept outside ZIA. Everything else in a state dict is derived from the PAC file versions.
OVERLAY_KEYS = ("paused_reason", "last_error", "not_activated", "drift_ack", "drift_accepted")
_RULE_COMMENT = re.compile(r";\s*// (\w+) (DIRECT|PROXY)\s*$", re.M)


def normalize_content(content: str) -> str:
    """CRLF -> LF, trailing whitespace stripped from every line and from the end."""
    return "\n".join(line.rstrip() for line in (content or "").replace("\r\n", "\n").split("\n")).rstrip()


def normalized_hash(content: str) -> str:
    return hashlib.sha256((normalize_content(content) + "\n").encode("utf-8")).hexdigest()


def parse_rules(content: str) -> List[dict]:
    """PACR numbers and actions from the rule comments of a rendered PAC."""
    return [{"number": n, "action": a.lower()} for n, a in _RULE_COMMENT.findall(content or "")]


class ZiaHistory:
    def __init__(self, deployer: ZiaDeployer, state_file: str, name_for: Callable[[str], str],
                 archive_dir: Optional[str] = None, known_codes: Optional[List[str]] = None):
        self.deployer = deployer
        self.state_file = Path(state_file)
        self.root = self.state_file.parent  # the run lock lives next to the state file
        self.name_for = name_for
        self.archive_dir = Path(archive_dir) if archive_dir else None
        self.known_codes = list(known_codes or [])
        self.push_enabled = False
        self._pending: Dict[str, str] = {}

    # -- state file ----------------------------------------------------------------------------------------------
    def _load(self) -> Dict[str, dict]:
        return json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.is_file() else {}

    def _save(self, data: Dict[str, dict]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pac-state-", suffix=".json", dir=self.state_file.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.state_file)

    # -- GitHistory interface ------------------------------------------------------------------------------------
    def ensure(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def plan_hash(self, content: str, digest: str) -> str:
        return normalized_hash(content)

    def codes(self) -> List[str]:
        return sorted(set(self.known_codes) | set(self._load()))

    def _pac_id(self, code: str) -> Optional[int]:
        try:
            return self.deployer.find_pac_id(self.name_for(code))
        except PacFileNotFound:
            return None

    def read_state(self, code: str) -> dict:
        st = dict(self._load().get(code, {}))
        pac_id = self._pac_id(code)
        if pac_id is None:
            return st
        st["pac_id"] = pac_id
        versions = self.deployer.versions(pac_id, with_content=True)
        for key, status in (("deployed", "DEPLOYED"), ("staged", "STAGE")):
            v = self.deployer._by_status(versions, status)
            if v is not None:
                when = to_epoch(getattr(v, "last_modified_time", None))
                st[key] = {"version": int(v.pac_version), "hash": normalized_hash(v.pac_content), "commit": None,
                           "at": datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds") if when else None}
        return st

    def read_rules(self, code: str) -> List[dict]:
        pac_id = self._pac_id(code)
        if pac_id is None:
            return []
        deployed = self.deployer._by_status(self.deployer.versions(pac_id, with_content=True), "DEPLOYED")
        return parse_rules(getattr(deployed, "pac_content", None) or "") if deployed is not None else []

    def read_pac(self, code: str) -> Optional[str]:
        pac_id = self._pac_id(code)
        deployed = self.deployer._by_status(self.deployer.versions(pac_id, True), "DEPLOYED") if pac_id else None
        return getattr(deployed, "pac_content", None) if deployed is not None else None

    def write_region(self, code: str, content: str, rules: List[dict]) -> bool:
        self._pending[code] = content
        return True

    def write_state(self, code: str, state: dict) -> None:
        data = self._load()
        keep = {k: state[k] for k in OVERLAY_KEYS if state.get(k) is not None}
        if keep:
            data[code] = keep
        else:
            data.pop(code, None)
        self._save(data)

    def update_state(self, code: str, change: Callable[[dict], None]) -> None:
        overlay = dict(self._load().get(code, {}))
        change(overlay)
        self.write_state(code, overlay)

    def diff_stat(self) -> str:
        parts = []
        for code, content in sorted(self._pending.items()):
            old = self.read_pac(code) or ""
            diff = list(difflib.unified_diff(normalize_content(old).splitlines(), normalize_content(content).splitlines(),
                                             f"{code} deployed", f"{code} render", lineterm="", n=2))
            if diff:
                parts.append("\n".join(diff))
        return "\n".join(parts)

    def archive_version(self, code: str, version) -> None:
        if self.archive_dir is None:
            log.warning("version v%s of %s is deleted from ZIA and not archived (history.archive_dir is not set)",
                        version.pac_version, code)
            return
        write_archive(self.archive_dir / code, version)

    def commit(self, message: str, everything: bool = False) -> Optional[str]:
        return None

    def discard(self) -> None:
        self._pending.clear()

    def head(self) -> str:
        return ""

    def tag(self, name: str, message: str) -> None:
        return None

    def push(self) -> None:
        return None
