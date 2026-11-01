"""deploy.skip_activate_if_pending, and creating prefixed PAC files so that the next run does not hit a 400."""

import json

from pac_sync import main as m

from .helpers import add_rule, demo_cfg, pac_file, sim, statuses


def _activations(client):
    return client.store.load().get("activations", 0)


def _state(tmp_path):
    return json.loads((tmp_path / "pac-state.json").read_text(encoding="utf-8")) if (tmp_path / "pac-state.json").exists() else {}


def test_run_creates_prefixed_files_activates_and_the_next_run_stages(tmp_path, monkeypatch):
    """AC14 with strict_commit: a clone on a PAC file created but not activated would be a 400 RESOURCE_NOT_FOUND."""
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    assert m.sync(cfg) == 1  # EMEA and APAC (auto) created; NA (stage) is refused and reported
    assert sorted(f["name"] for f in client.pac_files_summary()) == ["demo-apac-proxy.pac", "demo-emea-proxy.pac"]
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "DEPLOYED")]
    assert client.zia.activate.status()[0].status == "ACTIVE"  # created, then activated
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0

    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 0  # no 400: the parent is committed
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "LKG"), (2, "DEPLOYED")]


def test_pending_changes_of_others_are_never_activated(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    assert m.sync(cfg) == 1
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    client.set_foreign_pending()  # another admin has pending changes
    before = _activations(client)

    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 1  # exit 1: saved, not activated
    assert _activations(client) == before  # activate() was never called
    assert statuses(client, "demo-emea-proxy.pac")[-1] == (2, "DEPLOYED")  # but the version is in ZIA
    source = cfg["_sources"][-1]
    row = source.deployments[-1]
    assert "not activated" in row["u_summary"] and row["u_summary"].startswith("PAC Sync ")
    updates = dict(source.rule_updates)
    mine = updates["rnew"]
    # not live yet: the rule is only "staged" with a message and u_state is NOT touched, so the request stays open
    assert mine["u_deploy_status"] == "staged" and "u_state" not in mine and "u_deploy_version" not in mine
    assert mine["u_deploy_message"] == "saved in ZIA as EMEA v2, not activated: activate in the portal; the next run confirms"
    # NA was not written in this run: its rules do not get the note
    assert "not activated" not in updates["r1043"].get("u_deploy_message", "")
    assert updates["r1043"]["u_deploy_status"] == "deployed"
    assert _state(tmp_path)["EMEA"]["not_activated"] == "pending"


def test_the_next_runs_confirm_once_zia_is_active(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    assert m.sync(cfg) == 1
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    client.set_foreign_pending()
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 1
    activations = _activations(client)

    # still pending: the rule must NOT flip to deployed just because the hashes match; exit 1, nothing activated
    assert m.sync(cfg) == 1
    again = dict(cfg["_sources"][-1].rule_updates)["rnew"]
    assert again["u_deploy_status"] == "staged" and "u_state" not in again and "not activated" in again["u_deploy_message"]
    assert _activations(client) == activations and _state(tmp_path)["EMEA"]["not_activated"] == "pending"

    client.zia.activate.activate()  # an admin reviews and activates in the portal
    assert m.sync(cfg) == 0
    final = dict(cfg["_sources"][-1].rule_updates)["rnew"]
    assert final["u_deploy_status"] == "deployed" and final["u_state"] == "deployed"
    assert final["u_deploy_version"] == "EMEA:v2" and final["u_deploy_message"] == ""
    assert "not_activated" not in _state(tmp_path).get("EMEA", {})
    row = next(d for d in cfg["_sources"][-1].deployments if d["u_zia_version"] == 2)
    assert "activation confirmed" in row["u_summary"] and row["u_status"] == "deployed"  # so /pacsync drops its banner


def test_a_failing_activate_still_records_the_state(tmp_path, monkeypatch):
    """The deploy worked and activate() raised: the state must be written, or the next run reads the new DEPLOYED
    version as a manual change."""
    cfg = demo_cfg(tmp_path, monkeypatch, history="git")
    assert m.bootstrap_region(cfg, "NA", yes=True) == 0
    assert m.sync(cfg) == 0
    client = sim(cfg)
    real = client.zia.activate.activate
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("503 from ZIA")

    monkeypatch.setattr(m, "make_zscaler_client", lambda z: client)
    client.zia.activate.activate = boom
    cfg["deploy"]["skip_activate_if_pending"] = False
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 1 and calls
    state = json.loads((tmp_path / "history" / "regions" / "EMEA" / "state.json").read_text(encoding="utf-8"))
    assert state["deployed"]["version"] == 2 and state["not_activated"] == "pending"
    assert "paused_reason" not in state
    assert dict(cfg["_sources"][-1].rule_updates)["rnew"]["u_deploy_status"] == "staged"

    client.zia.activate.activate = real  # ZIA is back: the next run confirms, and does not call this drift
    assert m.sync(cfg) == 0
    state = json.loads((tmp_path / "history" / "regions" / "EMEA" / "state.json").read_text(encoding="utf-8"))
    assert "paused_reason" not in state and "not_activated" not in state
    assert client.zia.activate.status()[0].status == "ACTIVE"
    assert dict(cfg["_sources"][-1].rule_updates)["rnew"]["u_deploy_status"] == "deployed"


def test_notification_and_log_say_not_activated(tmp_path, monkeypatch, caplog):
    cfg = demo_cfg(tmp_path, monkeypatch)
    sim(cfg)
    messages = []
    monkeypatch.setattr(m, "notify", lambda c, text: messages.append(text))
    sim(cfg).set_foreign_pending()
    assert m.sync(cfg) == 1
    assert any("not activated" in t for t in messages)
    assert "not activated" in caplog.text


def test_guard_off_activates_even_with_pending_changes(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    cfg["deploy"]["skip_activate_if_pending"] = False
    client = sim(cfg)
    client.set_foreign_pending()
    assert m.sync(cfg) == 1  # only because NA (stage) is not bootstrapped
    assert client.zia.activate.status()[0].status == "ACTIVE"


def test_region_created_without_activation_waits_then_continues(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    client.set_foreign_pending()
    assert m.sync(cfg) == 1
    assert _state(tmp_path)["EMEA"]["not_activated"] == "created"
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "DEPLOYED")]
    activations = _activations(client)

    # still pending: the next run does not clone on the uncommitted file (that would be a 400), it waits
    add_rule(cfg, tmp_path, "new-app.example.com")
    assert m.sync(cfg) == 1
    assert statuses(client, "demo-emea-proxy.pac") == [(1, "DEPLOYED")]
    assert _activations(client) == activations
    assert "waiting for the activation" in caplog.text

    client.zia.activate.activate()  # an admin reviews and activates in the portal
    m.sync(cfg)  # exit 1 only because NA (stage mode) was never bootstrapped
    assert statuses(client, "demo-emea-proxy.pac")[-1] == (2, "DEPLOYED")
    assert "not_activated" not in _state(tmp_path).get("EMEA", {})


def test_explicit_bootstrap_with_pending_changes_marks_the_region(tmp_path, monkeypatch):
    cfg = demo_cfg(tmp_path, monkeypatch)
    client = sim(cfg)
    client.set_foreign_pending()
    assert m.bootstrap_region(cfg, "NA", yes=True) == 1  # created, not activated
    assert _state(tmp_path)["NA"]["not_activated"] == "created"
    assert pac_file(client, "demo-na-proxy.pac")["versions"][0]["pac_version_status"] == "DEPLOYED"


def test_retire_waits_while_activation_is_pending():
    """A retire request closes only once the PAC without the rule is live (activated), not merely saved."""
    from pac_sync.normalizer import NormalizeResult
    from pac_sync.writeback import compute_rule_updates

    retiring = {"sys_id": "r9", "u_state": "retire_requested", "u_deploy_status": "deployed"}
    result = NormalizeResult(rules_by_region={}, retiring=[retiring])
    states = {"EMEA": {"deployed": {"version": 3, "hash": "h"}}}
    args = (result, {"r9": retiring}, states, {"EMEA": "h"}, "2026-11-01 10:00:00")

    assert compute_rule_updates(*args, not_activated={"EMEA"}) == []
    assert compute_rule_updates(*args)[0][1]["u_state"] == "retired"
