"""Simulated ZIA tenant for demos and tests: same surface as the SDK's `client.zia.pac_files` and `client.zia.activate`.

`zscaler.target: simulated` makes `make_zscaler_client` return a SimulatedZscalerClient, so the real ZiaDeployer
logic (unstage, clone, deploy, version fallback, bootstrap) runs without a tenant. State lives in
`<state_dir>/pacfiles.json`; every version is also written to `<state_dir>/<pac name>/v<N>.pac` and the deployed one
to `<state_dir>/<pac name>/deployed.pac`, so the demo can open the file ZIA would serve.

Semantics copied from the fake in tests/test_deployer_sdk.py (not verified against a tenant):
- at most 10 versions per PAC file; a clone at the cap evicts the oldest version that is not DEPLOYED;
- DEPLOY demotes the previous DEPLOYED version to LKG; UNSTAGE sets UNSTAGED;
- every call returns the SDK's (value, response, error) tuple.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_VERSIONS = 10
_FILE_FIELDS = ("id", "name", "domain", "description")


class SimObject:
    """Attribute access like the SDK models, plus .get() and .as_dict()."""

    def __init__(self, data: Dict[str, Any]):
        self.__dict__.update(data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    def __repr__(self) -> str:
        shown = {k: v for k, v in self.__dict__.items() if k != "pac_content"}
        return f"SimObject({shown})"


def safe_dir_name(name: str) -> str:
    """PAC names come from ServiceNow: keep them inside state_dir (no separators, no '..')."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") or "_"
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimPacFilesAPI:
    def __init__(self, store: "SimStore"):
        self._store = store

    # -- reads -------------------------------------------------------------------------------------------------
    def list_pac_files(self, query_params: Optional[dict] = None):
        params = query_params or {}
        search = str(params.get("search") or "").lower()
        no_content = params.get("filter") == "pac_content"
        state = self._store.load()
        out = []
        for f in state["files"].values():
            if search and search not in f["name"].lower():
                continue
            current = _current(f)
            out.append(_obj(f, current, with_content=not no_content))
        return out, None, None

    def get_pac_file(self, pac_id: int, query_params: Optional[dict] = None):
        f = self._store.load()["files"].get(str(pac_id))
        if f is None:
            return None, None, f"PAC file {pac_id} not found"
        no_content = (query_params or {}).get("filter") == "pac_content"
        versions = sorted(f["versions"], key=lambda v: v["pac_version"])
        return [_obj(f, v, with_content=not no_content) for v in versions], None, None

    def validate_pac_file(self, pac_file_content: str):
        ok = "function FindProxyForURL" in (pac_file_content or "")
        message = "PAC file is valid" if ok else "missing function FindProxyForURL(url, host)"
        return SimObject({"success": ok, "message": message, "severity": None if ok else "ERROR"}), None, None

    # -- writes ------------------------------------------------------------------------------------------------
    def add_pac_file(self, **kwargs):
        content = kwargs.get("pac_content")
        if not content:
            raise ValueError("Missing required parameter: pac_content")
        result, _, _ = self.validate_pac_file(content)
        if not result.success:
            raise Exception(f"PAC content validation failed: {result.message}")
        name = kwargs.get("name") or ""
        if not name:
            return None, None, "name is required"
        state = self._store.load()
        if any(f["name"] == name for f in state["files"].values()):
            return None, None, f"a PAC file named '{name}' already exists"
        pac_id = state["next_id"]
        state["next_id"] += 1
        version = {
            "pac_version": 1,
            "pac_version_status": kwargs.get("pac_version_status") or "DEPLOYED",
            "pac_commit_message": kwargs.get("pac_commit_message") or "",
            "pac_verification_status": kwargs.get("pac_verification_status") or "VERIFY_NOERR",
            "pac_content": content,
            "last_modification_time": _now(),
        }
        f = {"id": pac_id, "name": name, "domain": kwargs.get("domain") or "", "description": kwargs.get("description"),
             "versions": [version]}
        state["files"][str(pac_id)] = f
        self._store.save(state)
        self._store.write_version(f, version)
        if version["pac_version_status"] == "DEPLOYED":
            self._store.write_deployed(f, version)
        return _obj(f, version), None, None

    def clone_pac_file(self, pac_id: int, pac_version: str, pac_content: str, pac_version_status: str,
                       pac_verification_status: str = "VERIFY_NOERR", pac_commit_message: str = "",
                       delete_version: Optional[int] = None):
        if not pac_content:
            raise ValueError("Missing required parameter: pac_content")
        result, _, _ = self.validate_pac_file(pac_content)
        if not result.success:
            raise Exception(f"PAC content validation failed: {result.message}")
        state = self._store.load()
        f = state["files"].get(str(pac_id))
        if f is None:
            return None, None, f"PAC file {pac_id} not found"
        if not any(v["pac_version"] == int(pac_version) for v in f["versions"]):
            return None, None, f"PAC file {pac_id} has no version {pac_version}"
        if len(f["versions"]) >= MAX_VERSIONS:
            evictable = [v for v in f["versions"] if v["pac_version_status"] != "DEPLOYED"]
            if delete_version is not None:
                evictable = [v for v in evictable if v["pac_version"] == int(delete_version)]
            if not evictable:
                return None, None, "no version can be evicted"
            gone = min(evictable, key=lambda v: v["pac_version"])
            f["versions"].remove(gone)
            self._store.remove_version(f, gone)
        new = {
            "pac_version": max([v["pac_version"] for v in f["versions"]] + [state["max_version"].get(str(pac_id), 0)]) + 1,
            "pac_version_status": pac_version_status,
            "pac_commit_message": pac_commit_message or "",
            "pac_verification_status": pac_verification_status,
            "pac_content": pac_content,
            "last_modification_time": _now(),
        }
        f["versions"].append(new)
        state["max_version"][str(pac_id)] = new["pac_version"]
        self._store.save(state)
        self._store.write_version(f, new)
        return _obj(f, new), None, None

    def update_pac_file(self, pac_id: int, pac_version: int, pac_version_action: Optional[str] = None, **kwargs):
        state = self._store.load()
        f = state["files"].get(str(pac_id))
        if f is None:
            return None, None, f"PAC file {pac_id} not found"
        target = next((v for v in f["versions"] if v["pac_version"] == int(pac_version)), None)
        if target is None:
            return None, None, f"PAC file {pac_id} has no version {pac_version}"
        if pac_version_action == "DEPLOY":
            for v in f["versions"]:
                if v["pac_version_status"] == "DEPLOYED" and v is not target:
                    v["pac_version_status"] = "LKG"
            target["pac_version_status"] = "DEPLOYED"
        elif pac_version_action == "UNSTAGE":
            target["pac_version_status"] = "UNSTAGED"
        elif pac_version_action:
            target["pac_version_status"] = pac_version_action
        target["last_modification_time"] = _now()
        self._store.save(state)
        if target["pac_version_status"] == "DEPLOYED":
            self._store.write_deployed(f, target)
        return _obj(f, target), None, None


class SimActivateAPI:
    def __init__(self, store: "SimStore"):
        self._store = store

    def activate(self):
        state = self._store.load()
        state["activations"] = state.get("activations", 0) + 1
        self._store.save(state)
        return SimObject({"status": "ACTIVE"}), None, None

    def status(self):
        return SimObject({"status": "ACTIVE"}), None, None


class SimStore:
    """JSON state file with atomic writes (temp file + os.replace)."""

    def __init__(self, state_dir: str):
        self.root = Path(state_dir)
        self.path = self.root / "pacfiles.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"next_id": 1, "files": {}, "max_version": {}, "activations": 0}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state.setdefault("max_version", {})
        return state

    def save(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pacfiles-", suffix=".json", dir=self.root)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def _dir(self, f: Dict[str, Any]) -> Path:
        d = self.root / safe_dir_name(f["name"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write(self, path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".pac-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:  # byte-identical to what was sent
            fh.write(content)
        os.replace(tmp, path)

    def write_version(self, f: Dict[str, Any], v: Dict[str, Any]) -> None:
        self._write(self._dir(f) / f"v{v['pac_version']}.pac", v["pac_content"])

    def remove_version(self, f: Dict[str, Any], v: Dict[str, Any]) -> None:
        (self._dir(f) / f"v{v['pac_version']}.pac").unlink(missing_ok=True)

    def write_deployed(self, f: Dict[str, Any], v: Dict[str, Any]) -> None:
        self._write(self._dir(f) / "deployed.pac", v["pac_content"])


def _current(f: Dict[str, Any]) -> Dict[str, Any]:
    deployed = [v for v in f["versions"] if v["pac_version_status"] == "DEPLOYED"]
    pool = deployed or f["versions"]
    return max(pool, key=lambda v: v["pac_version"])


def _obj(f: Dict[str, Any], v: Dict[str, Any], with_content: bool = True) -> SimObject:
    data = {k: f.get(k) for k in _FILE_FIELDS}
    data.update(v)
    if not with_content:
        data["pac_content"] = None
    return SimObject(data)


class _Zia:
    def __init__(self, store: SimStore):
        self.pac_files = SimPacFilesAPI(store)
        self.activate = SimActivateAPI(store)


class SimulatedZscalerClient:
    """Stand-in for zscaler.ZscalerClient: exposes `.zia.pac_files` and `.zia.activate`."""

    def __init__(self, state_dir: str = "./sim-zia"):
        self.state_dir = state_dir
        self.zia = _Zia(SimStore(state_dir))

    def __enter__(self) -> "SimulatedZscalerClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def pac_files_summary(self) -> List[Dict[str, Any]]:
        """For tests and the demo: every PAC file with its versions (no content)."""
        state = self.zia.pac_files._store.load()
        return [{**{k: f[k] for k in _FILE_FIELDS}, "versions": [{k: v[k] for k in v if k != "pac_content"}
                                                               for v in f["versions"]]}
                for f in state["files"].values()]
