"""Credentials from a `.env` file (stdlib only) and required environment variables.

The config only names environment variables (`*_env` keys). Their values come from the process environment or from
a git-ignored `.env` file next to the config. The process environment always wins over `.env`.
Values are never logged: functions here return or report variable names only.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """Invalid or incomplete configuration (missing env var, unknown option). The message never holds a secret."""


_DQ = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')
_SQ = re.compile(r"^'([^']*)'\s*(?:#.*)?$")
_ESC = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}


def _unquote(raw: str) -> str:
    r"""Quoted values may be followed by ` # comment`. In double quotes only \\ \" \n \r \t are escapes, so
    C:\keys\private.pem survives (\n and \t ARE escapes: put such paths in single quotes)."""
    raw = raw.strip()
    m = _DQ.match(raw)
    if m:
        return re.sub(r'\\([\\"nrt])', lambda e: _ESC[e.group(1)], m.group(1))
    m = _SQ.match(raw)
    if m:
        return m.group(1)
    hit = re.search(r"\s#", raw)  # unquoted: an inline comment starts at " #"
    return (raw[: hit.start()] if hit else raw).strip()


def _parse_line(line: str) -> Optional[Tuple[str, str]]:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export "):].lstrip()
    key, sep, value = text.partition("=")
    key = key.strip()
    if not sep or not _KEY.match(key):
        return None
    return key, _unquote(value)


def parse_env_file(path: str) -> Dict[str, str]:
    """Return KEY -> value from a .env file (missing file = empty)."""
    p = Path(path)
    if not p.is_file():
        return {}
    values: Dict[str, str] = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]
    return values


def load_env_file(path: str) -> List[str]:
    """Copy variables from ``path`` into os.environ without overriding existing ones. Return the names loaded."""
    loaded = []
    for key, value in parse_env_file(path).items():
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _quote(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_./:@+,=-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def write_env_file(path: str, values: Mapping[str, str]) -> None:
    """Set ``values`` in the .env file: existing keys are rewritten in place, other lines are kept, new keys appended."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8-sig").splitlines() if p.is_file() else []
    pending = dict(values)
    out = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] in pending:
            out.append(f"{parsed[0]}={_quote(pending.pop(parsed[0]))}")
        else:
            out.append(line)
    out.extend(f"{k}={_quote(v)}" for k, v in pending.items())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def require_env(name: str, purpose: str) -> str:
    """Return the value of env var ``name`` or raise ConfigError naming it (never the value)."""
    value = os.environ.get(name, "") if name else ""
    if not value:
        raise ConfigError(
            f"environment variable {name or '(unnamed)'} is not set ({purpose}). "
            "Set it in the environment or in .env, or run `pac-sync setup`."
        )
    return value


def describe_env(name: str) -> str:
    """'set (N chars)' or 'not set': safe to print, never shows the value."""
    value = os.environ.get(name, "")
    return f"set ({len(value)} chars)" if value else "not set"


def default_env_file(config_path: str) -> str:
    """The `.env` of the project that owns ``config_path``: <config folder>/.. first, then the current directory.
    Returns the project-root path when neither exists (where `setup` should create it)."""
    root = Path(config_path).resolve().parent.parent / ".env"
    if root.is_file():
        return str(root)
    cwd = Path(".env")
    return str(cwd) if cwd.is_file() else str(root)


def stdin_is_interactive() -> bool:
    """True only for a real console: not a pipe, not a file, not Windows NUL (which reports isatty() == True)."""
    stream = sys.stdin
    if stream is None or not stream.isatty():
        return False
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt

            mode = ctypes.c_uint32()
            return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))
        except Exception:  # cannot tell (e.g. a test double): trust isatty()
            return True
    return True
