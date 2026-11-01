from datetime import datetime, timezone

import pytest
import requests

from pac_sync import main, snow_client
from pac_sync.snow_client import ServiceNowClient


class Resp:
    def __init__(self, status, body=b"{}"):
        self.status_code, self.content, self.headers = status, body, {}

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(snow_client.time, "sleep", lambda s: None)
    c = ServiceNowClient({"instance_url": "http://snow", "auth": "none", "tables": {"rule": "u_pac_rule",
                                                                                 "deployment": "u_pac_deployment"}})
    return c


def script(client, outcomes):
    calls = []

    def request(method, url, **kw):
        calls.append(method)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    client._session.request = request
    return calls


def test_patch_retries_a_dropped_connection(client):
    calls = script(client, [requests.ConnectionError("reset"), Resp(200)])
    client.update_rule("r1", {"u_deploy_status": "deployed"})
    assert calls == ["PATCH", "PATCH"]


def test_post_is_not_resent_after_a_timeout(client):
    calls = script(client, [requests.Timeout("slow"), Resp(201)])
    with pytest.raises(requests.Timeout):
        client.log_deployment({})
    assert calls == ["POST"]


def test_a_401_fetches_a_new_oauth_token_once(client, monkeypatch):
    client._cfg["auth"] = "oauth"
    tokens = []
    monkeypatch.setattr(client, "_ensure_token", lambda: tokens.append(client._token_expiry))
    calls = script(client, [Resp(401), Resp(200)])
    client._token_expiry = 9e18
    client.update_rule("r1", {})
    assert calls == ["PATCH", "PATCH"] and tokens[-1] == 0.0


class Source:
    def __init__(self, fail):
        self.fail, self.sent = fail, []

    def update_rule(self, sys_id, body):
        if sys_id in self.fail:
            raise self.fail[sys_id]
        self.sent.append(sys_id)

    def log_deployment(self, body):
        pass


def write_back(monkeypatch, source):
    monkeypatch.setattr(main, "compute_rule_updates", lambda *a: [("r1", {}), ("r2", {}), ("r3", {})])
    return main._write_back({"servicenow": {}}, source, None, [], {}, [], "", datetime.now(timezone.utc))


def test_write_back_stops_when_servicenow_is_down(monkeypatch):
    source = Source({"r1": requests.ConnectionError("down")})
    assert "3 rule update(s)" in write_back(monkeypatch, source)
    assert source.sent == []


def test_write_back_goes_on_after_one_refused_record(monkeypatch):
    source = Source({"r2": requests.HTTPError("400", response=Resp(400))})
    assert write_back(monkeypatch, source) == "1 write(s), see the log"
    assert source.sent == ["r1", "r3"]


def test_write_back_success_reports_nothing(monkeypatch):
    assert write_back(monkeypatch, Source({})) == ""
