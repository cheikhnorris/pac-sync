"""Test-only fake of a ZIA tenant: same surface as the SDK's `client.zia.pac_files` and `client.zia.activate`.

Tests inject it by monkeypatching `make_zscaler_client` (see `install_fake_zia`), so the real ZiaDeployer
logic (unstage, clone, deploy, LKG, eviction, bootstrap) runs without a tenant. State lives in
`<state_dir>/pacfiles.json`; every version is also written to `<state_dir>/<folder>/v<N>.pac` and the deployed one
to `<state_dir>/<folder>/deployed.pac`. `<folder>` is the PAC file name when that is a plain lower-case file name
(`emea-proxy.pac`, `demo-na-proxy.pac`), else the sanitized name plus a hash of the exact name, so two names can
never share a folder (see `safe_dir_name`).

Semantics (copied from the fake in tests/test_deployer_sdk.py and the SDK docs, not verified against a tenant):
- at most 10 versions per PAC file; a clone at the cap deletes `delete_version` when given, else the LOWEST version
  number (what the SDK sends), and the server refuses to delete a DEPLOYED, LKG or STAGE version;
- DEPLOY demotes the previous DEPLOYED to LKG (and a previous LKG to UNSTAGED: one LKG); UNSTAGE -> UNSTAGED;
  LKG marks a version (the old LKG becomes UNSTAGED); REMOVE_LKG unmarks it and optionally marks `new_lkg_ver`;
- version objects carry the SDK field names `last_modified_time` (epoch seconds), `create_time`, `last_modified_by`;
- `activate.status()` is PENDING after any write that was not activated, or when `foreign_pending` is set in
  pacfiles.json (test hook standing for another admin's unactivated change); `activate()` clears both;
- with `strict_commit` (`strict_commit=True`) a clone on a PAC file created since the last
  activation fails like the SDK documents (400 RESOURCE_NOT_FOUND until the parent is committed).
Every call returns the SDK's (value, response, error) tuple.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_VERSIONS = 10
PROTECTED = ("DEPLOYED", "LKG", "STAGE")
SIM_USER = {"id": 0, "name": "pac-sync-sim"}
_FILE_FIELDS = ("id", "name", "domain", "description")
_RESERVED = {"con", "prn", "aux", "nul", "com1", "com2", "lpt1", "lpt2"}


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
    """Folder for a PAC name. Plain lower-case names are kept; anything else (separators, '..', upper case that
    would collide on NTFS, reserved device names) gets a short hash of the exact name, so folders are unique."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") or "_"
    plain = cleaned == name and name == name.lower() and name.split(".")[0] not in _RESERVED
    return cleaned if plain else f"{cleaned}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"


def _now() -> int:
    return int(time.time())


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return _now()


class SimPacFilesAPI:
    def __init__(self, store: "SimStore", strict_commit: bool = False):
        self._store = store
        self._strict = strict_commit

    # -- reads -------------------------------------------------------------------------------------------------
    def list_pac_files(self, query_params: Optional[dict] = None):
        params = query_params or {}
        search = str(params.get("search") or "").lower()
        no_content = params.get("filter") == "pac_content"
        out = []
        for f in self._store.load()["files"].values():
            if search and search not in f["name"].lower():
                continue
            out.append(_obj(f, _current(f), with_content=not no_content))
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
        version = _new_version(1, kwargs.get("pac_version_status") or "DEPLOYED", kwargs.get("pac_commit_message"),
                               kwargs.get("pac_verification_status"), content)
        f = {"id": pac_id, "name": name, "domain": kwargs.get("domain") or "", "description": kwargs.get("description"),
             "committed": False, "versions": [version]}
        state["files"][str(pac_id)] = f
        state["unactivated"] = True
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
        if self._strict and not f.get("committed", True):
            return None, None, "400 RESOURCE_NOT_FOUND: the PAC file is not committed yet, activate first"
        if not any(v["pac_version"] == int(pac_version) for v in f["versions"]):
            return None, None, f"PAC file {pac_id} has no version {pac_version}"
        if len(f["versions"]) >= MAX_VERSIONS:
            if delete_version is not None:
                gone = next((v for v in f["versions"] if v["pac_version"] == int(delete_version)), None)
                if gone is None:
                    return None, None, f"deleteVersion {delete_version} does not exist"
            else:  # what the SDK sends when the caller names nothing: min(pac_versions), whatever its status
                gone = min(f["versions"], key=lambda v: v["pac_version"])
            if gone["pac_version_status"] in PROTECTED:
                return None, None, f"400: version {gone['pac_version']} is {gone['pac_version_status']} and cannot be deleted"
            f["versions"].remove(gone)
            self._store.remove_version(f, gone)
        top = max([v["pac_version"] for v in f["versions"]] + [state["max_version"].get(str(pac_id), 0)])
        new = _new_version(top + 1, pac_version_status, pac_commit_message, pac_verification_status, pac_content)
        f["versions"].append(new)
        state["max_version"][str(pac_id)] = new["pac_version"]
        state["unactivated"] = True
        self._store.save(state)
        self._store.write_version(f, new)
        return _obj(f, new), None, None

    def update_pac_file(self, pac_id: int, pac_version: int, pac_version_action: Optional[str] = None,
                        new_lkg_ver: Optional[int] = None, **kwargs):
        state = self._store.load()
        f = state["files"].get(str(pac_id))
        if f is None:
            return None, None, f"PAC file {pac_id} not found"
        by_no = {v["pac_version"]: v for v in f["versions"]}
        target = by_no.get(int(pac_version))
        if target is None:
            return None, None, f"PAC file {pac_id} has no version {pac_version}"
        action = pac_version_action
        if action == "DEPLOY":
            for v in f["versions"]:
                if v is not target and v["pac_version_status"] == "LKG":
                    v["pac_version_status"] = "UNSTAGED"
            for v in f["versions"]:
                if v is not target and v["pac_version_status"] == "DEPLOYED":
                    v["pac_version_status"] = "LKG"
            target["pac_version_status"] = "DEPLOYED"
        elif action == "UNSTAGE":
            target["pac_version_status"] = "UNSTAGED"
        elif action == "LKG":
            if target["pac_version_status"] == "DEPLOYED":
                return None, None, "the DEPLOYED version cannot be marked LKG"
            for v in f["versions"]:
                if v["pac_version_status"] == "LKG":
                    v["pac_version_status"] = "UNSTAGED"
            target["pac_version_status"] = "LKG"
        elif action == "REMOVE_LKG":
            if target["pac_version_status"] != "LKG":
                return None, None, f"version {pac_version} is not the LKG version"
            new = by_no.get(int(new_lkg_ver)) if new_lkg_ver is not None else None
            if new_lkg_ver is not None and (new is None or new["pac_version_status"] == "DEPLOYED"):
                return None, None, f"newLKGVer {new_lkg_ver} is not a valid version"
            target["pac_version_status"] = "UNSTAGED"
            if new is not None:
                new["pac_version_status"] = "LKG"
        elif action:
            target["pac_version_status"] = action
        target["last_modified_time"] = _now()
        state["unactivated"] = True
        self._store.save(state)
        if target["pac_version_status"] == "DEPLOYED":
            self._store.write_deployed(f, target)
        return _obj(f, target), None, None

    def delete_pac_file(self, pac_id: int):
        state = self._store.load()
        f = state["files"].pop(str(pac_id), None)
        if f is None:
            return None, None, f"PAC file {pac_id} not found"
        state["unactivated"] = True
        self._store.save(state)
        self._store.remove_folder(f)
        return None, None, None


class SimActivateAPI:
    def __init__(self, store: "SimStore"):
        self._store = store

    def activate(self):
        state = self._store.load()
        state["activations"] = state.get("activations", 0) + 1
        state["unactivated"] = False
        state["foreign_pending"] = False
        for f in state["files"].values():
            f["committed"] = True
        self._store.save(state)
        return SimObject({"status": "ACTIVE"}), None, None

    def status(self):
        state = self._store.load()
        pending = state.get("foreign_pending") or state.get("unactivated")
        return SimObject({"status": "PENDING" if pending else "ACTIVE"}), None, None


class SimStore:
    """JSON state file with atomic writes (temp file + os.replace)."""

    def __init__(self, state_dir: str):
        self.root = Path(state_dir)
        self.path = self.root / "pacfiles.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"next_id": 1, "files": {}, "max_version": {}, "activations": 0, "url_categories": {}, "url_rules": {},
                    "forwarding_rules": {}, "proxy_gateways": []}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state.setdefault("max_version", {})
        state.setdefault("url_categories", {})
        state.setdefault("url_rules", {})
        state.setdefault("forwarding_rules", {})
        state.setdefault("proxy_gateways", [])
        for f in state["files"].values():
            for v in f["versions"]:  # Phase 1 files stored an ISO string under last_modification_time
                old = v.pop("last_modification_time", None)
                v["last_modified_time"] = _epoch(v.get("last_modified_time", old))
                v.setdefault("create_time", v["last_modified_time"])
                v.setdefault("last_modified_by", dict(SIM_USER))
        return state

    def save(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pacfiles-", suffix=".json", dir=self.root)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def folder(self, f: Dict[str, Any]) -> Path:
        return self.root / safe_dir_name(f["name"])

    def _dir(self, f: Dict[str, Any]) -> Path:
        d = self.folder(f)
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

    def remove_folder(self, f: Dict[str, Any]) -> None:
        shutil.rmtree(self.folder(f), ignore_errors=True)


def _new_version(number: int, status: str, message: Optional[str], verification: Optional[str], content: str) -> dict:
    now = _now()
    return {"pac_version": number, "pac_version_status": status, "pac_commit_message": message or "",
            "pac_verification_status": verification or "VERIFY_NOERR", "pac_content": content,
            "create_time": now, "last_modified_time": now, "last_modified_by": dict(SIM_USER)}


def _current(f: Dict[str, Any]) -> Dict[str, Any]:
    deployed = [v for v in f["versions"] if v["pac_version_status"] == "DEPLOYED"]
    return max(deployed or f["versions"], key=lambda v: v["pac_version"])


def _obj(f: Dict[str, Any], v: Dict[str, Any], with_content: bool = True) -> SimObject:
    data = {k: f.get(k) for k in _FILE_FIELDS}
    data.update(v)
    if not with_content:
        data["pac_content"] = None
    return SimObject(data)


class SimUrlCategoriesAPI:
    """client.zia.url_categories: same method names and kwargs as the SDK; objects use the SDK attribute names.
    Custom category ids are CUSTOM_NN; a category still referenced by a rule cannot be deleted."""

    def __init__(self, store: "SimStore"):
        self._store = store

    def list_categories(self, query_params: Optional[dict] = None):
        custom_only = (query_params or {}).get("custom_only")
        items = [c for c in self._store.load()["url_categories"].values() if c.get("custom_category") or not custom_only]
        return [SimObject(c) for c in items], None, None

    def add_url_category(self, super_category: str, urls: Optional[List[str]] = None, configured_name: Optional[str] = None,
                         **kwargs):
        state = self._store.load()
        if any(c["configured_name"] == configured_name for c in state["url_categories"].values()):
            return None, None, f"a URL category named '{configured_name}' already exists"
        n = state["next_category"] = state.get("next_category", 0) + 1
        cat = {"id": f"CUSTOM_{n:02d}", "configured_name": configured_name, "super_category": super_category,
               "urls": list(urls or []), "description": kwargs.get("description"),
               "custom_category": bool(kwargs.get("custom_category"))}
        state["url_categories"][cat["id"]] = cat
        state["unactivated"] = True
        self._store.save(state)
        return SimObject(cat), None, None

    def update_url_category(self, category_id: str, action: Optional[str] = None, **kwargs):
        state = self._store.load()
        cat = state["url_categories"].get(category_id)
        if cat is None:
            return None, None, f"URL category {category_id} not found"
        cat.update({k: v for k, v in kwargs.items() if k in ("configured_name", "super_category", "urls", "description")})
        state["unactivated"] = True
        self._store.save(state)
        return SimObject(cat), None, None

    def delete_category(self, category_id: str):
        state = self._store.load()
        if category_id not in state["url_categories"]:
            return None, None, f"URL category {category_id} not found"
        if any(category_id in r["url_categories"] for r in state["url_rules"].values()):
            return None, None, f"400: URL category {category_id} is referenced by a URL filtering rule"
        del state["url_categories"][category_id]
        state["unactivated"] = True
        self._store.save(state)
        return None, None, None


class SimUrlFilteringAPI:
    """client.zia.url_filtering: a rule added without `order` goes to the bottom."""

    def __init__(self, store: "SimStore"):
        self._store = store

    def list_rules(self, query_params: Optional[dict] = None):
        return [SimObject(r) for r in self._store.load()["url_rules"].values()], None, None

    def add_rule(self, **kwargs):
        state = self._store.load()
        if any(r["name"] == kwargs.get("name") for r in state["url_rules"].values()):
            return None, None, f"a URL filtering rule named '{kwargs.get('name')}' already exists"
        n = state["next_rule"] = state.get("next_rule", 1000) + 1
        order = kwargs.get("order") or max([r["order"] for r in state["url_rules"].values()] + [0]) + 1
        rule = {"id": n, "order": order, "rank": kwargs.get("rank"), "name": kwargs.get("name"),
                "action": kwargs.get("action"), "state": kwargs.get("state"),
                "protocols": kwargs.get("protocols"), "description": kwargs.get("description"),
                "url_categories": list(kwargs.get("url_categories") or [])}
        state["url_rules"][str(n)] = rule
        state["unactivated"] = True
        self._store.save(state)
        return SimObject(rule), None, None

    def update_rule(self, rule_id, **kwargs):
        state = self._store.load()
        rule = state["url_rules"].get(str(rule_id))
        if rule is None:
            return None, None, f"URL filtering rule {rule_id} not found"
        rule.update(kwargs)
        state["unactivated"] = True
        self._store.save(state)
        return SimObject(rule), None, None

    def delete_rule(self, rule_id):
        state = self._store.load()
        if state["url_rules"].pop(str(rule_id), None) is None:
            return None, None, f"URL filtering rule {rule_id} not found"
        state["unactivated"] = True
        self._store.save(state)
        return None, None, None


class SimForwardingControlAPI:
    """client.zia.forwarding_control: a rule added without `order` goes to the bottom; `proxy_gateway` is read back as
    an object with .id and .name, like the SDK's CommonBlocks."""

    def __init__(self, store: "SimStore"):
        self._store = store

    @staticmethod
    def _obj(rule: Dict[str, Any]) -> SimObject:
        gw = rule.get("proxy_gateway")
        return SimObject(dict(rule, proxy_gateway=SimObject(gw) if gw else None))

    def list_rules(self, query_params: Optional[dict] = None):
        return [self._obj(r) for r in self._store.load()["forwarding_rules"].values()], None, None

    def add_rule(self, **kwargs):
        state = self._store.load()
        if any(r["name"] == kwargs.get("name") for r in state["forwarding_rules"].values()):
            return None, None, f"a forwarding rule named '{kwargs.get('name')}' already exists"
        if len(kwargs.get("name") or "") > 31:
            return None, None, "400: forwarding rule names are at most 31 characters"
        n = state["next_fwd_rule"] = state.get("next_fwd_rule", 2000) + 1
        order = kwargs.get("order") or max([r["order"] for r in state["forwarding_rules"].values()] + [0]) + 1
        rule = {"id": n, "order": order, "rank": None, "state": None, "forward_method": None, "dest_addresses": [],
                "proxy_gateway": None, "description": None, **kwargs}
        rule["order"] = order
        state["forwarding_rules"][str(n)] = rule
        state["unactivated"] = True
        self._store.save(state)
        return self._obj(rule), None, None

    def update_rule(self, rule_id, **kwargs):
        state = self._store.load()
        rule = state["forwarding_rules"].get(str(rule_id))
        if rule is None:
            return None, None, f"forwarding rule {rule_id} not found"
        rule.update(kwargs)
        state["unactivated"] = True
        self._store.save(state)
        return self._obj(rule), None, None

    def delete_rule(self, rule_id):
        state = self._store.load()
        if state["forwarding_rules"].pop(str(rule_id), None) is None:
            return None, None, f"forwarding rule {rule_id} not found"
        state["unactivated"] = True
        self._store.save(state)
        return None, None, None


class SimProxiesAPI:
    def __init__(self, store: "SimStore"):
        self._store = store

    def list_proxy_gateways(self):
        return [SimObject(g) for g in self._store.load()["proxy_gateways"]], None, None


class _Zia:
    def __init__(self, store: SimStore, strict_commit: bool):
        self.pac_files = SimPacFilesAPI(store, strict_commit)
        self.activate = SimActivateAPI(store)
        self.url_categories = SimUrlCategoriesAPI(store)
        self.url_filtering = SimUrlFilteringAPI(store)
        self.forwarding_control = SimForwardingControlAPI(store)
        self.proxies = SimProxiesAPI(store)


class SimulatedZscalerClient:
    """Stand-in for zscaler.ZscalerClient: exposes `.zia.pac_files` and `.zia.activate`."""

    def __init__(self, state_dir: str = "./sim-zia", strict_commit: bool = False):
        self.state_dir = state_dir
        self.store = SimStore(state_dir)
        self.zia = _Zia(self.store, strict_commit)

    def __enter__(self) -> "SimulatedZscalerClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def set_foreign_pending(self, value: bool = True) -> None:
        """Test hook: another admin has unactivated changes."""
        state = self.store.load()
        state["foreign_pending"] = value
        self.store.save(state)

    def set_proxy_gateways(self, *names: str) -> None:
        """Test hook: the proxy gateways ZIA has (ids 1, 2, ...)."""
        state = self.store.load()
        state["proxy_gateways"] = [{"id": i, "name": n} for i, n in enumerate(names, 1)]
        self.store.save(state)

    def seed_object(self, kind: str, data: Dict[str, Any]) -> Any:
        """Test hook: add a URL category (kind "category") or URL filtering rule (kind "rule") as another admin would;
        fields use the SDK attribute names."""
        state = self.store.load()
        if kind == "forwarding":
            n = state["next_fwd_rule"] = state.get("next_fwd_rule", 2000) + 1
            data = {"id": n, "order": max([r["order"] for r in state["forwarding_rules"].values()] + [0]) + 1, "rank": 7,
                    "state": "ENABLED", "forward_method": "DIRECT", "dest_addresses": [], "proxy_gateway": None,
                    "description": None, **data}
            state["forwarding_rules"][str(n)] = data
        elif kind == "category":
            data = {"id": f"CUSTOM_{len(state['url_categories']) + 90:02d}", "custom_category": True, "super_category":
                    "USER_DEFINED", "urls": [], "description": None, **data}
            state["url_categories"][data["id"]] = data
        else:
            n = state["next_rule"] = state.get("next_rule", 1000) + 1
            data = {"id": n, "order": max([r["order"] for r in state["url_rules"].values()] + [0]) + 1, "rank": 7,
                    "state": "ENABLED", "url_categories": [], "description": None, **data}
            state["url_rules"][str(n)] = data
        self.store.save(state)  # pre-existing object: does not mark ZIA pending
        return SimObject(data)

    def pac_files_summary(self) -> List[Dict[str, Any]]:
        """For tests and the demo: every PAC file with its versions (no content)."""
        state = self.store.load()
        return [{**{k: f[k] for k in _FILE_FIELDS}, "versions": [{k: v[k] for k in v if k != "pac_content"}
                                                               for v in f["versions"]]}
                for f in state["files"].values()]


def install_fake_zia(monkeypatch, state_dir, strict_commit=False):
    """Make main and doctor build this fake instead of an SDK client; keeps the real factory's prefix validation."""
    from pac_sync import doctor, main
    from pac_sync.deployer import validate_prefix_config

    def factory(zcfg):
        validate_prefix_config(zcfg)
        return SimulatedZscalerClient(str(state_dir), strict_commit)

    monkeypatch.setattr(main, "make_zscaler_client", factory)
    monkeypatch.setattr(doctor, "make_zscaler_client", factory)
