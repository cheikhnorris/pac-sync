"""zscaler.pac_name_prefix / only_prefixed: every write on an unprefixed PAC file is refused before any request."""

import pytest

from pac_sync import main as m
from pac_sync import zia_cli
from pac_sync.deployer import ZiaDeployer, effective_pac_name, make_zscaler_client, validate_prefix_config
from pac_sync.envfile import ConfigError
from pac_sync.simzia import SimulatedZscalerClient

from .helpers import PAC, SEED_MESSAGE, add_rule, demo_cfg, pac_file, sim

WRITES = ("add_pac_file", "clone_pac_file", "update_pac_file", "delete_pac_file")


class WriteSpy:
    def __init__(self, target):
        self._target, self.writes = target, []

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if name not in WRITES:
            return attr

        def call(*a, **kw):
            self.writes.append(name)
            return attr(*a, **kw)

        return call


def _guarded(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    plain = ZiaDeployer(client)
    pac_id = plain.bootstrap("emea-proxy.pac", PAC, "example.com", SEED_MESSAGE)  # created without the guard
    other = plain.bootstrap("demo-na-proxy.pac", PAC, "example.com", SEED_MESSAGE)
    client.zia.activate.activate()
    spy = WriteSpy(client.zia.pac_files)
    client.zia.pac_files = spy
    d = ZiaDeployer(client)
    d.prefix, d.only_prefixed = "demo-", True
    return d, spy, pac_id, other


def test_effective_name_and_startup_check():
    assert effective_pac_name({"pac_name_prefix": "demo-"}, "emea-proxy.pac") == "demo-emea-proxy.pac"
    assert effective_pac_name({}, "emea-proxy.pac") == "emea-proxy.pac"
    with pytest.raises(ConfigError, match="pac_name_prefix"):
        validate_prefix_config({"only_prefixed": True})
    with pytest.raises(ConfigError):  # the factory checks it too: nothing is built on a broken guard
        make_zscaler_client({"target": "simulated", "only_prefixed": True})
    validate_prefix_config({"only_prefixed": True, "pac_name_prefix": "x-"})
    validate_prefix_config({"pac_name_prefix": "x-"})  # a prefix without the guard is fine


def test_every_write_is_refused_before_any_request(tmp_path):
    d, spy, pac_id, other = _guarded(tmp_path)
    attempts = [
        lambda: d.stage(pac_id, PAC, "m"),
        lambda: d.deploy(pac_id, 1),
        lambda: d.set_lkg(pac_id, 1),
        lambda: d.remove_lkg(pac_id),
        lambda: d.delete_pac(pac_id),
        lambda: d.probe_write(pac_id, "m"),
        lambda: d.bootstrap("emea-other.pac", PAC, "example.com", "m"),
        lambda: d._action(pac_id, d.versions(pac_id)[0], "UNSTAGE"),
    ]
    assert d.find_pac_id("emea-proxy.pac") == pac_id  # reads are allowed; this is how the name becomes known
    for attempt in attempts:
        with pytest.raises(ConfigError, match="only_prefixed"):
            attempt()
    assert spy.writes == []  # not a single write request left the process
    # and the same calls work on a prefixed file
    assert d.find_pac_id("demo-na-proxy.pac") == other
    d.stage(other, PAC + "// x\n", "m")
    assert spy.writes == ["clone_pac_file"]


def test_guard_off_without_the_flag(tmp_path):
    d, spy, pac_id, other = _guarded(tmp_path)
    d.only_prefixed = False
    d.stage(pac_id, PAC + "// y\n", "m")  # prefix alone is only a naming convention
    assert spy.writes == ["clone_pac_file"]


def test_live_target_auto_bootstraps_prefixed_files(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["zscaler"]["target"] = "live"  # live, but the factory hands out the simulator (no tenant is called)
    client = sim(cfg)
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    assert m.auto_bootstrap_refusal(cfg) is None
    cfg["zscaler"]["only_prefixed"] = False  # same config without the guard: a live target stays cautious
    cfg["deploy"]["live_max_mode"] = "stage"
    assert m.auto_bootstrap_refusal(cfg) is not None
    cfg["zscaler"]["only_prefixed"] = True
    del cfg["deploy"]["live_max_mode"]

    assert m.sync(cfg) == 1  # NA is stage mode: refused, EMEA and APAC are created
    names = sorted(f["name"] for f in client.pac_files_summary())
    assert names == ["demo-apac-proxy.pac", "demo-emea-proxy.pac"]  # every name has the prefix
    summaries = " ".join(d["u_summary"] for d in cfg["_sources"][-1].deployments)
    assert "pac-sync bootstrap --region NA" in summaries


def test_demo_cleanup_deletes_only_prefixed_files(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    plain = ZiaDeployer(client)
    for name in ("demo-emea-proxy.pac", "demo-na-proxy.pac", "emea-proxy.pac", "corp-main.pac"):
        plain.bootstrap(name, PAC, "example.com", SEED_MESSAGE)
    client.zia.activate.activate()
    cfg["zscaler"]["target"] = "live"
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    lines = []
    assert zia_cli.demo_cleanup(cfg, yes=True, out=lines.append) == 0
    assert sorted(f["name"] for f in client.pac_files_summary()) == ["corp-main.pac", "emea-proxy.pac"]
    assert any("deleted demo-emea-proxy.pac" in line for line in lines)
    assert not (tmp_path / "sim" / "demo-emea-proxy.pac").exists()


def test_demo_cleanup_refuses_without_the_guard_or_without_yes(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    ZiaDeployer(client).bootstrap("demo-emea-proxy.pac", PAC, "example.com", SEED_MESSAGE)
    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    cfg["zscaler"]["only_prefixed"] = False
    assert zia_cli.demo_cleanup(cfg, yes=True, out=lambda s: None) == 2
    cfg["zscaler"]["only_prefixed"] = True
    assert zia_cli.demo_cleanup(cfg, yes=False, out=lambda s: None, confirm=lambda p: "delete") == 2  # no console
    assert len(client.pac_files_summary()) == 1


def test_a_sync_never_touches_an_unprefixed_file(tmp_path, monkeypatch):
    """The region maps to emea-proxy.pac; with a prefix the run only ever creates demo-emea-proxy.pac."""
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    ZiaDeployer(client).bootstrap("emea-proxy.pac", PAC, "example.com", SEED_MESSAGE)  # production-like file
    client.zia.activate.activate()
    before = pac_file(client, "emea-proxy.pac")
    add_rule(cfg, tmp_path, "new-app.example.com")
    m.sync(cfg)
    assert pac_file(client, "emea-proxy.pac") == before
    assert pac_file(client, "demo-emea-proxy.pac")["versions"]


def test_unknown_name_is_refused_without_any_request(tmp_path):
    """AC13 literally: the refusal happens before ANY request, a read included."""
    client = SimulatedZscalerClient(str(tmp_path))
    pac_id = ZiaDeployer(client).bootstrap("emea-proxy.pac", PAC, "example.com", SEED_MESSAGE)
    calls = []

    class Everything:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"request {name} before the refusal")

    client.zia.pac_files = Everything()
    d = ZiaDeployer.__new__(ZiaDeployer)
    d.client, d.pac, d.dry_run, d.prefix, d.only_prefixed, d.on_evict = client, client.zia.pac_files, False, "demo-", True, None
    d._cache, d._names = {}, {}
    for attempt in (lambda: d.stage(pac_id, PAC, "m"), lambda: d.deploy(pac_id, 1), lambda: d.set_lkg(pac_id, 1),
                    lambda: d.remove_lkg(pac_id), lambda: d.delete_pac(pac_id), lambda: d.probe_write(pac_id, "m")):
        with pytest.raises(ConfigError, match="its name is not known"):
            attempt()
    assert calls == []
