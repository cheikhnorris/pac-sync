"""`pac-sync doctor`: read-only checks of the whole chain, one OK / WARN / FAIL line each.

Nothing here writes to ServiceNow, git or ZIA (validate_pac_file is a check-only POST). Secret values are never
printed: environment variables are reported as "set (N chars)" or "not set". Exit 0 when no check FAILs, else 1.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .deployer import PacFileNotFound, ZiaDeployer, close_zscaler_client, make_zscaler_client, zscaler_target
from .envfile import describe_env
from .history import redact

OK, WARN, FAIL = "OK", "WARN", "FAIL"
Check = Tuple[str, str, str]  # (status, label, detail)


@dataclass
class Context:
    config_path: str
    env_file: str
    cfg: Optional[dict] = None
    source_data: Optional[tuple] = None  # (regions, profiles, raw_rules) once ServiceNow answered
    modes: Dict[str, str] = field(default_factory=dict)


def _git(args: List[str], cwd: Optional[str] = None, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


# -- individual checks: each returns a list of (status, label, detail) ------------------------------------------

def check_python(ctx: Context) -> List[Check]:
    v = sys.version_info
    checks = [(OK if (3, 10) <= v[:2] else FAIL, "python",
               f"{platform.python_version()} ({sys.executable})" + ("" if v[:2] >= (3, 10) else ", need >= 3.10"))]
    try:
        from importlib.metadata import version

        checks.append((OK, "zscaler-sdk-python", version("zscaler-sdk-python")))
    except Exception:
        live = ctx.cfg is None or zscaler_target(ctx.cfg.get("zscaler") or {}) == "live"
        checks.append((FAIL if live else WARN, "zscaler-sdk-python", "not installed (pip install -e .)"))
    return checks


def check_config(ctx: Context) -> List[Check]:
    from .main import auto_bootstrap_refusal, bootstrap_mode, live_max_mode, load_config, region_mode

    try:
        ctx.cfg = load_config(ctx.config_path)
        if not isinstance(ctx.cfg, dict):
            raise ValueError("not a YAML mapping")
    except Exception as exc:
        ctx.cfg = None
        return [(FAIL, "config", f"{ctx.config_path}: {exc}. Run `pac-sync setup`.")]
    cfg = ctx.cfg
    checks: List[Check] = [(OK, "config", str(Path(ctx.config_path).resolve()))]
    template = Path(cfg.get("render", {}).get("template", ""))
    checks.append((OK if template.is_file() else FAIL, "template", str(template) + ("" if template.is_file() else " not found")))
    try:
        zcfg = cfg.get("zscaler") or {}
        target = zscaler_target(zcfg)
        cap = live_max_mode(cfg)
        detail = f"target {target}" + (f", auth {zcfg.get('auth') or 'oneapi'}" if target == "live" else
                                         f", state in {(zcfg.get('simulated') or {}).get('state_dir') or './sim-zia'}")
        detail += f", bootstrap {bootstrap_mode(cfg)}" + (f", live_max_mode {cap}" if cap else "")
        refusal = auto_bootstrap_refusal(cfg)
        if bootstrap_mode(cfg) == "missing" and refusal:
            detail += f" (automatic bootstrap disabled: {refusal})"
        checks.append((OK, "zia target", detail))
        codes = sorted(set((cfg.get("regions") or {}).keys()))
        ctx.modes = {c: region_mode(cfg, c) for c in codes}
        configured = {c: (cfg.get("regions") or {}).get(c, {}).get("mode") or cfg["deploy"].get("default_mode", "stage")
                      for c in codes}
        shown = ", ".join(f"{c} {m}" + (f" (capped from {configured[c]})" if configured[c] != m else "")
                          for c, m in ctx.modes.items())
        checks.append((OK, "region modes", (shown or "none configured") +
                       f"; default {cfg['deploy'].get('default_mode', 'stage')}"))
    except Exception as exc:
        checks.append((FAIL, "config values", str(exc)))
    return checks


def zia_env_names(zcfg: dict) -> List[str]:
    if zscaler_target(zcfg) == "simulated":
        return []
    if (zcfg.get("auth") or "oneapi") == "legacy":
        return [zcfg.get("username_env", "ZIA_USERNAME"), zcfg.get("password_env", "ZIA_PASSWORD"),
                zcfg.get("api_key_env", "ZIA_API_KEY"), zcfg.get("legacy_cloud_env", "ZIA_CLOUD")]
    names = [zcfg.get("client_id_env", "ZSCALER_CLIENT_ID"), zcfg.get("vanity_domain_env", "ZSCALER_VANITY_DOMAIN")]
    names.append(zcfg["private_key_env"] if zcfg.get("private_key_env") else zcfg.get("client_secret_env", "ZSCALER_CLIENT_SECRET"))
    return names


def _zia_needed(ctx: Context) -> bool:
    return any(m != "paused" for m in ctx.modes.values()) or \
        (ctx.cfg or {}).get("deploy", {}).get("default_mode", "stage") != "paused"


def check_env(ctx: Context) -> List[Check]:
    cfg = ctx.cfg
    checks: List[Check] = []
    snow = cfg.get("servicenow") or {}
    names: List[Tuple[str, str]] = []
    if snow.get("source") != "file":
        if snow.get("auth", "oauth") == "basic":
            names += [(snow.get("username_env", ""), "servicenow basic"), (snow.get("password_env", ""), "servicenow basic")]
        else:
            names += [(snow.get("client_id_env", ""), "servicenow oauth"), (snow.get("client_secret_env", ""), "servicenow oauth")]
    zcfg = cfg.get("zscaler") or {}
    try:
        if _zia_needed(ctx):
            names += [(n, f"zscaler {zcfg.get('auth') or 'oneapi'}") for n in zia_env_names(zcfg)]
    except Exception:
        pass  # reported by check_config
    for name, why in names:
        state = describe_env(name)
        checks.append((OK if state != "not set" else FAIL, f"env {name or '(unnamed)'}",
                       f"{state} [{why}]" + ("" if state != "not set" else "; set it in .env or run `pac-sync setup`")))
    if not names:
        checks.append((OK, "env", "no credentials needed (file source, simulated ZIA)"))
    return checks


def check_env_file(ctx: Context) -> List[Check]:
    path = Path(ctx.env_file)
    if not path.is_file():
        return [(OK, "env file", f"{path} not present (credentials come from the environment)")]
    folder = str(path.resolve().parent)
    try:
        inside = _git(["rev-parse", "--is-inside-work-tree"], cwd=folder)
    except (OSError, subprocess.SubprocessError):
        return [(WARN, "env file", f"{path} present; git not available to check that it is ignored")]
    if inside.returncode != 0:
        return [(OK, "env file", f"{path} present, not inside a git repository")]
    tracked = _git(["ls-files", "--error-unmatch", path.name], cwd=folder)
    if tracked.returncode == 0:
        return [(FAIL, "env file", f"{path} is TRACKED by git: `git rm --cached {path.name}` and add it to .gitignore")]
    ignored = _git(["check-ignore", "-q", path.name], cwd=folder)
    if ignored.returncode != 0:
        return [(FAIL, "env file", f"{path} is not git-ignored: add it to .gitignore")]
    return [(OK, "env file", f"{path} present and git-ignored")]


def check_servicenow(ctx: Context) -> List[Check]:
    from .main import load_source_data
    from .snow_client import ServiceNowClient, make_source

    snow = ctx.cfg.get("servicenow") or {}
    checks: List[Check] = []
    try:
        source = make_source(snow)
    except Exception as exc:
        return [(FAIL, "servicenow", f"cannot create the source: {exc}")]
    if isinstance(source, ServiceNowClient):
        for key in ("region", "proxy_profile", "rule"):
            table = snow["tables"][key]
            try:
                source.probe(table)
                checks.append((OK, f"servicenow {table}", f"read OK ({snow['instance_url']})"))
            except Exception as exc:
                checks.append((FAIL, f"servicenow {table}", f"{snow.get('instance_url')}: {exc}"))
        if any(c[0] == FAIL for c in checks):
            return checks
    else:
        checks.append((OK, "servicenow", f"file source {snow.get('sample_file')}"))
    try:
        ctx.source_data = load_source_data(source)
        regions, _profiles, rules = ctx.source_data
        codes = ", ".join(sorted(r.code for r in regions.values())) or "none"
        checks.append((OK if regions else WARN, "servicenow data", f"active regions {codes}; {len(rules)} rule(s) to sync"))
    except Exception as exc:
        checks.append((FAIL, "servicenow data", str(exc)))
    return checks


def check_history(ctx: Context) -> List[Check]:
    hcfg = ctx.cfg.get("history") or {}
    repo = Path(hcfg.get("repo_path", ""))
    try:
        ver = _git(["--version"])
        if ver.returncode != 0:
            raise OSError(ver.stderr)
    except (OSError, subprocess.SubprocessError):
        return [(FAIL, "git", "not found: install Git (winget install Git.Git)")]
    checks: List[Check] = [(OK, "git", ver.stdout.strip())]
    if not (repo / ".git").exists():
        if repo.exists() and any(repo.iterdir()):
            return checks + [(FAIL, "history repo", f"{repo} exists but is not a git repository")]
        checks.append((OK, "history repo", f"{repo} will be created by the first run"))
    else:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo)).stdout.strip() or "?"
        dirty = [line for line in _git(["status", "--porcelain", "--", "regions"], cwd=str(repo)).stdout.splitlines() if line]
        checks.append((OK if not dirty else WARN, "history repo",
                       f"{repo} on {branch}, " + ("regions/ clean" if not dirty else
                                                  f"{len(dirty)} uncommitted change(s) under regions/ (the next run commits them)")))
        lock = repo / ".pac-sync.lock"
        if lock.exists():
            checks.append((WARN, "history lock", f"{lock} exists: a run or backup is in progress, or one died"))
    if hcfg.get("push"):
        remote = hcfg.get("remote", "origin")
        url = _git(["remote", "get-url", remote], cwd=str(repo)) if (repo / ".git").exists() else None
        if url is None or url.returncode != 0:
            checks.append((FAIL, "history push", f"push: true but remote '{remote}' is not configured"))
        else:
            try:
                probe = _git(["ls-remote", "--exit-code", remote, "HEAD"], cwd=str(repo), timeout=60)
                ok = probe.returncode in (0, 2)  # 2 = reachable, empty repository
                checks.append((OK if ok else FAIL, "history push",
                               f"{remote} = {redact(url.stdout.strip())} " +
                               ("reachable" if ok else f"unreachable: {redact(probe.stderr.strip())}")))
            except subprocess.TimeoutExpired:
                checks.append((FAIL, "history push", f"{remote}: git ls-remote timed out"))
    else:
        checks.append((OK, "history push", "off (history stays local)"))
    return checks


def check_zia(ctx: Context) -> List[Check]:
    from .main import auto_bootstrap_refusal, bootstrap_mode, make_history, make_renderer, region_mode
    from .normalizer import normalize

    cfg = ctx.cfg
    if not _zia_needed(ctx):
        return [(OK, "zia", "not used: every region is paused")]
    zcfg = cfg.get("zscaler") or {}
    try:
        client = make_zscaler_client(zcfg)
    except Exception as exc:  # ConfigError for a missing variable, ValueError from the SDK constructor
        return [(FAIL, "zia client", str(exc))]
    checks: List[Check] = [(OK, "zia client", type(client).__name__)]
    try:
        deployer = ZiaDeployer(client)
        try:
            value, _resp, error = deployer.pac.list_pac_files(query_params={"filter": "pac_content"})
            if error:
                raise RuntimeError(error)
            checks.append((OK, "zia list_pac_files", f"{len(value or [])} PAC file(s)"))
        except Exception as exc:
            return checks + [(FAIL, "zia list_pac_files", str(exc))]

        try:
            value, _resp, error = client.zia.activate.status()
            if error:
                raise RuntimeError(error)
            checks.append((OK, "zia activation status", str(getattr(value, "status", value))))
        except Exception as exc:
            checks.append((FAIL, "zia activation status", str(exc)))

        if ctx.source_data is None:
            return checks + [(WARN, "zia regions", "skipped: ServiceNow data unavailable")]
        regions, profiles, raw_rules = ctx.source_data
        result = normalize(raw_rules, regions, profiles)
        renderer = make_renderer(cfg)
        history = make_history(cfg, push=False)
        refusal = auto_bootstrap_refusal(cfg)
        for region in sorted(regions.values(), key=lambda r: r.code):
            label = f"zia {region.code}"
            if region_mode(cfg, region.code) == "paused":
                checks.append((OK, label, "paused: not checked"))
                continue
            try:
                pac_id = deployer.find_pac_id(region.zia_pac_name)
                version = deployer.deployed_version(pac_id)
                if version is None:
                    checks.append((FAIL, label, f"PAC file '{region.zia_pac_name}' (id {pac_id}) has no DEPLOYED version"))
                else:
                    recorded = history.read_state(region.code).get("pac_id")
                    if recorded and int(recorded) != int(pac_id):
                        checks.append((WARN, label, f"'{region.zia_pac_name}' is id {pac_id} in ZIA but state.json "
                                                    f"records {recorded}: fix regions/{region.code}/state.json"))
                    else:
                        checks.append((OK, label, f"'{region.zia_pac_name}' id {pac_id}, deployed v{version}"))
            except PacFileNotFound:
                if refusal is None:
                    checks.append((WARN, label, f"no PAC file '{region.zia_pac_name}' yet: the next run creates it "
                                                f"(zscaler.bootstrap: {bootstrap_mode(cfg)})"))
                else:
                    checks.append((FAIL, label, f"region {region.code}: no ZIA PAC file named '{region.zia_pac_name}'. "
                                                f"Run `pac-sync bootstrap --region {region.code}` ({refusal})"))
            except Exception as exc:
                checks.append((FAIL, label, f"region {region.code}: {exc}"))
            try:
                content, _ = renderer.render(region, result.rules_by_region.get(region.code, []))
                value, _resp, error = deployer.pac.validate_pac_file(content)
                success = getattr(value, "success", None) if value is not None else None
                if error or not success:
                    detail = error or getattr(value, "message", None) or "rejected"
                    checks.append((FAIL, f"zia validate {region.code}", f"current render rejected: {detail}"))
                else:
                    checks.append((OK, f"zia validate {region.code}", "current render passes validate_pac_file"))
            except Exception as exc:
                checks.append((FAIL, f"zia validate {region.code}", str(exc)))
    finally:
        close_zscaler_client(client)
    return checks


def run_doctor(config_path: str, env_file: str = ".env", out: Callable[[str], None] = print) -> int:
    ctx = Context(config_path=config_path, env_file=env_file)
    results: List[Check] = []

    def emit(checks: List[Check]) -> None:
        for status, label, detail in checks:
            results.append((status, label, detail))
            out(f"[{status:^4}] {label}: {detail}")

    emit(check_config(ctx))
    emit(check_python(ctx))
    if ctx.cfg is not None:
        for check in (check_env, check_env_file, check_servicenow, check_history, check_zia):
            try:
                emit(check(ctx))
            except Exception as exc:  # a broken check must not hide the others
                emit([(FAIL, check.__name__.replace("check_", ""), f"unexpected error: {exc}")])
    else:
        emit(check_env_file(ctx))
    counts = {s: sum(1 for r in results if r[0] == s) for s in (OK, WARN, FAIL)}
    out(f"doctor: {counts[OK]} OK, {counts[WARN]} WARN, {counts[FAIL]} FAIL")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_doctor(os.environ.get("PAC_SYNC_CONFIG", "config/config.yaml")))
