from datetime import datetime, timezone

from pac_sync.normalizer import normalize
from pac_sync.renderer import PacRenderer

from .conftest import ROOT

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _renderer():
    return PacRenderer(str(ROOT / "templates" / "proxy.pac.j2"), True, ["10.0.0.0/8"])


def test_render_emea(sample, regions, profiles):
    res = normalize(sample["u_pac_rule"], regions, profiles, NOW)
    emea = next(r for r in regions.values() if r.code == "EMEA")
    content, digest = _renderer().render(emea, res.rules_by_region["EMEA"])

    assert 'if (h === "corp.local" || dnsDomainIs(h, ".corp.local")) return "DIRECT"; // PACR0001001' in content
    assert 'if (isInNetSafe("10.20.0.0", "255.255.0.0")) return "DIRECT"; // PACR0001002' in content
    assert 'return "PROXY partner-proxy.corp.local:8080; DIRECT"; // PACR0001042' in content
    assert 'return "PROXY ${GATEWAY}:80; PROXY ${SECONDARY_GATEWAY}:80; DIRECT";' in content
    # priority order is kept
    assert content.index("PACR0001001") < content.index("PACR0001002") < content.index("PACR0001042")
    assert len(digest) == 64


def test_render_is_deterministic(sample, regions, profiles):
    res = normalize(sample["u_pac_rule"], regions, profiles, NOW)
    na = next(r for r in regions.values() if r.code == "NA")
    r = _renderer()
    assert r.render(na, res.rules_by_region["NA"]) == r.render(na, list(res.rules_by_region["NA"]))
    reversed_input = normalize(list(reversed(sample["u_pac_rule"])), regions, profiles, NOW)
    assert r.render(na, reversed_input.rules_by_region["NA"]) == r.render(na, res.rules_by_region["NA"])
