"""envfile fixes after Phase 1: comments after quotes, escapes inside double quotes, default .env, interactive stdin."""

import subprocess
import sys

from pac_sync.envfile import default_env_file, parse_env_file, stdin_is_interactive, write_env_file


def test_comment_after_a_quoted_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text('A="quoted" # comment\nB=\'single quoted\'   # note\nC="has # inside" # and a comment\nD=plain # c\n',
                   encoding="utf-8")
    assert parse_env_file(str(env)) == {"A": "quoted", "B": "single quoted", "C": "has # inside", "D": "plain"}


def test_only_the_five_escapes_are_unescaped_in_double_quotes(tmp_path):
    env = tmp_path / ".env"
    lines = [
        r'KEY="C:\keys\private.pem"',
        r'Esc="a\\b \"q\" \r"',
        r'NL="x\ny"',
        r"SQ='C:\keys\new.pem'",
    ]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    values = parse_env_file(str(env))
    assert values["KEY"] == r"C:\keys\private.pem"  # \k and \p are not escapes: the path is intact
    assert values["Esc"] == 'a\\b "q" \r'
    assert values["NL"] == "x\ny"  # \n IS an escape in double quotes
    assert values["SQ"] == r"C:\keys\new.pem"  # single quotes: nothing is unescaped, safe for any Windows path


def test_a_windows_path_survives_write_then_read(tmp_path):
    env = tmp_path / ".env"
    write_env_file(str(env), {"ZSCALER_PRIVATE_KEY": r"C:\keys\new\tmp.pem"})
    assert parse_env_file(str(env))["ZSCALER_PRIVATE_KEY"] == r"C:\keys\new\tmp.pem"


def test_default_env_file_is_the_projects_not_the_cwd(tmp_path, monkeypatch):
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "config").mkdir(parents=True)
    elsewhere.mkdir()
    (project / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    config = str(project / "config" / "config.yaml")
    assert default_env_file(config) == str((project / ".env").resolve())
    (elsewhere / ".env").write_text("X=2\n", encoding="utf-8")  # the project's file still wins
    assert default_env_file(config) == str((project / ".env").resolve())
    (project / ".env").unlink()  # no project file: fall back to the cwd
    assert default_env_file(config) == ".env"
    (elsewhere / ".env").unlink()  # neither: where setup should create it
    assert default_env_file(config) == str((project / ".env").resolve())


def test_stdin_that_is_not_a_console_is_not_interactive(monkeypatch):
    out = subprocess.run([sys.executable, "-c", "from pac_sync.envfile import stdin_is_interactive as f; print(f())"],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"  # NUL on Windows reports isatty() == True; it is still not a console

    class Fake:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Fake())
    assert stdin_is_interactive() is False
    monkeypatch.setattr(sys, "stdin", None)
    assert stdin_is_interactive() is False
