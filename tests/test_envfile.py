"""envfile: .env parsing, precedence, rewriting, and that values never reach logs or errors."""

import logging
import os

import pytest

from pac_sync.envfile import ConfigError, describe_env, load_env_file, parse_env_file, require_env, write_env_file

SECRET = "s3cr3t-Value-#not-a-comment"


def test_parse_quotes_comments_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "PLAIN=abc\n"
        "export EXPORTED=yes\n"
        "INLINE=value # trailing comment\n"
        "DQ=\"with space # kept\"\n"
        "SQ='single $quoted'\n"
        "ESC=\"line1\\nline2 \\\"q\\\"\"\n"
        "EMPTY=\n"
        "not a line\n"
        "1BAD=x\n",
        encoding="utf-8",
    )
    values = parse_env_file(str(env))
    assert values == {
        "PLAIN": "abc", "EXPORTED": "yes", "INLINE": "value", "DQ": "with space # kept",
        "SQ": "single $quoted", "ESC": 'line1\nline2 "q"', "EMPTY": "",
    }


def test_process_env_wins_and_names_returned(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PS_TEST_A=from-file\nPS_TEST_B=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PS_TEST_A", "from-process")
    monkeypatch.delenv("PS_TEST_B", raising=False)
    assert load_env_file(str(env)) == ["PS_TEST_B"]
    assert os.environ["PS_TEST_A"] == "from-process"
    assert os.environ["PS_TEST_B"] == "from-file"


def test_missing_file_loads_nothing(tmp_path):
    assert load_env_file(str(tmp_path / "nope.env")) == []


def test_write_preserves_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my notes\nKEEP=1\nexport CHANGE=old\n", encoding="utf-8")
    write_env_file(str(env), {"CHANGE": "new value", "ADDED": SECRET})
    text = env.read_text(encoding="utf-8")
    assert text.splitlines()[:3] == ["# my notes", "KEEP=1", 'CHANGE="new value"']
    assert parse_env_file(str(env)) == {"KEEP": "1", "CHANGE": "new value", "ADDED": SECRET}


def test_multiline_value_round_trips(tmp_path):
    env = tmp_path / ".env"
    pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    write_env_file(str(env), {"ZSCALER_PRIVATE_KEY": pem})
    assert parse_env_file(str(env))["ZSCALER_PRIVATE_KEY"] == pem


def test_values_never_logged_or_in_errors(tmp_path, monkeypatch, caplog):
    env = tmp_path / ".env"
    env.write_text(f"PS_TEST_SECRET={SECRET!r}\n", encoding="utf-8")
    monkeypatch.delenv("PS_TEST_SECRET", raising=False)
    with caplog.at_level(logging.DEBUG):
        names = load_env_file(str(env))
    assert names == ["PS_TEST_SECRET"]
    assert SECRET not in caplog.text
    assert describe_env("PS_TEST_SECRET") == f"set ({len(SECRET)} chars)"
    monkeypatch.delenv("PS_TEST_SECRET")
    with pytest.raises(ConfigError) as err:
        require_env("PS_TEST_SECRET", "unit test")
    assert "PS_TEST_SECRET" in str(err.value) and SECRET not in str(err.value)

