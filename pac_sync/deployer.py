"""ZIA PAC file operations through zscaler-sdk-python (client.zia.pac_files)."""

from __future__ import annotations

import logging
import os
from typing import List, Optional

log = logging.getLogger(__name__)


class DeployError(RuntimeError):
    pass


def make_zscaler_client(cfg: dict):
    from zscaler import ZscalerClient  # imported lazily so render/tests work without the SDK

    config = {
        "clientId": os.environ[cfg["client_id_env"]],
        "clientSecret": os.environ[cfg["client_secret_env"]],
        "vanityDomain": os.environ[cfg["vanity_domain_env"]],
        "logging": {"enabled": False, "verbose": False},
    }
    if cfg.get("cloud"):
        config["cloud"] = cfg["cloud"]
    return ZscalerClient(config)


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
        matches = {f.id for f in files if f.name == name}
        if len(matches) != 1:
            raise DeployError(
                f"expected exactly one ZIA PAC file named '{name}', found {len(matches)}. "
                "Create it once in the ZIA admin portal (bootstrap) before the first run."
            )
        return matches.pop()

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
