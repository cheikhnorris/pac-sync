"""A rule number is written into the PAC as a comment and parsed back: it must not be able to end that comment."""

import json
from datetime import datetime, timezone

import pytest
import yaml

from pac_sync import main as m
from pac_sync.normalizer import normalize
from pac_sync.renderer import PacRenderer
from pac_sync.zia_history import parse_rules

from .fake_zia import install_fake_zia

from .conftest import ROOT

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
EVIL = "PACR1\nalert(1); //"


def _rule(number, sys_id="evil"):
    return {"sys_id": sys_id, "number": number, "u_action": "direct", "u_match_type": "host_exact",
            "u_value": "evil.example.com", "u_proxy_profile": "", "u_scope": "global", "u_regions": "", "u_priority": "50",
            "u_state": "approved", "u_valid_from": "", "u_valid_to": ""}


@pytest.mark.parametrize("number", [EVIL, "PACR1\r\nx", "PACR 1", "PACR1;", "", "A" * 41, "PACRé1", "PACR1\n"])
def test_bad_numbers_are_rejected_with_a_reason(sample, regions, profiles, number):
    res = normalize(sample["u_pac_rule"] + [_rule(number)], regions, profiles, NOW)
    assert all(r.sys_id != "evil" for r in res.all_rules)
    reasons = [why for rec, why in res.rejected if rec["sys_id"] == "evil"]
    assert reasons and "invalid rule number" in reasons[0]


@pytest.mark.parametrize("number", ["PACR0001099", "a_b-C9", "A" * 40])
def test_good_numbers_pass(sample, regions, profiles, number):
    res = normalize(sample["u_pac_rule"] + [_rule(number)], regions, profiles, NOW)
    assert any(r.number == number for r in res.all_rules)


def test_an_injected_number_never_reaches_a_rendered_pac(sample, regions, profiles):
    res = normalize(sample["u_pac_rule"] + [_rule(EVIL)], regions, profiles, NOW)
    renderer = PacRenderer(str(ROOT / "templates" / "proxy.pac.j2"), True, ["10.0.0.0/8"])
    for region in regions.values():
        content, _ = renderer.render(region, res.rules_by_region[region.code])
        assert "alert(1)" not in content and "evil.example.com" not in content
        assert [r["number"] for r in parse_rules(content)] == [r.number for r in res.rules_by_region[region.code]]


def test_a_sync_reports_it_to_servicenow_and_deploys_nothing_of_it(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    data = json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))
    data["u_pac_rule"].append(_rule(EVIL))
    path = tmp_path / "snow.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    cfg["servicenow"]["sample_file"] = str(path)
    cfg["history"]["repo_path"] = str(tmp_path / "history")
    install_fake_zia(monkeypatch, tmp_path / "sim")
    cfg["zscaler"].update(pac_domain="example.com",
                          bootstrap="missing")
    cfg["regions"] = {"EMEA": {"mode": "auto"}, "NA": {"mode": "auto"}, "APAC": {"mode": "auto"}}
    sources = []
    real = m.make_source

    def capture(c):
        sources.append(real(c))
        return sources[-1]

    monkeypatch.setattr(m, "make_source", capture)
    m.sync(cfg)
    for region in ("EMEA", "NA", "APAC"):
        assert "alert(1)" not in (tmp_path / "history" / "regions" / region / "proxy.pac").read_text(encoding="utf-8")
    patch = dict(sources[-1].rule_updates)["evil"]
    assert patch["u_deploy_status"] == "failed" and "invalid rule number" in patch["u_deploy_message"]
