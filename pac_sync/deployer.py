"""ZIA PAC file operations through zscaler-sdk-python (client.zia.pac_files).

`make_zscaler_client` picks the auth mode from `zscaler.auth` (oneapi | legacy, absent = oneapi). Every client
exposes `.zia.pac_files` and `.zia.activate`, so ZiaDeployer does not care which one it drives.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .envfile import ConfigError, require_env

log = logging.getLogger(__name__)

TARGETS = ("live",)
AUTH_MODES = ("oneapi", "legacy")
MAX_VERSIONS = 10  # ZIA keeps at most 10 versions per PAC file
PROTECTED = ("DEPLOYED", "LKG", "STAGE")  # never deleted to make room


class DeployError(RuntimeError):
    pass


class PacFileNotFound(DeployError):
    """No ZIA PAC file has the region's name: it needs a bootstrap."""


def zscaler_target(cfg: dict) -> str:
    target = (cfg or {}).get("target") or "live"
    if target not in TARGETS:
        raise ConfigError(f"zscaler.target '{target}' was removed: PAC Sync only drives a real ZIA tenant; "
                          "run `pac-sync setup --force` to write a live config with a demo- PAC name prefix")
    return target


def make_zscaler_client(cfg: dict):
    """Build the ZIA client for `zscaler.auth`. Missing env vars raise ConfigError (name only)."""
    validate_prefix_config(cfg)
    zscaler_target(cfg)
    auth = cfg.get("auth") or "oneapi"
    if auth == "oneapi":
        why = "zscaler.auth=oneapi"
        config = {
            "clientId": require_env(cfg.get("client_id_env", "ZSCALER_CLIENT_ID"), why),
            "vanityDomain": require_env(cfg.get("vanity_domain_env", "ZSCALER_VANITY_DOMAIN"), why),
            "logging": {"enabled": False, "verbose": False},
        }
        if cfg.get("private_key_env"):
            config["privateKey"] = require_env(cfg["private_key_env"], why + " with private_key_env")
        else:
            config["clientSecret"] = require_env(cfg.get("client_secret_env", "ZSCALER_CLIENT_SECRET"), why)
        if cfg.get("cloud"):
            config["cloud"] = cfg["cloud"]
        import zscaler  # imported lazily so render/tests work without the SDK

        return zscaler.ZscalerClient(config)
    if auth == "legacy":
        why = "zscaler.auth=legacy"
        config = {
            "username": require_env(cfg.get("username_env", "ZIA_USERNAME"), why),
            "password": require_env(cfg.get("password_env", "ZIA_PASSWORD"), why),
            "api_key": require_env(cfg.get("api_key_env", "ZIA_API_KEY"), why),
            "cloud": require_env(cfg.get("legacy_cloud_env", "ZIA_CLOUD"), why),
            "logging": {"enabled": False, "verbose": False},
        }
        from zscaler import oneapi_client

        return oneapi_client.LegacyZIAClient(config)
    raise ConfigError(f"zscaler.auth must be one of {', '.join(AUTH_MODES)}, not '{auth}'")


def close_zscaler_client(client) -> None:
    """End the SDK session, unless ZIA has pending changes.

    Both SDK clients end the ZIA session on close (legacy always, OneAPI after any write: request_executor.deauthenticate)
    and ZIA activates pending changes when a session ends, so closing would activate what skip_activate_if_pending left.
    """
    exit_ = getattr(client, "__exit__", None)
    if exit_ is None:
        return
    if ZiaDeployer(client).activation_pending():
        log.warning("ZIA has pending changes: the API session is left to expire instead of being closed, because closing "
                    "it would activate them. Review and activate them in the ZIA portal.")
        return
    try:
        exit_(None, None, None)
    except Exception as exc:  # closing must never hide the run's own result
        log.warning("closing the ZIA client failed: %s", exc)


def _unwrap(result, what: str):
    value, _response, error = result
    if error:
        raise DeployError(f"{what} failed: {error}")
    return value


def effective_pac_name(zcfg: dict, base: str) -> str:
    """The ZIA PAC file name for a region: `zscaler.pac_name_prefix` + `u_zia_pac_name`."""
    return f"{(zcfg or {}).get('pac_name_prefix') or ''}{base}"


def validate_prefix_config(zcfg: dict) -> None:
    if (zcfg or {}).get("only_prefixed") and not (zcfg or {}).get("pac_name_prefix"):
        raise ConfigError("zscaler.only_prefixed: true needs a non-empty zscaler.pac_name_prefix")


def assert_prefixed(prefix: str, only_prefixed: bool, name: str, what: str = "PAC file") -> None:
    """The prefix guard for any ZIA object: with only_prefixed on, a name lacking the prefix raises before any request."""
    if not only_prefixed:
        return
    if not prefix:
        raise ConfigError("zscaler.only_prefixed: true needs a non-empty zscaler.pac_name_prefix")
    if not str(name).startswith(prefix):
        raise ConfigError(f"refusing to write {what} '{name}': zscaler.only_prefixed is on and the name does not "
                          f"start with '{prefix}'")


def to_epoch(value) -> Optional[int]:
    """ZIA timestamps: epoch seconds or milliseconds (format unverified), or an ISO string. Return epoch seconds."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return int(number / 1000) if number > 1e11 else int(number)


class ZiaDeployer:
    """Stage, deploy, roll back and activate PAC file versions.

    ZIA keeps at most 10 versions per PAC file. `stage` picks the version to delete itself (never DEPLOYED, LKG or
    STAGE; the SDK would otherwise delete the lowest number) and reports it through ``on_evict`` first.
    ``prefix`` / ``only_prefixed`` (set by main from zscaler.pac_name_prefix / only_prefixed) make every write on a
    PAC file whose name lacks the prefix raise ConfigError before any request is sent. The versions list is cached
    per pac_id for the life of the instance; every write drops it.
    """

    def __init__(self, client):
        self.client = client
        self.pac = client.zia.pac_files if client else None
        self.prefix = ""
        self.only_prefixed = False
        self.on_evict: Optional[Callable[[object], None]] = None
        self._cache: Dict[int, Tuple[bool, List]] = {}
        self._names: Dict[int, str] = {}

    # -- guard ---------------------------------------------------------------------------------------------------
    def assert_writable(self, pac_id: Optional[int] = None, name: Optional[str] = None) -> None:
        if not self.only_prefixed:
            return
        if not self.prefix:
            raise ConfigError("zscaler.only_prefixed: true needs a non-empty zscaler.pac_name_prefix")
        if name is None:
            name = self._names.get(pac_id)
            if name is None:  # never ask ZIA here: a refusal must not be preceded by a request
                raise ConfigError(f"refusing to write PAC id {pac_id}: zscaler.only_prefixed is on and its name is not known, "
                                  "so the guard cannot check it (look it up by name first: find_pac_id)")
        assert_prefixed(self.prefix, self.only_prefixed, name)

    # -- reads ---------------------------------------------------------------------------------------------------
    def list_files(self) -> List:
        files = _unwrap(self.pac.list_pac_files(query_params={"filter": "pac_content"}), "list_pac_files")
        for f in files or []:
            self._names[f.id] = f.name
        return list(files or [])

    def find_pac_id(self, name: str) -> int:
        files = _unwrap(
            self.pac.list_pac_files(query_params={"search": name, "filter": "pac_content"}), "list_pac_files"
        )
        matches = {f.id for f in files or [] if f.name == name}
        if not matches:
            raise PacFileNotFound(
                f"no ZIA PAC file named '{name}'. Create it once with `pac-sync bootstrap --region <CODE>` "
                "(or in the ZIA admin portal) before the first run."
            )
        if len(matches) > 1:
            raise DeployError(f"expected exactly one ZIA PAC file named '{name}', found {len(matches)}")
        pac_id = matches.pop()
        self._names[pac_id] = name
        return pac_id

    def versions(self, pac_id: int, with_content: bool = False) -> List:
        cached = self._cache.get(pac_id)
        if cached and (cached[0] or not with_content):
            return cached[1]
        params = None if with_content else {"filter": "pac_content"}
        found = _unwrap(self.pac.get_pac_file(pac_id, query_params=params), "get_pac_file")
        self._cache[pac_id] = (with_content, found)
        return found

    @staticmethod
    def _by_status(versions: List, status: str) -> Optional[object]:
        found = [v for v in versions if v.pac_version_status == status]
        return max(found, key=lambda v: int(v.pac_version)) if found else None

    def deployed_info(self, pac_id: int) -> Optional[object]:
        """The DEPLOYED version object (message, times, author), or None."""
        return self._by_status(self.versions(pac_id), "DEPLOYED")

    def deployed_version(self, pac_id: int) -> Optional[int]:
        found = self.deployed_info(pac_id)
        return int(found.pac_version) if found is not None else None

    def activation_pending(self) -> bool:
        """True unless activate.status() says ACTIVE. Unreadable status counts as pending (the safe answer)."""
        try:
            value = _unwrap(self.client.zia.activate.status(), "activate status")
            return str(getattr(value, "status", value)).upper() != "ACTIVE"
        except Exception as exc:
            log.warning("cannot read the ZIA activation status, treating it as pending: %s", exc)
            return True

    # -- writes --------------------------------------------------------------------------------------------------
    def bootstrap(self, name: str, content: str, domain: str, message: str) -> int:
        """Create the PAC file with ``content`` as its first, DEPLOYED version. Return the new pac_id."""
        if not domain:
            raise DeployError("zscaler.pac_domain is not set: ZIA needs a domain to create a PAC file")
        self.assert_writable(name=name)
        try:
            created = _unwrap(
                self.pac.add_pac_file(
                    name=name,
                    description=f"Managed by PAC Sync: {name}",
                    domain=domain,
                    pac_commit_message=message[:255],
                    pac_verification_status="VERIFY_NOERR",
                    pac_version_status="DEPLOYED",
                    pac_content=content,
                ),
                "add_pac_file",
            )
        except DeployError:
            raise
        except Exception as exc:  # the SDK raises (not returns) on PAC validation failure
            raise DeployError(f"add_pac_file failed: {exc}") from exc
        pac_id = getattr(created, "id", None)
        if pac_id is None:  # response did not echo the id: look it up
            pac_id = self.find_pac_id(name)
        self._names[int(pac_id)] = name
        log.info("Bootstrapped PAC file '%s' (id %s)", name, pac_id)
        return int(pac_id)

    def stage(self, pac_id: int, content: str, message: str) -> int:
        """Create a new STAGE version from the deployed one. Return its version number."""
        self.assert_writable(pac_id)
        versions = self.versions(pac_id, with_content=True)
        deployed = self._by_status(versions, "DEPLOYED")
        if deployed is None:
            raise DeployError(f"PAC {pac_id} has no DEPLOYED version to branch from")
        old_stage = self._by_status(versions, "STAGE")
        if old_stage is not None:
            # Keep at most one staged version: the newest render replaces the previous one.
            self._action(pac_id, old_stage, "UNSTAGE")
            versions = self.versions(pac_id, with_content=True)

        extra = self._make_room(pac_id, versions)

        created = _unwrap(
            self.pac.clone_pac_file(
                pac_id=pac_id,
                pac_version=str(deployed.pac_version),
                pac_content=content,
                pac_version_status="STAGE",
                pac_commit_message=message[:255],
                **extra,
            ),
            "clone_pac_file",
        )
        self._cache.pop(pac_id, None)
        version = getattr(created, "pac_version", None)
        if version is None:  # response did not echo the version: take the newest
            version = max(int(v.pac_version) for v in self.versions(pac_id))
        log.info("Staged PAC %s version %s", pac_id, version)
        return int(version)

    def _make_room(self, pac_id: int, versions: List) -> dict:
        """At the 10-version cap: choose the version to delete (never DEPLOYED, LKG or STAGE), report it, and return the
        clone kwargs that name it. The SDK alone would delete the LOWEST version whatever its status."""
        if len(versions) < MAX_VERSIONS:
            return {}
        evictable = [v for v in versions if v.pac_version_status not in PROTECTED]
        if not evictable:
            raise DeployError(f"PAC {pac_id} has {len(versions)} versions and none can be deleted (DEPLOYED, LKG "
                              "and STAGE are protected): remove the LKG mark or delete versions in the portal")
        gone = min(evictable, key=lambda v: int(v.pac_version))
        log.warning('evicting v%s (%s, "%s") from PAC %s', gone.pac_version, gone.pac_version_status,
                    gone.pac_commit_message, pac_id)
        if self.on_evict:
            self.on_evict(gone)
        return {"delete_version": int(gone.pac_version)}

    def deploy(self, pac_id: int, version: int) -> None:
        """Promote an existing version (staged, or an older one for rollback) to DEPLOYED."""
        self.assert_writable(pac_id)
        target = next((v for v in self.versions(pac_id, with_content=True) if int(v.pac_version) == version), None)
        if target is None:
            raise DeployError(f"PAC {pac_id} has no version {version} (ZIA keeps only the last 10)")
        self._action(pac_id, target, "DEPLOY")
        log.info("Deployed PAC %s version %s", pac_id, version)

    def set_lkg(self, pac_id: int, version: int) -> None:
        self.assert_writable(pac_id)
        target = next((v for v in self.versions(pac_id, with_content=True) if int(v.pac_version) == version), None)
        if target is None:
            raise DeployError(f"PAC {pac_id} has no version {version}")
        self._action(pac_id, target, "LKG")

    def remove_lkg(self, pac_id: int, new_lkg: Optional[int] = None) -> None:
        self.assert_writable(pac_id)
        current = self._by_status(self.versions(pac_id, with_content=True), "LKG")
        if current is None:
            raise DeployError(f"PAC {pac_id} has no LKG version")
        self._action(pac_id, current, "REMOVE_LKG", new_lkg=new_lkg)

    def delete_pac(self, pac_id: int, name: Optional[str] = None) -> None:
        self.assert_writable(pac_id, name=name)
        _unwrap(self.pac.delete_pac_file(pac_id), "delete_pac_file")
        self._cache.pop(pac_id, None)

    def probe_write(self, pac_id: int, message: str) -> List[Tuple[str, str]]:
        """Clone the deployed version as STAGE, then UNSTAGE it. Return [(step, "ok" | error)]. Does not activate (the
        caller does, and only when nothing else was pending). At the cap it deletes the same safe version stage() would."""
        self.assert_writable(pac_id)
        steps: List[Tuple[str, str]] = []
        try:
            current = self.versions(pac_id, with_content=True)
            deployed = self._by_status(current, "DEPLOYED")
            if deployed is None:
                return [("read", "no DEPLOYED version")]
            if self._by_status(current, "STAGE") is not None:
                return [("precheck", "a STAGE version already exists: promote or unstage it first")]
            extra = self._make_room(pac_id, current)
            created = _unwrap(self.pac.clone_pac_file(
                pac_id=pac_id, pac_version=str(deployed.pac_version), pac_content=deployed.pac_content,
                pac_version_status="STAGE", pac_commit_message=message[:255], **extra), "clone_pac_file")
        except Exception as exc:
            return steps + [("clone", str(exc))]
        self._cache.pop(pac_id, None)
        steps.append(("clone", "ok"))
        try:
            number = getattr(created, "pac_version", None) or self._by_status(
                self.versions(pac_id, with_content=True), "STAGE").pac_version
            probe = next(v for v in self.versions(pac_id, with_content=True) if int(v.pac_version) == int(number))
            self._action(pac_id, probe, "UNSTAGE")
            steps.append(("unstage", "ok"))
        except Exception as exc:
            steps.append(("unstage", str(exc)))
        return steps

    def _action(self, pac_id: int, version_obj, action: str, new_lkg: Optional[int] = None) -> None:
        self.assert_writable(pac_id)
        # The action endpoint takes the version's own fields in the body (see update_pac_file docstring).
        body = {
            "name": version_obj.name,
            "description": getattr(version_obj, "description", None),
            "domain": version_obj.domain,
            "pac_commit_message": version_obj.pac_commit_message,
            "pac_verification_status": "VERIFY_NOERR",
            "pac_content": getattr(version_obj, "pac_content", None),
        }
        extra = {"new_lkg_ver": int(new_lkg)} if new_lkg is not None else {}
        _unwrap(
            self.pac.update_pac_file(
                pac_id=pac_id,
                pac_version=int(version_obj.pac_version),
                pac_version_action=action,
                **extra,
                **{k: v for k, v in body.items() if v is not None},
            ),
            f"update_pac_file {action}",
        )
        self._cache.pop(pac_id, None)

    def activate(self) -> None:
        _unwrap(self.client.zia.activate.activate(), "activate")
        log.info("ZIA configuration activated")
