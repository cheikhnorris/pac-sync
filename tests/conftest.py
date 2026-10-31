import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pac_sync.models import ProxyProfile, Region  # noqa: E402


@pytest.fixture
def sample():
    return json.loads((ROOT / "config" / "servicenow_sample_data.json").read_text(encoding="utf-8"))


@pytest.fixture
def profiles(sample):
    return {p["sys_id"]: ProxyProfile(p["sys_id"], p["u_name"], p["u_proxy_chain"]) for p in sample["u_pac_proxy_profile"]}


@pytest.fixture
def regions(sample, profiles):
    return {
        r["sys_id"]: Region(r["sys_id"], r["u_code"], r["u_zia_pac_name"], profiles[r["u_default_profile"]].chain)
        for r in sample["u_pac_region"]
    }


@pytest.fixture(autouse=True)
def _restore_environ():
    """load_env_file writes to os.environ: undo it after each test."""
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
