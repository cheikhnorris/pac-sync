"""Simulated ZIA: SDK-shaped state machine, persisted on disk, driven through the real ZiaDeployer."""

import json

from pac_sync.deployer import ZiaDeployer
from pac_sync.simzia import MAX_VERSIONS, SimulatedZscalerClient, safe_dir_name

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
    assert safe_dir_name("../../evil") == "_.._evil"
    assert safe_dir_name("..") == "_"
    client = SimulatedZscalerClient(str(tmp_path / "sim"))
    client.zia.pac_files.add_pac_file(name="../escape.pac", domain="example.com", pac_content=PAC)
    assert not (tmp_path / "escape.pac").exists()
    assert list((tmp_path / "sim").glob("*/deployed.pac"))
