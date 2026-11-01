"""ZiaDeployer against the real zscaler-sdk-python stack.

Only the OAuth token and the raw HTTP send are replaced; URL building, body
serialization and response models are the SDK's own code.
"""

import json
import re

import pytest
import requests

pytest.importorskip("zscaler")

from pac_sync.deployer import ZiaDeployer, make_zscaler_client  # noqa: E402

BASE = "/zia/api/v1"


class FakeZia:
    """In-memory ZIA PAC file endpoints."""

    def __init__(self):
        self.calls = []
        self.urls = []
        self.versions = [
            {"id": 11, "name": "emea-proxy.pac", "domain": "example.com", "description": "EMEA",
             "pacVersion": 1, "pacVersionStatus": "DEPLOYED", "pacCommitMessage": "bootstrap",
             "pacVerificationStatus": "VERIFY_NOERR", "pacContent": "function FindProxyForURL(u,h){return \"DIRECT\";}"},
        ]

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and path == f"{BASE}/pacFiles":
            return [{"id": 11, "name": "emea-proxy.pac", "domain": "example.com"},
                    {"id": 12, "name": "emea-proxy.pac.old", "domain": "example.com"}]
        if method == "GET" and path == f"{BASE}/pacFiles/11/version":
            return self.versions
        if method == "POST" and path == f"{BASE}/pacFiles/validate":
            return {"success": True, "message": "ok"}
        m = re.fullmatch(rf"{BASE}/pacFiles/11/version/(\d+)", path)
        if method == "POST" and m:
            new = dict(self.versions[0], pacVersion=max(v["pacVersion"] for v in self.versions) + 1,
                       pacVersionStatus=body["pacVersionStatus"], pacContent=body["pacContent"],
                       pacCommitMessage=body["pacCommitMessage"])
            self.versions.append(new)
            return new
        m = re.fullmatch(rf"{BASE}/pacFiles/11/version/(\d+)/action/(\w+)", path)
        if method == "PUT" and m:
            version, action = int(m.group(1)), m.group(2)
            for v in self.versions:
                if action == "DEPLOY" and v["pacVersionStatus"] == "DEPLOYED":
                    v["pacVersionStatus"] = "LKG"
                if v["pacVersion"] == version:
                    v["pacVersionStatus"] = {"DEPLOY": "DEPLOYED", "UNSTAGE": "UNSTAGED"}.get(action, action)
            return next(v for v in self.versions if v["pacVersion"] == version)
        if method == "POST" and path == f"{BASE}/status/activate":
            return {"status": "ACTIVE"}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def zia(monkeypatch):
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
    client = make_zscaler_client({"client_id_env": "ZSCALER_CLIENT_ID", "client_secret_env": "ZSCALER_CLIENT_SECRET",
                                  "vanity_domain_env": "ZSCALER_VANITY_DOMAIN"})
    fake = FakeZia()
    executor = client.zia.pac_files._request_executor
    monkeypatch.setattr(executor._oauth, "_get_access_token", lambda *a, **k: "token")

    def send_request(request):
        path = requests.utils.urlparse(request["url"]).path
        raw = request.get("json", request.get("data"))
        query = "&".join(f"{k}={v}" for k, v in (request.get("params") or {}).items())  # the SDK moves ?x=y here
        fake.urls.append((request["method"].upper(), request["url"] + (f"?{query}" if query else "")))
        result = fake.handle(request["method"].upper(), path, raw)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(result).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.url = request["url"]
        return resp, None

    monkeypatch.setattr(executor._http_client, "send_request", send_request)
    return client, fake


def test_find_pac_id_exact_name(zia):
    client, _ = zia
    assert ZiaDeployer(client).find_pac_id("emea-proxy.pac") == 11


def test_stage_then_deploy_then_activate(zia):
    client, fake = zia
    d = ZiaDeployer(client)
    content = 'function FindProxyForURL(url, host) { return "DIRECT"; }'

    version = d.stage(11, content, "PAC Sync test")
    assert version == 2
    clone = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/pacFiles/11/version/1"))
    assert clone[2] == {"pacContent": content, "pacVerificationStatus": "VERIFY_NOERR",
                        "pacCommitMessage": "PAC Sync test", "pacVersionStatus": "STAGE"}

    d.deploy(11, version)
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[1].endswith("/pacFiles/11/version/2/action/DEPLOY")
    assert put[2]["pacContent"] == content and put[2]["name"] == "emea-proxy.pac"
    assert [v["pacVersionStatus"] for v in fake.versions] == ["LKG", "DEPLOYED"]

    d.activate()
    assert fake.calls[-1][:2] == ("POST", f"{BASE}/status/activate")


def test_stage_replaces_previous_stage(zia):
    client, fake = zia
    d = ZiaDeployer(client)
    d.stage(11, 'function FindProxyForURL(url, host) { return "DIRECT"; }', "first")
    d.stage(11, 'function FindProxyForURL(url, host) { return "PROXY a:80"; }', "second")
    assert any(c[0] == "PUT" and c[1].endswith("/version/2/action/UNSTAGE") for c in fake.calls)
    assert [v["pacVersionStatus"] for v in fake.versions] == ["DEPLOYED", "UNSTAGED", "STAGE"]


def _ten_versions(fake):
    """v1 DEPLOYED, v2 LKG, v3..v10 UNSTAGED: ZIA's per-file cap is reached."""
    base = fake.versions[0]
    fake.versions = [dict(base, pacVersion=n, pacVersionStatus={1: "DEPLOYED", 2: "LKG"}.get(n, "UNSTAGED"),
                          pacCommitMessage=f"v{n}") for n in range(1, 11)]


def test_clone_at_the_cap_deletes_the_oldest_evictable_not_the_lowest(zia):
    client, fake = zia
    _ten_versions(fake)
    evicted = []
    d = ZiaDeployer(client)
    d.on_evict = lambda v: evicted.append((int(v.pac_version), v.pac_version_status))
    d.stage(11, 'function FindProxyForURL(url, host) { return "DIRECT"; }', "new")
    clone_url = next(u for method, u in fake.urls if method == "POST" and "/pacFiles/11/version/" in u)
    assert clone_url.endswith("/pacFiles/11/version/1?deleteVersion=3")  # the SDK alone would have said 1
    assert evicted == [(3, "UNSTAGED")]


def test_lkg_actions_urls(zia):
    client, fake = zia
    _ten_versions(fake)
    d = ZiaDeployer(client)
    d.set_lkg(11, 3)
    d.remove_lkg(11, new_lkg=4)
    d.remove_lkg(11)
    puts = [u for method, u in fake.urls if method == "PUT"]
    assert puts[0].endswith("/pacFiles/11/version/3/action/LKG")
    assert puts[1].endswith("/pacFiles/11/version/3/action/REMOVE_LKG?newLKGVer=4")
    assert puts[2].endswith("/action/REMOVE_LKG") and "newLKGVer" not in puts[2]


def test_probe_at_the_cap_carries_the_safe_delete_version(zia):
    """Real SDK: without it the SDK sends deleteVersion=<lowest> (v1, the DEPLOYED one, here an LKG too)."""
    client, fake = zia
    _ten_versions(fake)  # v1 DEPLOYED, v2 LKG, v3..v10 UNSTAGED
    d = ZiaDeployer(client)
    evicted = []
    d.on_evict = lambda v: evicted.append(int(v.pac_version))
    steps = d.probe_write(11, "PAC Sync 20261101T000000Z EMEA probe")
    assert steps == [("clone", "ok"), ("unstage", "ok")]
    clone_url = next(u for method, u in fake.urls if method == "POST" and "/pacFiles/11/version/" in u)
    assert clone_url.endswith("/pacFiles/11/version/1?deleteVersion=3")
    assert evicted == [3]
    assert any(m == "PUT" and u.endswith("/version/11/action/UNSTAGE") for m, u in fake.urls)


def test_probe_at_the_cap_with_nothing_deletable_sends_no_write(zia):
    client, fake = zia
    base = fake.versions[0]
    fake.versions = [dict(base, pacVersion=n, pacVersionStatus="DEPLOYED" if n == 1 else "LKG") for n in range(1, 11)]
    steps = ZiaDeployer(client).probe_write(11, "m")
    assert steps[0][0] == "clone" and "none can be deleted" in steps[0][1]
    assert not [1 for method, u in fake.urls if method in ("POST", "PUT") and "/pacFiles/11/version" in u]
