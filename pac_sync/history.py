"""PAC history in a git repository.

Layout:
    regions/<CODE>/proxy.pac     rendered PAC (what ZIA receives)
    regions/<CODE>/rules.json    normalized rule snapshot (who, why, which PACR)
    regions/<CODE>/state.json    ZIA state: pac_id, last staged / deployed version + hash + commit

One commit per run that changes something; one tag per ZIA deployment: <CODE>-v<version>.
PAC Sync commits, diffs and discards only under regions/: other files in the repository are left alone.
With `history.mode: both`, every ZIA version PAC Sync is about to delete is first saved under
regions/<CODE>/zia-archive/v<N>.pac + v<N>.json and committed with the next state commit.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)
REGIONS = "regions"


def write_archive(folder: Path, version) -> Path:
    """Save a ZIA version (content + metadata) as v<N>.pac / v<N>.json in ``folder``. Used before it is deleted."""
    from .deployer import to_epoch

    folder.mkdir(parents=True, exist_ok=True)
    number = int(version.pac_version)
    (folder / f"v{number}.pac").write_text(getattr(version, "pac_content", None) or "", encoding="utf-8", newline="")
    by = getattr(version, "last_modified_by", None)
    meta = {
        "name": getattr(version, "name", None),
        "version": number,
        "status": getattr(version, "pac_version_status", None),
        "message": getattr(version, "pac_commit_message", None),
        "created": to_epoch(getattr(version, "create_time", None)),
        "modified": to_epoch(getattr(version, "last_modified_time", None)),
        "modified_by": (by.get("name") if isinstance(by, dict) else getattr(by, "name", None)),
    }
    (folder / f"v{number}.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return folder / f"v{number}.pac"


def redact(text: str) -> str:
    """Hide credentials embedded in remote URLs (https://user:token@host) before logging an error."""
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", text)


class GitHistory:
    def __init__(self, repo_path: str, author_name: str, author_email: str, remote: str = "origin", push: bool = False):
        self.root = Path(repo_path)
        self._ident = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}"]
        self.remote = remote
        self.push_enabled = push

    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *self._ident, *args], cwd=self.root, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {redact(proc.stderr.strip())}")
        return proc.stdout.strip()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").exists():
            return
        self._git("init")
        self._git("checkout", "-b", "main", check=False)
        (self.root / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".pac-sync.lock\n", encoding="utf-8")
        (self.root / "README.md").write_text(
            "# PAC history\n\nManaged by PAC Sync. Do not edit by hand: every file is regenerated from ServiceNow.\n",
            encoding="utf-8",
        )
        self.commit("Initialize PAC history repository", everything=True)

    def plan_hash(self, content: str, digest: str) -> str:
        """Hash used to compare a render with the recorded state: the render's own sha256 in git."""
        return digest

    def codes(self) -> List[str]:
        d = self.root / REGIONS
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def update_state(self, code: str, change) -> None:
        st = self.read_state(code)
        change(st)
        self.write_state(code, st)

    def archive_version(self, code: str, version) -> None:
        """`history.mode: both`: keep a ZIA version that is about to be deleted."""
        write_archive(self._dir(code) / "zia-archive", version)

    def _dir(self, code: str) -> Path:
        path = self.root / "regions" / code
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read_pac(self, code: str) -> Optional[str]:
        path = self.root / "regions" / code / "proxy.pac"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def read_rules(self, code: str) -> List[dict]:
        path = self.root / "regions" / code / "rules.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def read_state(self, code: str) -> dict:
        path = self.root / "regions" / code / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def write_region(self, code: str, content: str, rules: List[dict]) -> bool:
        """Write the PAC and its rule snapshot. Return True when the PAC changed."""
        changed = self.read_pac(code) != content
        d = self._dir(code)
        with open(d / "proxy.pac", "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        self._write_json(d / "rules.json", rules)
        return changed

    def write_state(self, code: str, state: dict) -> None:
        self._write_json(self._dir(code) / "state.json", state)

    @staticmethod
    def _write_json(path: Path, data) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

    def commit(self, message: str, everything: bool = False) -> Optional[str]:
        """Commit changes under regions/ only. Other files in the repo (e.g. snow-mock backups) are never touched;
        ``everything`` is for the initial commit made by ensure()."""
        if everything:
            self._git("add", "-A")
            if not self._git("status", "--porcelain"):
                return None
            self._git("commit", "-m", message)
            return self.head()
        if not (self.root / REGIONS).exists():
            return None
        self._git("add", "-A", "--", REGIONS)
        if not self._git("diff", "--cached", "--name-only", "--", REGIONS):
            return None
        self._git("commit", "-m", message, "--", REGIONS)
        return self.head()

    def diff_stat(self) -> str:
        if not (self.root / REGIONS).exists():
            return ""
        self._git("add", "-A", "--", REGIONS)
        return self._git("diff", "--cached", "--stat", "--", REGIONS)

    def discard(self) -> None:
        """Drop uncommitted changes under regions/ (used by --dry-run). Nothing outside regions/ is touched."""
        if not (self.root / REGIONS).exists():
            return
        self._git("reset", "-q", "--", REGIONS)
        if self._git("ls-tree", "-d", "HEAD", REGIONS, check=False):
            self._git("checkout", "-q", "HEAD", "--", REGIONS)
        self._git("clean", "-fdq", "--", REGIONS)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def tag(self, name: str, message: str) -> None:
        self._git("tag", "-a", "-f", name, "-m", message)

    def push(self) -> None:
        if self.push_enabled:
            self._git("push", self.remote, "HEAD")
            self._git("push", "--force", self.remote, "--tags")
