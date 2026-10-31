"""PAC history in a git repository.

Layout:
    regions/<CODE>/proxy.pac     rendered PAC (what ZIA receives)
    regions/<CODE>/rules.json    normalized rule snapshot (who, why, which PACR)
    regions/<CODE>/state.json    ZIA state: pac_id, last staged / deployed version + hash + commit

One commit per run that changes something; one tag per ZIA deployment: <CODE>-v<version>.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


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
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
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
        self.commit("Initialize PAC history repository")

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

    def commit(self, message: str) -> Optional[str]:
        self._git("add", "-A")
        if not self._git("status", "--porcelain"):
            return None
        self._git("commit", "-m", message)
        return self.head()

    def diff_stat(self) -> str:
        self._git("add", "-A")
        return self._git("diff", "--cached", "--stat")

    def discard(self) -> None:
        """Drop uncommitted changes (used by --dry-run)."""
        self._git("reset", "-q", "--hard", "HEAD")
        self._git("clean", "-fdq")

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def tag(self, name: str, message: str) -> None:
        self._git("tag", "-a", "-f", name, "-m", message)

    def push(self) -> None:
        if self.push_enabled:
            self._git("push", self.remote, "HEAD")
            self._git("push", "--force", self.remote, "--tags")
