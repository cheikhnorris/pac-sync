# PAC Sync

Python service: ServiceNow PAC rules -> validated, per-region PAC files -> git history (`pac-history` repo) -> Zscaler ZIA
(via `zscaler-sdk-python`) -> status write-back to ServiceNow. See README.md for the full picture.

## Commands

```powershell
.venv\Scripts\python -m pytest -q                                       # all tests (must pass)
.venv\Scripts\python -m pac_sync --config config\config.mock.yaml run --dry-run
.venv\Scripts\python -m pac_sync --config config\config.mock.yaml render --region EMEA
```

- Python 3.11 venv in `.venv` (the SDK needs >= 3.10). The SDK is installed editable from `..\zscaler-sdk-python`.
- Local ServiceNow: `..\snow-mock` (PHP, `powershell -ExecutionPolicy Bypass -File ..\snow-mock\start.ps1`, http://127.0.0.1:8090).
  Env for the mock: `SNOW_CLIENT_ID=pac-sync`, `SNOW_CLIENT_SECRET=pac-sync-secret`.

## Module map

| Module | Responsibility |
| --- | --- |
| `snow_client.py` | Table API (OAuth/basic, paging, retries) and the offline `file` source |
| `normalizer.py` | Validation V1-V12, scope, validity dates, duplicate/conflict detection |
| `renderer.py` + `templates/proxy.pac.j2` | Deterministic PAC rendering + SHA-256 |
| `history.py` | Git repo layout `regions/<CODE>/{proxy.pac,rules.json,state.json}`, tags `<CODE>-v<N>` |
| `deployer.py` | ZIA `pac_files` stage/deploy/unstage, `activate` |
| `writeback.py` | ServiceNow `u_pac_rule` status updates |
| `main.py` | Orchestration, guardrails, lock, CLI |
| `simzia.py` | Simulated ZIA (`zscaler.target: simulated`): SDK-shaped client, state in `sim-zia/` |
| `envfile.py` | `.env` loading/writing, `ConfigError`, `require_env` (names the variable, never the value) |
| `doctor.py` | Read-only connectivity and configuration checks (`pac-sync doctor`) |
| `setup_wizard.py` | `pac-sync setup`: writes git-ignored `config/config.yaml` and `.env` |
| `start-poc.ps1` / `.cmd` | One-click POC launcher (prereqs, venv, setup, mock, doctor, first run) |

## Invariants (do not break)

- **Validation parity:** `normalizer.py` and `..\snow-mock\src\validation.php` implement the same rules. Change both together.
- **Contract with ServiceNow:** table and field names (`u_pac_rule`, `u_state`, `u_deploy_*`, ...) are defined in the
  "PAC Sync - ServiceNow Implementation Requirements" document and in `..\snow-mock\src\schema.php`. Renaming one breaks the integration.
- **No injection:** every ServiceNow value reaching the PAC goes through `validate_value` and is emitted as a JSON string literal (`renderer.js`).
- **Determinism:** same input -> byte-identical PAC (rules sorted by `PacRule.sort_key`). No timestamps in PAC content.
- **Git is the history of record:** ZIA keeps only 10 versions per PAC file.
- **Safety:** guardrails (removed-rules ratio, zero rules abort), one region failing never stops the others, run lock.
- **Secrets:** never in code, config or fixtures. Config holds env-var names only (`*_env` keys); values live in the
  environment or the git-ignored `.env`. Never log a value: report "set (N chars)". Mask credentials in git remote URLs.
- **History scope:** pac-sync stages, commits, diffs and discards `regions/` only; other folders in the history repo
  (e.g. `backups/` written by `snow-mock/backup.ps1` under the shared `.pac-sync.lock`) are never touched.
- **Live safety:** `deploy.live_max_mode` caps what a region may do on a live target; automatic bootstrap is refused on
  a capped live target (use `pac-sync bootstrap --region X --yes`).
- **ZIA calls are not verified against a live tenant** (see README "Verify before production"). Tests use a fake ZIA or the real SDK with mocked HTTP (`tests/test_deployer_sdk.py`).

## Conventions

- `from __future__ import annotations`, type hints, dataclasses for models; module docstring stating the module's role.
- Tests: pytest in `tests/`, no network; `tests/test_sync.py` patterns for end-to-end runs with `FakeDeployer`.
- Do not commit, push or deploy unless asked. Commits end with the attribution trailer configured for the session.
