"""ZiaDeployer.stage at the 10-version cap, the version cache, and the LKG / delete / probe helpers (simulated ZIA)."""

import pytest

from pac_sync.deployer import DeployError, ZiaDeployer
from pac_sync.simzia import MAX_VERSIONS, SimulatedZscalerClient

from .helpers import PAC, SEED_MESSAGE, statuses


def _pac(n):
    return PAC.replace("DIRECT", f"PROXY p{n}:80")


class Spy:
    """Wrap one API object: record every call (name, kwargs)."""

    def __init__(self, target, log):
        self._target, self._log = target, log

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self._log.append((name, kwargs))
            return attr(*args, **kwargs)

        return call


def _spied(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    log = []
    client.zia.pac_files = Spy(client.zia.pac_files, log)
    return client, ZiaDeployer(client), log


def _fill(d, pac_id, statuses_by_version):
    """Create versions 2.. with the given statuses straight through the API (v1 stays DEPLOYED)."""
    for n, status in enumerate(statuses_by_version, start=2):
        _, _, err = d.pac.clone_pac_file(pac_id, "1", _pac(n), status)
        assert err is None


def test_explicit_delete_version_skips_protected_and_reports_before_cloning(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    _fill(d, pac_id, ["LKG"] + ["UNSTAGED"] * 8)  # v1 DEPLOYED, v2 LKG, v3..v10 UNSTAGED
    d._cache.clear()
    order = []
    d.on_evict = lambda v: order.append(("evict", int(v.pac_version), v.pac_version_status))
    log.clear()
    d.stage(pac_id, _pac(11), "new")
    clone = next(kw for name, kw in log if name == "clone_pac_file")
    assert clone["delete_version"] == 3  # not 1 (DEPLOYED, what the SDK would pick) and not 2 (LKG)
    assert order == [("evict", 3, "UNSTAGED")]
    assert len(statuses(client, "p.pac")) == MAX_VERSIONS
    assert 3 not in [v for v, _ in statuses(client, "p.pac")]


def test_previous_stage_becomes_evictable_but_the_new_stage_is_kept(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    _fill(d, pac_id, ["UNSTAGED"] * 7 + ["STAGE", "UNSTAGED"])  # v9 is the staged one, v10 unstaged
    d._cache.clear()
    d.stage(pac_id, _pac(11), "new")
    got = dict(statuses(client, "p.pac"))
    assert got[11] == "STAGE" and got[9] == "UNSTAGED" and got[1] == "DEPLOYED"
    assert 2 not in got  # oldest evictable went, not the DEPLOYED v1


def test_nothing_evictable_raises_before_any_write(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    _fill(d, pac_id, ["LKG"] * 9)
    d._cache.clear()
    log.clear()
    with pytest.raises(DeployError, match="none can be deleted"):
        d.stage(pac_id, _pac(11), "new")
    assert not [name for name, _ in log if name in ("clone_pac_file", "update_pac_file")]


def test_below_the_cap_no_delete_version(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    log.clear()
    d.stage(pac_id, _pac(2), "second")
    clone = next(kw for name, kw in log if name == "clone_pac_file")
    assert "delete_version" not in clone


def test_cache_reduces_gets_and_writes_invalidate(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    gets = lambda: len([1 for name, _ in log if name == "get_pac_file"])  # noqa: E731
    d.versions(pac_id)
    d.versions(pac_id)
    d.deployed_version(pac_id)
    assert gets() == 1
    d.versions(pac_id, with_content=True)  # needs content: one more read, then cached for both
    d.versions(pac_id)
    d.versions(pac_id, with_content=True)
    assert gets() == 2
    d.stage(pac_id, _pac(2), "second")
    before = gets()
    d.versions(pac_id)
    assert gets() == before + 1  # the write dropped the cache


def test_delete_and_probe_helpers(tmp_path):
    client, d, log = _spied(tmp_path)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", SEED_MESSAGE)
    client.zia.activate.activate()
    steps = d.probe_write(pac_id, "PAC Sync 20261101T000000Z X probe")
    assert steps == [("clone", "ok"), ("unstage", "ok")]
    assert [s for _, s in statuses(client, "p.pac")] == ["DEPLOYED", "UNSTAGED"]
    assert client.zia.activate.status()[0].status == "PENDING"  # probe wrote; it did not activate
    d.stage(pac_id, _pac(3), "staged")
    assert d.probe_write(pac_id, "m")[0][0] == "precheck"  # never next to a reviewed STAGE version
    d.delete_pac(pac_id)
    assert d.list_files() == []
