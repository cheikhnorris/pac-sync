"""CLI commands that work on the ZIA versions of a region's PAC file: versions, diff, lkg, demo-cleanup.

Each takes the parsed config and an injectable ``out`` (like main.status), returns the process exit code and needs
no git state: the PAC file is found by its effective name (zscaler.pac_name_prefix + u_zia_pac_name) when the
history has no pac_id. Every write goes through ZiaDeployer, so the prefix guard applies.
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .deployer import to_epoch
from .envfile import ConfigError, stdin_is_interactive
from .main import (_render_region, history_mode, resolve_pac_id, run_lock, skip_activate_if_pending, zia_session)
from .writeback import NOT_ACTIVATED
from .zia_objects import managed_groups
from .zia_history import normalize_content
from .zia_messages import parse_commit_message

log = logging.getLogger(__name__)


def _when(value) -> str:
    epoch = to_epoch(value)
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if epoch else "-"


def _by(version) -> str:
    by = getattr(version, "last_modified_by", None)
    name = by.get("name") if isinstance(by, dict) else getattr(by, "name", None)
    return str(name or "-")


def _pacrs(info: dict) -> str:
    parts = []
    for sign, key in (("+", "added"), ("-", "removed")):
        if info[key + "_count"]:
            shown = ",".join(info[key]) if info[key + "_count"] == len(info[key]) else f"{info[key + '_count']}({info[key][0]}...)"
            parts.append(f"{sign}{shown}")
    return " ".join(parts) or "-"


def versions(cfg: dict, code: str, out: Callable[[str], None] = print) -> int:
    with zia_session(cfg) as (history, deployer, name_for):
        pac_id = resolve_pac_id(history, deployer, name_for, code)
        rows = sorted(deployer.versions(pac_id), key=lambda v: int(v.pac_version))
        out(f"{code}: PAC file id {pac_id}, {len(rows)} version(s)")
        out(f"{'ver':>4}  {'status':<9} {'verified':<12} {'created':<20} {'modified':<20} {'by':<14} {'sync':<4} {'+/- PACR':<28} message")
        for v in rows:
            info = parse_commit_message(getattr(v, "pac_commit_message", ""))
            out(f"{int(v.pac_version):>4}  {str(v.pac_version_status):<9} {str(getattr(v, 'pac_verification_status', '') or '-'):<12} "
                f"{_when(getattr(v, 'create_time', None)):<20} {_when(getattr(v, 'last_modified_time', None)):<20} "
                f"{_by(v)[:14]:<14} {'yes' if info['pac_sync'] else 'no':<4} {_pacrs(info):<28} "
                f"{getattr(v, 'pac_commit_message', '') or ''}")
    return 0


def _content(rows: List, selector: str) -> Optional[str]:
    if selector.isdigit():
        found = next((v for v in rows if int(v.pac_version) == int(selector)), None)
    else:
        status = {"deployed": "DEPLOYED", "staged": "STAGE", "lkg": "LKG"}.get(selector.lower())
        if status is None:
            raise ConfigError(f"unknown version '{selector}': use a number, deployed, staged, lkg or render")
        matches = [v for v in rows if v.pac_version_status == status]
        found = max(matches, key=lambda v: int(v.pac_version)) if matches else None
    return None if found is None else (found.pac_content or "")


def diff(cfg: dict, code: str, src: str, dst: str, out: Callable[[str], None] = print) -> int:
    with zia_session(cfg) as (history, deployer, name_for):
        pac_id = resolve_pac_id(history, deployer, name_for, code)
        rows = deployer.versions(pac_id, with_content=True)
        texts = []
        for selector in (src, dst):
            if selector.lower() == "render":
                rendered = _render_region(cfg, code)
                if rendered is None:
                    log.error("unknown or inactive region %s", code)
                    return 2
                texts.append(rendered[2])
            else:
                text = _content(rows, selector)
                if text is None:
                    log.error("region %s has no %s version", code, selector)
                    return 2
                texts.append(text)
    lines = list(difflib.unified_diff(normalize_content(texts[0]).splitlines(), normalize_content(texts[1]).splitlines(),
                                      f"{code} {src}", f"{code} {dst}", lineterm="", n=2))
    out("\n".join(lines) if lines else f"{code}: {src} and {dst} are identical")
    return 0


def _activate(cfg: dict, deployer, skipped: bool) -> int:
    if skipped:
        log.warning(NOT_ACTIVATED)
        return 1
    if cfg["deploy"].get("activate", True):
        deployer.activate()
    return 0


def lkg(cfg: dict, code: str, version: Optional[int], remove: bool, new_lkg: Optional[int] = None) -> int:
    """Mark ``version`` as the last known good version, or (``remove``) unmark the LKG, optionally naming a new one."""
    if remove == (version is not None) or (new_lkg is not None and not remove):
        raise ConfigError("lkg needs --version N, or --remove [--new-lkg N]")
    with zia_session(cfg) as (history, deployer, name_for):
        with run_lock(history.root):
            pac_id = resolve_pac_id(history, deployer, name_for, code)
            skipped = bool(cfg["deploy"].get("activate", True) and skip_activate_if_pending(cfg)
                           and deployer.activation_pending())
            if remove:
                deployer.remove_lkg(pac_id, new_lkg)
            else:
                deployer.set_lkg(pac_id, version)
            return _activate(cfg, deployer, skipped)


def demo_cleanup(cfg: dict, yes: bool = False, out: Callable[[str], None] = print,
                 confirm: Callable[[str], str] = input) -> int:
    """Delete every PAC file, and every PAC Sync object of an enabled use case, whose name starts with
    zscaler.pac_name_prefix. Needs only_prefixed: true."""
    z = cfg.get("zscaler") or {}
    if not (z.get("only_prefixed") and z.get("pac_name_prefix")):
        log.error("demo-cleanup refused: it needs zscaler.only_prefixed: true and a zscaler.pac_name_prefix")
        return 2
    with zia_session(cfg) as (history, deployer, _name_for):
        prefix = z["pac_name_prefix"]
        files = [f for f in deployer.list_files() if str(f.name).startswith(prefix)]
        objects = managed_groups(cfg, deployer)  # rules first, then categories, inside each adapter's delete
        if not files and not objects:
            out(f"No PAC file starts with '{prefix}': nothing to delete.")
            return 0
        out("PAC files to delete (all their versions):")
        for f in files:
            out(f"  id {f.id}  {f.name}")
        for _api, zg in objects:
            out(f"  ZIA object (PAC Sync, use case)  {zg.name}")
        if not yes:
            if not stdin_is_interactive():
                log.error("demo-cleanup refused: pass --yes when stdin is not interactive")
                return 2
            if confirm(f"Delete these {len(files)} PAC file(s)" + (f" and {len(objects)} ZIA object(s)" if objects else "")
                       + "? Type 'delete' to confirm: ").strip() != "delete":
                log.error("demo-cleanup cancelled")
                return 2
        skipped = bool(cfg["deploy"].get("activate", True) and skip_activate_if_pending(cfg)
                       and deployer.activation_pending())
        for f in files:
            deployer.delete_pac(f.id, name=f.name)
            out(f"deleted {f.name}")
        for api, zg in objects:
            api.delete(zg)
            out(f"deleted {zg.name}")
        rc = _activate(cfg, deployer, skipped)
    if history_mode(cfg) == "zia":
        out("The state file (history.state_file) may still hold pauses for these regions; delete it for a fresh start.")
    return rc
