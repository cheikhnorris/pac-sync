"""ServiceNow access: live Table API, or an offline JSON file with the same shape."""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List

import requests

from .envfile import require_env

log = logging.getLogger(__name__)

REGION_FIELDS = "sys_id,u_code,u_zia_pac_name,u_default_profile,u_active"
PROFILE_FIELDS = "sys_id,u_name,u_proxy_chain,u_active"
RULE_FIELDS = (
    "sys_id,number,u_action,u_match_type,u_value,u_proxy_profile,u_scope,u_regions,u_priority,"
    "u_state,u_valid_from,u_valid_to,u_change_request,u_deploy_status,u_deploy_version,sys_updated_on"
)
RULE_QUERY = "u_stateINapproved,deployed,retire_requested^ORDERBYu_priority^ORDERBYnumber"


class ServiceNowClient:
    """Minimal Table API client: paginated GET, PATCH, POST, with retries."""

    def __init__(self, cfg: dict):
        self.base = cfg["instance_url"].rstrip("/")
        self.page_size = int(cfg.get("page_size", 1000))
        self.timeout = int(cfg.get("timeout_seconds", 30))
        self.tables = cfg["tables"]
        self._cfg = cfg
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self._token_expiry = 0.0
        if cfg.get("auth", "oauth") == "basic":
            why = "servicenow.auth=basic"
            self._session.auth = (require_env(cfg["username_env"], why), require_env(cfg["password_env"], why))

    def _ensure_token(self) -> None:
        if self._cfg.get("auth", "oauth") != "oauth" or time.time() < self._token_expiry - 60:
            return
        resp = requests.post(
            f"{self.base}/oauth_token.do",
            data={
                "grant_type": "client_credentials",
                "client_id": require_env(self._cfg["client_id_env"], "servicenow.auth=oauth"),
                "client_secret": require_env(self._cfg["client_secret_env"], "servicenow.auth=oauth"),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        self._session.headers["Authorization"] = f"Bearer {body['access_token']}"
        self._token_expiry = time.time() + int(body.get("expires_in", 1800))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(1, 5):
            self._ensure_token()
            resp = self._session.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
            if resp.status_code in (429, 502, 503, 504) and attempt < 4:
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                log.warning("ServiceNow %s %s -> %s, retry in %ss", method, path, resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        raise RuntimeError("unreachable")

    def get_all(self, table: str, query: str, fields: str) -> List[dict]:
        rows: List[dict] = []
        offset = 0
        while True:
            page = self._request(
                "GET",
                f"/api/now/table/{table}",
                params={
                    "sysparm_query": query,
                    "sysparm_fields": fields,
                    "sysparm_exclude_reference_link": "true",
                    "sysparm_display_value": "false",
                    "sysparm_limit": self.page_size,
                    "sysparm_offset": offset,
                },
            ).get("result", [])
            rows.extend(page)
            if len(page) < self.page_size:
                return rows
            offset += self.page_size

    def probe(self, table: str) -> int:
        """Read at most one record (doctor): proves the token and the table ACL. Return the rows read."""
        rows = self._request("GET", f"/api/now/table/{table}",
                             params={"sysparm_limit": 1, "sysparm_fields": "sys_id"}).get("result", [])
        return len(rows)

    def fetch_regions(self) -> List[dict]:
        return self.get_all(self.tables["region"], "u_active=true", REGION_FIELDS)

    def fetch_profiles(self) -> List[dict]:
        return self.get_all(self.tables["proxy_profile"], "u_active=true", PROFILE_FIELDS)

    def fetch_rules(self) -> List[dict]:
        return self.get_all(self.tables["rule"], RULE_QUERY, RULE_FIELDS)

    def update_rule(self, sys_id: str, body: dict) -> None:
        self._request("PATCH", f"/api/now/table/{self.tables['rule']}/{sys_id}", data=json.dumps(body))

    def log_deployment(self, body: dict) -> None:
        self._request("POST", f"/api/now/table/{self.tables['deployment']}", data=json.dumps(body))


class FileServiceNowSource:
    """Offline stand-in for development and tests. Writes are recorded, not sent."""

    def __init__(self, cfg: dict):
        with open(cfg["sample_file"], encoding="utf-8") as fh:
            self._data: Dict[str, List[dict]] = json.load(fh)
        self.tables = cfg["tables"]
        self.rule_updates: List[tuple] = []
        self.deployments: List[dict] = []

    def _active(self, table: str) -> List[dict]:
        return [r for r in self._data.get(table, []) if str(r.get("u_active", "true")).lower() == "true"]

    def fetch_regions(self) -> List[dict]:
        return self._active(self.tables["region"])

    def fetch_profiles(self) -> List[dict]:
        return self._active(self.tables["proxy_profile"])

    def fetch_rules(self) -> List[dict]:
        states = {"approved", "deployed", "retire_requested"}
        return [r for r in self._data.get(self.tables["rule"], []) if r.get("u_state") in states]

    def update_rule(self, sys_id: str, body: dict) -> None:
        log.info("[file source] PATCH %s %s", sys_id, body)
        self.rule_updates.append((sys_id, body))

    def log_deployment(self, body: dict) -> None:
        log.info("[file source] POST deployment %s", body)
        self.deployments.append(body)


def make_source(cfg: dict):
    return FileServiceNowSource(cfg) if cfg.get("source") == "file" else ServiceNowClient(cfg)
