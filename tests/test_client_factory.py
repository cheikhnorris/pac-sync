"""make_zscaler_client: target and auth selection, with the SDK classes replaced (no network)."""

import pytest

from pac_sync.deployer import ConfigError, make_zscaler_client

zscaler = pytest.importorskip("zscaler")
from zscaler import oneapi_client  # noqa: E402

SECRETS = {"ZSCALER_CLIENT_SECRET": "oneapi-secret-XYZ", "ZIA_PASSWORD": "legacy-pw-XYZ", "ZIA_API_KEY": "legacy-key-XYZ",
           "ZSCALER_PRIVATE_KEY": "C:/keys/zs-private.pem"}


class Recorder:
    def __init__(self):
        self.configs = []

    def __call__(self, config):
        self.configs.append(config)
        return ("client", config)


@pytest.fixture
def sdk(monkeypatch):
    for name in ("ZSCALER_CLIENT_ID", "ZSCALER_CLIENT_SECRET", "ZSCALER_VANITY_DOMAIN", "ZSCALER_PRIVATE_KEY",
                 "ZIA_USERNAME", "ZIA_PASSWORD", "ZIA_API_KEY", "ZIA_CLOUD"):
        monkeypatch.delenv(name, raising=False)
    oneapi, legacy = Recorder(), Recorder()
    monkeypatch.setattr(zscaler, "ZscalerClient", oneapi)
    monkeypatch.setattr(oneapi_client, "LegacyZIAClient", legacy)
    return oneapi, legacy


def test_simulated_target_is_rejected(sdk):
    with pytest.raises(ConfigError, match="removed"):
        make_zscaler_client({"target": "simulated"})
    assert sdk[0].configs == [] and sdk[1].configs == []


def test_absent_target_is_live_oneapi_secret(sdk, monkeypatch):
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_CLIENT_SECRET", SECRETS["ZSCALER_CLIENT_SECRET"])
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    make_zscaler_client({"client_id_env": "ZSCALER_CLIENT_ID", "client_secret_env": "ZSCALER_CLIENT_SECRET",
                         "vanity_domain_env": "ZSCALER_VANITY_DOMAIN", "cloud": "beta"})
    (config,) = sdk[0].configs
    assert config["clientId"] == "cid" and config["clientSecret"] == SECRETS["ZSCALER_CLIENT_SECRET"]
    assert config["vanityDomain"] == "acme" and config["cloud"] == "beta" and "privateKey" not in config


def test_oneapi_private_key_without_secret(sdk, monkeypatch):
    monkeypatch.setenv("ZSCALER_CLIENT_ID", "cid")
    monkeypatch.setenv("ZSCALER_VANITY_DOMAIN", "acme")
    monkeypatch.setenv("ZSCALER_PRIVATE_KEY", SECRETS["ZSCALER_PRIVATE_KEY"])
    make_zscaler_client({"target": "live", "auth": "oneapi", "private_key_env": "ZSCALER_PRIVATE_KEY"})
    (config,) = sdk[0].configs
    assert config["privateKey"] == SECRETS["ZSCALER_PRIVATE_KEY"] and "clientSecret" not in config
    assert config["clientId"] == "cid" and config["vanityDomain"] == "acme"


def test_legacy_builds_legacy_zia_client(sdk, monkeypatch):
    for k, v in {"ZIA_USERNAME": "api-admin@acme.com", "ZIA_PASSWORD": SECRETS["ZIA_PASSWORD"],
                 "ZIA_API_KEY": SECRETS["ZIA_API_KEY"], "ZIA_CLOUD": "zscalertwo"}.items():
        monkeypatch.setenv(k, v)
    make_zscaler_client({"target": "live", "auth": "legacy"})
    assert sdk[0].configs == []
    (config,) = sdk[1].configs
    assert {k: config[k] for k in ("username", "password", "api_key", "cloud")} == {
        "username": "api-admin@acme.com", "password": SECRETS["ZIA_PASSWORD"],
        "api_key": SECRETS["ZIA_API_KEY"], "cloud": "zscalertwo"}


@pytest.mark.parametrize("cfg,present,missing", [
    ({"auth": "oneapi"}, {"ZSCALER_CLIENT_ID": "cid", "ZSCALER_VANITY_DOMAIN": "acme"}, "ZSCALER_CLIENT_SECRET"),
    ({"auth": "oneapi", "private_key_env": "ZSCALER_PRIVATE_KEY"},
     {"ZSCALER_CLIENT_ID": "cid", "ZSCALER_VANITY_DOMAIN": "acme", "ZSCALER_CLIENT_SECRET": SECRETS["ZSCALER_CLIENT_SECRET"]},
     "ZSCALER_PRIVATE_KEY"),
    ({"auth": "legacy"}, {"ZIA_USERNAME": "u", "ZIA_PASSWORD": SECRETS["ZIA_PASSWORD"], "ZIA_CLOUD": "zscaler"}, "ZIA_API_KEY"),
])
def test_missing_variable_names_it_without_values(sdk, monkeypatch, cfg, present, missing):
    for k, v in present.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError) as err:
        make_zscaler_client(dict(cfg, target="live"))
    message = str(err.value)
    assert missing in message and cfg["auth"] in message and "pac-sync setup" in message
    assert not any(secret in message for secret in SECRETS.values())


def test_unknown_target_or_auth(sdk):
    with pytest.raises(ConfigError):
        make_zscaler_client({"target": "staging"})
    with pytest.raises(ConfigError):
        make_zscaler_client({"target": "live", "auth": "saml"})
