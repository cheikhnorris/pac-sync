from datetime import datetime, timezone

import pytest

from pac_sync.normalizer import normalize, validate_value

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "match_type,value,ok",
    [
        ("host_exact", "intranet.corp.local", True),
        ("host_exact", "https://intranet.corp.local", False),
        ("host_exact", "*.corp.local", False),
        ("domain_suffix", "corp.local", True),
        ("domain_suffix", "co.uk", False),
        ("domain_suffix", "localhost", False),
        ("ip_cidr", "10.20.0.0/16", True),
        ("ip_cidr", "10.20.1.0/16", False),
        ("ip_cidr", "0.0.0.0/0", False),
        ("url_pattern", "https://sso.partner-example.com/*", True),
        ("url_pattern", "*partner*", False),
        ("url_pattern", "https://a.com/'x", False),
        ("host_exact", 'evil.com"; return "DIRECT', False),
    ],
)
def test_validate_value(match_type, value, ok):
    assert (validate_value(match_type, value) is None) == ok


def test_sample_data_classification(sample, regions, profiles):
    res = normalize(sample["u_pac_rule"], regions, profiles, NOW)

    numbers = {r.number for r in res.all_rules}
    assert numbers == {"PACR0001001", "PACR0001002", "PACR0001042", "PACR0001043"}
    assert [r["number"] for r in res.future] == ["PACR0001050"]
    assert [r["number"] for r in res.expired] == ["PACR0001051"]
    assert [r["number"] for r in res.retiring] == ["PACR0001052"]

    rejected = {rec["number"]: why for rec, why in res.rejected}
    assert "plain host name" in rejected["PACR0001060"]
    assert rejected["PACR0001061"].startswith("conflicts with PACR0001001")


def test_scope_and_lowercase(sample, regions, profiles):
    res = normalize(sample["u_pac_rule"], regions, profiles, NOW)
    by_region = {code: [r.number for r in rules] for code, rules in res.rules_by_region.items()}
    assert by_region["EMEA"] == ["PACR0001001", "PACR0001002", "PACR0001042"]
    assert by_region["NA"] == ["PACR0001001", "PACR0001042", "PACR0001043"]
    assert by_region["APAC"] == ["PACR0001001"]
    rule = next(r for r in res.all_rules if r.number == "PACR0001042")
    assert rule.value == "portal.partner-example.com"
    assert rule.chain.startswith("PROXY partner-proxy")


def test_proxy_rule_needs_profile(regions, profiles):
    rec = {"sys_id": "x", "number": "PACR9", "u_action": "proxy", "u_match_type": "host_exact",
           "u_value": "a.example.com", "u_proxy_profile": "", "u_scope": "global", "u_priority": "10",
           "u_state": "approved"}
    res = normalize([rec], regions, profiles, NOW)
    assert res.rejected and "proxy profile" in res.rejected[0][1]
