"""UrlFilteringApi against the real zscaler-sdk-python stack: only the OAuth token and the raw HTTP send are replaced,
so URL building and body serialization are the SDK's own and the wire bodies can be asserted."""

import json
import re

import pytest
import requests

pytest.importorskip("zscaler")

from pac_sync.deployer import assert_prefixed, make_zscaler_client  # noqa: E402
from pac_sync.envfile import ConfigError  # noqa: E402
from pac_sync.url_filtering import UrlFilteringApi  # noqa: E402
from pac_sync.zia_objects import ZGroup  # noqa: E402

BASE = "/zia/api/v1"
GROUP = {"u_name": "partner-sites", "u_action": "allow"}


class FakeZia:
    """In-memory /urlCategories and /urlFilteringRules."""

    def __init__(self):
        self.calls = []
        self.urls = []
        self.categories = [{"id": "CUSTOM_05", "configuredName": "demo-partner-sites", "superCategory": "USER_DEFINED",
                            "customCategory": True, "urls": [".a.example.com"], "description": "PAC Sync x"},
                           {"id": "CUSTOM_06", "configuredName": "corp", "customCategory": True, "urls": []}]
        self.rules = [{"id": 77, "name": "demo-partner-sites", "action": "ALLOW", "state": "ENABLED", "order": 4,
                       "rank": 7, "urlCategories": ["CUSTOM_05"], "description": "PAC Sync x"}]

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and path == f"{BASE}/urlCategories":
            return self.categories
        if method == "GET" and path == f"{BASE}/urlFilteringRules":
            return self.rules
        if method == "POST" and path == f"{BASE}/urlCategories":
            return dict(body, id="CUSTOM_07")
        if method == "POST" and path == f"{BASE}/urlFilteringRules":
            return dict(body, id=88, order=9)
        if method == "PUT" and re.fullmatch(rf"{BASE}/(urlCategories/CUSTOM_05|urlFilteringRules/77)", path):
            return body
        if method == "DELETE":
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def zia(monkeypatch):
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
    client = make_zscaler_client({"client_id_env": "ZSCALER_CLIENT_ID", "client_secret_env": "ZSCALER_CLIENT_SECRET",
                                  "vanity_domain_env": "ZSCALER_VANITY_DOMAIN"})
    fake = FakeZia()
    executor = client.zia.url_categories._request_executor
    monkeypatch.setattr(executor._oauth, "_get_access_token", lambda *a, **k: "token")

    def send_request(request):
        path = requests.utils.urlparse(request["url"]).path
        query = "&".join(f"{k}={v}" for k, v in (request.get("params") or {}).items())
        fake.urls.append((request["method"].upper(), request["url"] + (f"?{query}" if query else "")))
        result = fake.handle(request["method"].upper(), path, request.get("json", request.get("data")))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(result).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.url = request["url"]
        return resp, None

    monkeypatch.setattr(executor._http_client, "send_request", send_request)
    return client, fake


def api(client, ucfg=None):
    return UrlFilteringApi(client, lambda name, what: assert_prefixed("demo-", True, name, what), "demo-", ucfg)


def test_read_keeps_prefixed_objects_and_builds_the_canonical_fields(zia):
    client, fake = zia
    groups = api(client).read()
    assert sorted(groups) == ["demo-partner-sites"]
    zg = groups["demo-partner-sites"]
    assert zg.canonical == {"action": "ALLOW", "state": "ENABLED", "categories": ["demo-partner-sites"],
                            "urls": [".a.example.com"]}
    assert ("GET", "https://api.zsapi.net/zia/api/v1/urlCategories?customOnly=True") in fake.urls


def test_create_wire_bodies(zia):
    client, fake = zia
    api(client).create("demo-new", GROUP, ["b.example.com", "c.example.com/x"], "PAC Sync msg")
    cat = next(b for m_, p, b in fake.calls if m_ == "POST" and p.endswith("/urlCategories"))
    assert cat["configuredName"] == "demo-new" and cat["superCategory"] == "USER_DEFINED" and cat["customCategory"] is True
    assert cat["urls"] == ["b.example.com", "c.example.com/x"] and cat["description"] == "PAC Sync msg"
    rule = next(b for m_, p, b in fake.calls if m_ == "POST" and p.endswith("/urlFilteringRules"))
    assert rule["urlCategories"] == ["CUSTOM_07"] and rule["action"] == "ALLOW" and rule["state"] == "ENABLED"
    assert rule["rank"] == 7 and rule["protocols"] == ["ANY_RULE"] and rule["name"] == "demo-new" and "order" not in rule
    api(client, {"rule_order": 3}).create("demo-two", GROUP, ["b.example.com"], "m")
    assert [b for m_, p, b in fake.calls if m_ == "POST" and p.endswith("/urlFilteringRules")][-1]["order"] == 3


def test_update_is_a_full_replace_and_keeps_order_and_rank(zia):
    client, fake = zia
    zg = api(client).read()["demo-partner-sites"]
    api(client).update(zg, {"u_name": "partner-sites", "u_action": "block"}, ["z.example.com"], "PAC Sync new")
    cat_url = next(u for m_, u in fake.urls if m_ == "PUT" and "urlCategories" in u)
    assert cat_url.endswith("/urlCategories/CUSTOM_05") and "action=" not in cat_url
    cat = next(b for m_, p, b in fake.calls if m_ == "PUT" and "urlCategories" in p)
    assert cat["configuredName"] == "demo-partner-sites" and cat["urls"] == ["z.example.com"]
    assert cat["superCategory"] == "USER_DEFINED" and cat["customCategory"] is True and cat["description"] == "PAC Sync new"
    rule = next(b for m_, p, b in fake.calls if m_ == "PUT" and "urlFilteringRules/77" in p)
    assert rule["order"] == 4 and rule["rank"] == 7 and rule["action"] == "BLOCK" and rule["urlCategories"] == ["CUSTOM_05"]


def test_delete_removes_the_rule_before_the_category(zia):
    client, fake = zia
    api(client).delete(api(client).read()["demo-partner-sites"])
    deletes = [p for m_, p, _b in fake.calls if m_ == "DELETE"]
    assert deletes == [f"{BASE}/urlFilteringRules/77", f"{BASE}/urlCategories/CUSTOM_05"]


def test_unprefixed_name_raises_before_any_http(zia):
    client, fake = zia
    with pytest.raises(ConfigError, match="refusing to write URL category 'evil'"):
        api(client).create("evil", GROUP, ["a.example.com"], "m")
    foreign = ZGroup("corp", [], None, [], (type("C", (), {"id": "X", "configured_name": "corp"})(), None))
    with pytest.raises(ConfigError):
        api(client).delete(foreign)
    assert fake.calls == []


# -- phase 2: forwarding ------------------------------------------------------------------------------------------------
from pac_sync.forwarding import ForwardingApi  # noqa: E402

FWD = {"u_name": "partner-egress", "u_forward_method": "proxychain", "u_proxy_gateway": "PARTNER-EGRESS-GW"}


@pytest.fixture
def fwd(zia, monkeypatch):
    client, fake = zia
    fake.rules_f = [{"id": 5, "name": "demo-partner-egress", "type": "FORWARDING", "forwardMethod": "PROXYCHAIN",
                     "state": "ENABLED", "order": 3, "rank": 7, "destAddresses": ["b.example.com"],
                     "proxyGateway": {"id": 9, "name": "PARTNER-EGRESS-GW"}, "description": "PAC Sync x"},
                    {"id": 6, "name": "corp", "forwardMethod": "DIRECT"}]
    real = fake.handle

    def handle(method, path, body):
        if method == "GET" and path == f"{BASE}/proxyGateways":
            fake.calls.append((method, path, body))
            return [{"id": 9, "name": "PARTNER-EGRESS-GW"}, {"id": 10, "name": "OTHER"}]
        if path.startswith(f"{BASE}/forwardingRules"):
            fake.calls.append((method, path, body))
            return fake.rules_f if method == "GET" else (body or {}) | {"id": 11}
        return real(method, path, body)

    fake.handle = handle
    return client, fake


def fapi(client, ucfg=None):
    return ForwardingApi(client, lambda name, what: assert_prefixed("demo-", True, name, what), "demo-", ucfg)


def test_forwarding_read_and_gateway_lookup(fwd):
    client, fake = fwd
    groups = fapi(client).read()
    assert sorted(groups) == ["demo-partner-egress"]
    assert groups["demo-partner-egress"].canonical == {
        "forward_method": "PROXYCHAIN", "proxy_gateway": "PARTNER-EGRESS-GW", "state": "ENABLED",
        "dest_addresses": ["b.example.com"]}


def test_forwarding_create_update_delete_wire_bodies(fwd):
    client, fake = fwd
    fapi(client).create("demo-new", FWD, ["z.example.com", "10.0.0.0/8"], "PAC Sync msg")
    body = next(b for m_, p, b in fake.calls if m_ == "POST" and p.endswith("/forwardingRules"))
    assert body["type"] == "FORWARDING" and body["forwardMethod"] == "PROXYCHAIN" and body["state"] == "ENABLED"
    assert body["destAddresses"] == ["10.0.0.0/8", "z.example.com"] and body["proxyGateway"] == {"id": 9, "name": "PARTNER-EGRESS-GW"}
    assert body["name"] == "demo-new" and body["rank"] == 7 and body["description"] == "PAC Sync msg" and "order" not in body
    assert any(m_ == "GET" and p == f"{BASE}/proxyGateways" for m_, p, _b in fake.calls)

    direct = {"u_name": "d", "u_forward_method": "direct", "u_proxy_gateway": ""}
    fapi(client, {"rule_order": 2}).create("demo-d", direct, ["a.example.com"], "m")
    body = [b for m_, p, b in fake.calls if m_ == "POST" and p.endswith("/forwardingRules")][-1]
    assert body["forwardMethod"] == "DIRECT" and "proxyGateway" not in body and body["order"] == 2

    zg = fapi(client).read()["demo-partner-egress"]
    fapi(client).update(zg, FWD, ["c.example.com"], "PAC Sync new")
    put = next(b for m_, p, b in fake.calls if m_ == "PUT")
    assert next(p for m_, p, b in fake.calls if m_ == "PUT").endswith("/forwardingRules/5")
    assert put["order"] == 3 and put["rank"] == 7 and put["destAddresses"] == ["c.example.com"] and put["type"] == "FORWARDING"
    fapi(client).delete(zg)
    assert [p for m_, p, _b in fake.calls if m_ == "DELETE"] == [f"{BASE}/forwardingRules/5"]


def test_forwarding_unprefixed_name_and_missing_gateway(fwd):
    client, fake = fwd
    with pytest.raises(ConfigError, match="refusing to write forwarding rule 'evil'"):
        fapi(client).create("evil", FWD, ["a.example.com"], "m")
    assert fake.calls == []
    from pac_sync.deployer import DeployError
    with pytest.raises(DeployError, match="no proxy gateway named 'NOPE'"):
        fapi(client).create("demo-x", dict(FWD, u_proxy_gateway="NOPE"), ["a.example.com"], "m")
    assert not [1 for m_, p, _b in fake.calls if m_ == "POST"]
