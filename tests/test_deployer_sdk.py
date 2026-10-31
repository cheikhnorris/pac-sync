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
