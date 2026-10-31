"""ZIA PAC file operations through zscaler-sdk-python (client.zia.pac_files), or the simulator in simzia.py.

`make_zscaler_client` picks the target from `zscaler.target` (simulated | live, absent = live) and, for live,
the auth mode from `zscaler.auth` (oneapi | legacy, absent = oneapi). Every client exposes `.zia.pac_files` and
`.zia.activate`, so ZiaDeployer does not care which one it drives.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .envfile import ConfigError, require_env

log = logging.getLogger(__name__)

TARGETS = ("simulated", "live")
AUTH_MODES = ("oneapi", "legacy")


class DeployError(RuntimeError):
    pass


class PacFileNotFound(DeployError):
    """No ZIA PAC file has the region's name: it needs a bootstrap."""


def zscaler_target(cfg: dict) -> str:
    target = (cfg or {}).get("target") or "live"
    if target not in TARGETS:
        raise ConfigError(f"zscaler.target must be one of {', '.join(TARGETS)}, not '{target}'")
    return target


def make_zscaler_client(cfg: dict):
    """Build the ZIA client for `zscaler.target` / `zscaler.auth`. Missing env vars raise ConfigError (name only)."""
    if zscaler_target(cfg) == "simulated":
        from .simzia import SimulatedZscalerClient

        return SimulatedZscalerClient((cfg.get("simulated") or {}).get("state_dir") or "./sim-zia")

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
    """End the SDK session. The legacy client deauthenticates here, which may also activate pending changes."""
    exit_ = getattr(client, "__exit__", None)
    if exit_ is None:
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


class ZiaDeployer:
    """Stage, deploy, roll back and activate PAC file versions.

    ZIA keeps at most 10 versions per PAC file; clone_pac_file evicts the oldest
    automatically, which is why git (not ZIA) is the history of record.
    """

    def __init__(self, client, dry_run: bool = False):
        self.client = client
        self.pac = client.zia.pac_files if client else None
        self.dry_run = dry_run

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
        return matches.pop()

    def bootstrap(self, name: str, content: str, domain: str, message: str) -> int:
        """Create the PAC file with ``content`` as its first, DEPLOYED version. Return the new pac_id."""
        if not domain:
            raise DeployError("zscaler.pac_domain is not set: ZIA needs a domain to create a PAC file")
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
        log.info("Bootstrapped PAC file '%s' (id %s)", name, pac_id)
        return int(pac_id)

    def deployed_version(self, pac_id: int) -> Optional[int]:
        found = self._by_status(self.versions(pac_id), "DEPLOYED")
        return int(found.pac_version) if found is not None else None

    def versions(self, pac_id: int, with_content: bool = False) -> List:
        params = None if with_content else {"filter": "pac_content"}
        return _unwrap(self.pac.get_pac_file(pac_id, query_params=params), "get_pac_file")

    @staticmethod
    def _by_status(versions: List, status: str) -> Optional[object]:
        found = [v for v in versions if v.pac_version_status == status]
        return max(found, key=lambda v: int(v.pac_version)) if found else None

    def stage(self, pac_id: int, content: str, message: str) -> int:
        """Create a new STAGE version from the deployed one. Return its version number."""
        if self.dry_run:
            log.info("[dry-run] would stage PAC %s: %s", pac_id, message)
            return -1
        versions = self.versions(pac_id, with_content=True)
        deployed = self._by_status(versions, "DEPLOYED")
        if deployed is None:
            raise DeployError(f"PAC {pac_id} has no DEPLOYED version to branch from")
        old_stage = self._by_status(versions, "STAGE")
        if old_stage is not None:
            # Keep at most one staged version: the newest render replaces the previous one.
            self._action(pac_id, old_stage, "UNSTAGE")

        created = _unwrap(
            self.pac.clone_pac_file(
                pac_id=pac_id,
                pac_version=str(deployed.pac_version),
                pac_content=content,
                pac_version_status="STAGE",
                pac_commit_message=message[:255],
            ),
            "clone_pac_file",
        )
        version = getattr(created, "pac_version", None)
        if version is None:  # response did not echo the version: take the newest
            version = max(int(v.pac_version) for v in self.versions(pac_id))
        log.info("Staged PAC %s version %s", pac_id, version)
        return int(version)

    def deploy(self, pac_id: int, version: int) -> None:
        """Promote an existing version (staged, or an older one for rollback) to DEPLOYED."""
        if self.dry_run:
            log.info("[dry-run] would deploy PAC %s version %s", pac_id, version)
            return
        target = next((v for v in self.versions(pac_id, with_content=True) if int(v.pac_version) == version), None)
        if target is None:
            raise DeployError(f"PAC {pac_id} has no version {version} (ZIA keeps only the last 10)")
        self._action(pac_id, target, "DEPLOY")
        log.info("Deployed PAC %s version %s", pac_id, version)

    def _action(self, pac_id: int, version_obj, action: str) -> None:
        # The action endpoint takes the version's own fields in the body (see update_pac_file docstring).
        body = {
            "name": version_obj.name,
            "description": getattr(version_obj, "description", None),
            "domain": version_obj.domain,
            "pac_commit_message": version_obj.pac_commit_message,
            "pac_verification_status": "VERIFY_NOERR",
            "pac_content": getattr(version_obj, "pac_content", None),
        }
        _unwrap(
            self.pac.update_pac_file(
                pac_id=pac_id,
                pac_version=int(version_obj.pac_version),
                pac_version_action=action,
                **{k: v for k, v in body.items() if v is not None},
            ),
            f"update_pac_file {action}",
        )

    def activate(self) -> None:
        if self.dry_run:
            log.info("[dry-run] would activate ZIA configuration")
            return
        _unwrap(self.client.zia.activate.activate(), "activate")
        log.info("ZIA configuration activated")
