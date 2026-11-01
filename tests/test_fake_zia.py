"""Fake ZIA (tests/fake_zia.py): SDK-shaped state machine, persisted on disk, driven through the real ZiaDeployer."""

import json

from pac_sync.deployer import ZiaDeployer
from .fake_zia import MAX_VERSIONS, SimulatedZscalerClient, safe_dir_name

PAC = 'function FindProxyForURL(url, host) { return "DIRECT"; }\n'


def _pac(n):
    return PAC.replace("DIRECT", f"PROXY p{n}:80")


def _statuses(client, pac_id):
    versions, _, err = client.zia.pac_files.get_pac_file(pac_id)
    assert err is None
    return [(v.pac_version, v.pac_version_status) for v in versions]


def test_bootstrap_stage_deploy_unstage(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    d = ZiaDeployer(client)
    pac_id = d.bootstrap("emea-proxy.pac", PAC, "example.com", "first")
    assert d.find_pac_id("emea-proxy.pac") == pac_id
    assert _statuses(client, pac_id) == [(1, "DEPLOYED")]
    assert (tmp_path / "emea-proxy.pac" / "deployed.pac").read_text(encoding="utf-8") == PAC

    v2 = d.stage(pac_id, _pac(2), "second")
    assert v2 == 2
    v3 = d.stage(pac_id, _pac(3), "third")  # replaces the staged v2
    assert _statuses(client, pac_id) == [(1, "DEPLOYED"), (2, "UNSTAGED"), (3, "STAGE")]

    d.deploy(pac_id, v3)
    assert _statuses(client, pac_id) == [(1, "LKG"), (2, "UNSTAGED"), (3, "DEPLOYED")]
    assert (tmp_path / "emea-proxy.pac" / "deployed.pac").read_text(encoding="utf-8") == _pac(3)
    assert (tmp_path / "emea-proxy.pac" / "v2.pac").read_text(encoding="utf-8") == _pac(2)
    d.activate()
    assert json.loads((tmp_path / "pacfiles.json").read_text(encoding="utf-8"))["activations"] == 1


def test_eviction_keeps_ten_and_never_the_deployed(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    d = ZiaDeployer(client)
    pac_id = d.bootstrap("na-proxy.pac", PAC, "example.com", "first")
    for n in range(2, 14):
        d.stage(pac_id, _pac(n), f"v{n}")
    versions = _statuses(client, pac_id)
    assert len(versions) == MAX_VERSIONS
    assert versions[0] == (1, "DEPLOYED")  # oldest, but deployed: kept
    assert [v for v, _ in versions[1:]] == list(range(5, 14))  # 2..4 evicted, numbers never reused
    assert not (tmp_path / "na-proxy.pac" / "v2.pac").exists()


def test_persistence_across_instances(tmp_path):
    a = ZiaDeployer(SimulatedZscalerClient(str(tmp_path)))
    pac_id = a.bootstrap("apac-proxy.pac", PAC, "example.com", "first")
    a.stage(pac_id, _pac(2), "second")
    b = ZiaDeployer(SimulatedZscalerClient(str(tmp_path)))
    assert b.find_pac_id("apac-proxy.pac") == pac_id
    assert b.deployed_version(pac_id) == 1
    b.deploy(pac_id, 2)
    assert a.deployed_version(pac_id) == 2


def test_errors_are_sdk_tuples_and_validation(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    pac = client.zia.pac_files
    assert pac.get_pac_file(99)[2]
    assert pac.list_pac_files()[0] == []
    assert not (tmp_path / "pacfiles.json").exists()  # reads never create state
    result, _, err = pac.validate_pac_file("var x = 1;")
    assert err is None and result.success is False and result.get("success") is False
    first, _, _ = pac.add_pac_file(name="x.pac", domain="example.com", pac_content=PAC)
    assert pac.add_pac_file(name="x.pac", domain="example.com", pac_content=PAC)[2]
    listed, _, _ = pac.list_pac_files(query_params={"search": "x.pac", "filter": "pac_content"})
    assert [(f.id, f.name, f.pac_content) for f in listed] == [(first.id, "x.pac", None)]


def test_pac_names_stay_inside_state_dir(tmp_path):
    assert safe_dir_name("emea-proxy.pac") == "emea-proxy.pac"  # plain names keep their folder
    evil = safe_dir_name("../../evil")
    assert evil.startswith("_.._evil-") and "/" not in evil and not evil.startswith("..")
    assert safe_dir_name("..").startswith("_-")
    client = SimulatedZscalerClient(str(tmp_path / "sim"))
    client.zia.pac_files.add_pac_file(name="../escape.pac", domain="example.com", pac_content=PAC)
    assert not (tmp_path / "escape.pac").exists()
    assert list((tmp_path / "sim").glob("*/deployed.pac"))


def test_folder_names_never_collide(tmp_path):
    names = ["a b.pac", "a_b.pac", "A_B.pac", "a/b.pac", "nul"]
    folders = {safe_dir_name(n) for n in names}
    assert len(folders) == len(names)
    client = SimulatedZscalerClient(str(tmp_path))
    for n in names:
        client.zia.pac_files.add_pac_file(name=n, domain="example.com", pac_content=PAC + f"// {n}" + chr(10))
    for n in names:
        assert (tmp_path / safe_dir_name(n) / "deployed.pac").read_text(encoding="utf-8") == PAC + f"// {n}" + chr(10)


# --- Phase 1b: protected eviction, LKG, SDK field names, activation status --------------------------------------

def _versions(client, pac_id):
    return {v.pac_version: v for v in client.zia.pac_files.get_pac_file(pac_id)[0]}


def test_protected_versions_are_never_evicted(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    api = client.zia.pac_files
    pac_id = api.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)[0].id
    api.clone_pac_file(pac_id, "1", _pac(2), "LKG")
    for n in range(3, 11):
        api.clone_pac_file(pac_id, "1", _pac(n), "UNSTAGED")
    assert len(_versions(client, pac_id)) == MAX_VERSIONS
    # explicit deleteVersion on a protected version is refused, nothing changes
    assert api.clone_pac_file(pac_id, "1", _pac(11), "STAGE", delete_version=1)[2]
    assert api.clone_pac_file(pac_id, "1", _pac(11), "STAGE", delete_version=2)[2]
    assert len(_versions(client, pac_id)) == MAX_VERSIONS
    # without deleteVersion the simulator does what the SDK sends: the LOWEST version, and v1 is DEPLOYED -> refused
    assert "cannot be deleted" in api.clone_pac_file(pac_id, "1", _pac(11), "STAGE")[2]
    assert len(_versions(client, pac_id)) == MAX_VERSIONS
    # an explicit, evictable deleteVersion is honoured
    assert api.clone_pac_file(pac_id, "1", _pac(12), "UNSTAGED", delete_version=6)[2] is None
    assert 6 not in _versions(client, pac_id)


def test_lkg_and_remove_lkg(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    d = ZiaDeployer(client)
    pac_id = d.bootstrap("p.pac", PAC, "example.com", "first")
    for n in (2, 3, 4):
        d.stage(pac_id, _pac(n), f"v{n}")
    d.deploy(pac_id, 4)  # v1 was DEPLOYED -> now v4; previous deployed (1) becomes LKG
    assert _statuses(client, pac_id) == [(1, "LKG"), (2, "UNSTAGED"), (3, "UNSTAGED"), (4, "DEPLOYED")]
    d.set_lkg(pac_id, 3)  # one LKG only: the old one becomes UNSTAGED
    assert _statuses(client, pac_id)[0:3] == [(1, "UNSTAGED"), (2, "UNSTAGED"), (3, "LKG")]
    d.remove_lkg(pac_id, new_lkg=2)
    assert _statuses(client, pac_id) == [(1, "UNSTAGED"), (2, "LKG"), (3, "UNSTAGED"), (4, "DEPLOYED")]
    d.remove_lkg(pac_id)
    assert all(s != "LKG" for _, s in _statuses(client, pac_id))
    assert client.zia.pac_files.update_pac_file(pac_id, 4, "LKG")[2]  # the DEPLOYED version cannot be LKG
    assert client.zia.pac_files.update_pac_file(pac_id, 1, "REMOVE_LKG")[2]  # not an LKG version


def test_sdk_field_names_and_legacy_state_file(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    v = client.zia.pac_files.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)[0]
    assert isinstance(v.last_modified_time, int) and isinstance(v.create_time, int)
    assert v.last_modified_by == {"id": 0, "name": "pac-sync-sim"}
    # a Phase 1 state file (ISO string under last_modification_time) is read once and converted
    data = json.loads((tmp_path / "pacfiles.json").read_text(encoding="utf-8"))
    for ver in data["files"]["1"]["versions"]:
        ver.pop("last_modified_time"), ver.pop("create_time"), ver.pop("last_modified_by")
        ver["last_modification_time"] = "2026-10-31T22:39:25Z"
    (tmp_path / "pacfiles.json").write_text(json.dumps(data), encoding="utf-8")
    old = client.zia.pac_files.get_pac_file(1)[0][0]
    assert old.last_modified_time == 1793486365 and old.create_time == old.last_modified_time
    assert old.last_modified_by["name"] == "pac-sync-sim"


def test_activation_status_pending_and_active(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    status = lambda: client.zia.activate.status()[0].status  # noqa: E731
    assert status() == "ACTIVE"
    client.zia.pac_files.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)
    assert status() == "PENDING"  # an unactivated write
    client.zia.activate.activate()
    assert status() == "ACTIVE"
    client.set_foreign_pending()  # test hook: someone else's pending change
    assert status() == "PENDING"
    client.zia.activate.activate()
    assert status() == "ACTIVE"


def test_strict_commit_rejects_clone_until_activation(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path), strict_commit=True)
    api = client.zia.pac_files
    pac_id = api.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)[0].id
    assert "RESOURCE_NOT_FOUND" in api.clone_pac_file(pac_id, "1", _pac(2), "STAGE")[2]
    client.zia.activate.activate()
    assert api.clone_pac_file(pac_id, "1", _pac(2), "STAGE")[2] is None


def test_delete_pac_file_removes_folder(tmp_path):
    client = SimulatedZscalerClient(str(tmp_path))
    pac_id = client.zia.pac_files.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)[0].id
    assert (tmp_path / "p.pac").is_dir()
    assert client.zia.pac_files.delete_pac_file(pac_id)[2] is None
    assert not (tmp_path / "p.pac").exists() and client.zia.pac_files.list_pac_files()[0] == []


def test_default_delete_is_the_lowest_version_whatever_its_status(tmp_path):
    """The SDK's own default at the cap is min(pac_versions): the simulator no longer hides that (it once deleted the LKG)."""
    client = SimulatedZscalerClient(str(tmp_path))
    api = client.zia.pac_files
    pac_id = api.add_pac_file(name="p.pac", domain="example.com", pac_content=PAC)[0].id
    for n in range(2, 11):
        api.clone_pac_file(pac_id, "1", _pac(n), "UNSTAGED")
    api.update_pac_file(pac_id, 3, "DEPLOY")  # v1 -> LKG
    assert "is LKG and cannot be deleted" in api.clone_pac_file(pac_id, "3", _pac(11), "STAGE")[2]  # lowest = v1 = LKG
    assert 1 in _versions(client, pac_id)
    api.update_pac_file(pac_id, 1, "REMOVE_LKG")  # v1 is now UNSTAGED, still the lowest
    assert api.clone_pac_file(pac_id, "3", _pac(11), "STAGE")[2] is None
    assert 1 not in _versions(client, pac_id)  # the lowest went, as the SDK would have it
