"""Assumptions about the SDK and requests that make HTTPS_PROXY / NO_PROXY / REQUESTS_CA_BUNDLE work (offline)."""

import socket

import pytest
import requests

from pac_sync.deployer import make_zscaler_client

PROXY = "http://alice:S3cr%2Ft@proxy.corp:8080"
ZCFG = {"client_id_env": "ZSCALER_CLIENT_ID", "client_secret_env": "ZSCALER_CLIENT_SECRET",
        "vanity_domain_env": "ZSCALER_VANITY_DOMAIN"}


@pytest.fixture
def client(monkeypatch):
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy", "NO_PROXY", "no_proxy",
              "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {"ZSCALER_CLIENT_ID": "id", "ZSCALER_CLIENT_SECRET": "secret", "ZSCALER_VANITY_DOMAIN": "acme"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    return make_zscaler_client(ZCFG)


def _response(body, status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.headers["Content-Type"] = "application/json"
    resp.url = "https://acme.zslogin.net/oauth2/v1/token"
    return resp


def test_oneapi_token_request_uses_https_proxy(client, monkeypatch):
    seen = {}

    def post(url, **kwargs):
        seen.update(kwargs)
        return _response(b'{"access_token": "tok", "expires_in": 3600}')

    monkeypatch.setattr("zscaler.oneapi_oauth_client.requests.post", post)
    oauth = client.zia.pac_files._request_executor._oauth
    oauth._access_token = None
    assert oauth._get_access_token() == "tok"
    assert seen["proxies"] == {"http": PROXY, "https": PROXY}


def test_oneapi_api_calls_use_https_proxy(client, monkeypatch):
    http = client.zia.pac_files._request_executor._http_client
    assert http._proxy == PROXY
    seen = {}

    def request(**kwargs):
        seen.update(kwargs)
        return _response(b"{}")

    monkeypatch.setattr("zscaler.oneapi_http_client.requests.request", request)
    http.send_request({"method": "GET", "url": "https://api.zsapi.net/zia/api/v1/pacFiles", "headers": {}, "params": {}, "uuid": "u"})
    assert seen["proxies"] == {"http": PROXY, "https": PROXY}
    assert seen["verify"] is True


def test_requests_env_covers_legacy_and_ca_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    settings = requests.Session().merge_environment_settings("https://zsapi.zscalertwo.net/api/v1/pacFiles", {}, None, True, None)
    assert settings["proxies"]["https"] == PROXY
    assert settings["verify"] == str(bundle)
    assert requests.utils.get_environ_proxies("http://127.0.0.1:8090")["http"] == PROXY  # without NO_PROXY
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    assert requests.utils.get_environ_proxies("http://127.0.0.1:8090") == {}


def test_proxy_error_does_not_leak_credentials(monkeypatch):
    with socket.socket() as s:  # a free localhost port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://u:topsecret@127.0.0.1:{port}")
    with pytest.raises(requests.exceptions.RequestException) as exc:
        requests.get("https://example.invalid", timeout=2)
    assert "topsecret" not in str(exc.value)
