"""ZIA commit messages: format, 255-character limit, round trip, legacy and foreign messages."""

from datetime import datetime, timezone

import pytest

from pac_sync.zia_messages import MAX_LEN, build_message, parse_commit_message, run_id

RID = run_id(datetime(2026, 11, 1, 10, 20, 30, tzinfo=timezone.utc))
DIGEST = "ab" * 32


def test_run_id_is_utc_compact():
    assert RID == "20261101T102030Z"


def test_format_and_round_trip():
    msg = build_message(RID, "EMEA", ["PACR0001001", "PACR0001002"], ["PACR0001003"], DIGEST, "c" * 40)
    assert msg == "PAC Sync 20261101T102030Z EMEA +PACR0001001,PACR0001002 -PACR0001003 sha256:abababababab git:cccccccccc"
    info = parse_commit_message(msg)
    assert info["pac_sync"] and not info["legacy"]
    assert (info["run_id"], info["code"]) == (RID, "EMEA")
    assert info["added"] == ["PACR0001001", "PACR0001002"] and info["removed"] == ["PACR0001003"]
    assert info["sha256"] == "abababababab" and info["git"] == "cccccccccc"


def test_no_git_part_and_no_rules():
    msg = build_message(RID, "NA", digest=DIGEST)
    assert msg == "PAC Sync 20261101T102030Z NA sha256:abababababab"
    info = parse_commit_message(msg)
    assert info["added"] == [] and info["git"] is None and info["kind"] == ""


@pytest.mark.parametrize("kind", ["bootstrap", "probe"])
def test_kinds(kind):
    info = parse_commit_message(build_message(RID, "APAC", digest=DIGEST, kind=kind))
    assert info["kind"] == kind and info["pac_sync"]


def test_rollback_message():
    msg = build_message(RID, "EMEA", digest=DIGEST, rollback_to=41)
    assert "rollback-to v41" in msg
    assert parse_commit_message(msg)["rollback_to"] == 41


def test_long_lists_collapse_and_stay_within_limit():
    added = [f"PACR{n:07d}" for n in range(1, 80)]
    removed = [f"PACR{n:07d}" for n in range(200, 260)]
    msg = build_message(RID, "EMEA", added, removed, DIGEST, "d" * 40)
    assert len(msg) <= MAX_LEN
    info = parse_commit_message(msg)
    assert info["added_count"] == 79 and info["removed_count"] == 60
    assert info["added"] == ["PACR0000001"] and info["sha256"] == "abababababab" and info["git"] == "dddddddddd"


def test_every_builder_output_fits_255():
    for kind in ("", "bootstrap", "probe"):
        assert len(build_message(RID, "VERY-LONG-REGION-CODE", ["PACR0000001"] * 300, [], DIGEST, "e" * 40, kind=kind)) <= MAX_LEN


def test_legacy_and_foreign_messages():
    legacy = parse_commit_message("PAC Sync 1f4797af91 PAC update: EMEA: +3 -0")
    assert legacy["pac_sync"] and legacy["legacy"] and legacy["run_id"] is None
    for text in ("", None, "edited in the portal", "pac sync lowercase"):
        info = parse_commit_message(text)
        assert not info["pac_sync"] and not info["legacy"]
