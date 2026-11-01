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
  Env for the mock: `SNOW_CLIENT_ID=pac-sync`, `SNOW_CLIENT_SECRET=pac-sync-secret` (in `.env`, found next to the config's folder).
- `config\config.demo.yaml` = mock ServiceNow + a REAL ZIA tenant with prefixed PAC files (`demo-`), history mode `zia`. Never run it
  from an agent session without credentials and an explicit go; test it with a scratch copy that has `target: simulated`.
- The owner's git-ignored `config\config.yaml` has its own simulated state dir (`sim-zia-owner/`); `-Reset` of `start-poc` refuses it.

## Module map

| Module | Responsibility |
| --- | --- |
| `snow_client.py` | Table API (OAuth/basic, paging, retries) and the offline `file` source |
| `normalizer.py` | Validation V1-V12, scope, validity dates, duplicate/conflict detection |
| `renderer.py` + `templates/proxy.pac.j2` | Deterministic PAC rendering + SHA-256 |
| `history.py` | Git repo layout `regions/<CODE>/{proxy.pac,rules.json,state.json}`, tags `<CODE>-v<N>`; `zia-archive/` in mode `both` |
| `zia_history.py` | `history.mode: zia`: same interface as `GitHistory`, state derived from the ZIA versions + git-ignored `state_file` |
| `zia_messages.py` | ZIA version commit message `PAC Sync <run-id> <CODE> +/-PACR sha256 git` (build, parse, 255 limit) |
| `zia_cli.py` | `versions`, `diff`, `lkg`, `demo-cleanup` |
| `deployer.py` | ZIA client factory (simulated / OneAPI / legacy), prefix guard, `pac_files` stage (explicit `delete_version`) / deploy / LKG / bootstrap / delete, version cache, `activate` |
| `writeback.py` | ServiceNow `u_pac_rule` status updates |
| `main.py` | Orchestration, guardrails, drift check, activation guard, lock, CLI |
| `simzia.py` | Simulated ZIA (`zscaler.target: simulated`): SDK-shaped client, state in `sim-zia/` |
| `envfile.py` | `.env` loading/writing, `ConfigError`, `require_env` (names the variable, never the value) |
| `doctor.py` | Read-only connectivity and configuration checks (`pac-sync doctor`) |
| `setup_wizard.py` | `pac-sync setup`: writes git-ignored `config/config.yaml` and `.env` |
| `start-poc.ps1` / `.cmd` | One-click POC launcher (prereqs, venv, setup, mock, doctor, first run) |

## Invariants (do not break)

- **Validation parity:** `normalizer.py` and `..\snow-mock\src\validation.php` implement the same rules. Change both together; `tests/test_validation_parity.py` runs both on the same cases.
- **Contract with ServiceNow:** table and field names (`u_pac_rule`, `u_state`, `u_deploy_*`, ...) are defined in the
  "PAC Sync - ServiceNow Implementation Requirements" document and in `..\snow-mock\src\schema.php`. Renaming one breaks the integration.
- **No injection:** every ServiceNow value reaching the PAC goes through `validate_value` and is emitted as a JSON string literal (`renderer.js`).
- **Determinism:** same input -> byte-identical PAC (rules sorted by `PacRule.sort_key`). No timestamps in PAC content.
- **History of record:** git (modes `git` and `both`) because ZIA keeps only 10 versions per PAC file; `both` also archives every
  version ZIA is about to delete (`zia-archive/`). With `history.mode: zia` the ZIA versions themselves are the history (their commit
  messages, `zia_messages.py`) and git is not used; only pauses and errors live in the git-ignored `history.state_file`.
- **Template comment contract:** every rule line of `templates/proxy.pac.j2` ends with `// <number> <ACTION>`; `zia_history.parse_rules`
  reads it back for the removed-rules guardrail in mode `zia` (tested in `tests/test_renderer.py`). Change both together.
- **Prefix guard:** with `zscaler.only_prefixed: true` every ZIA write (stage, deploy, unstage, LKG, bootstrap, rollback, delete,
  probe) on a PAC file whose name lacks `zscaler.pac_name_prefix` raises `ConfigError` before any request (`ZiaDeployer.assert_writable`).
  New write paths must call it. Effective name = prefix + `u_zia_pac_name`.
- **Activation guard:** with `deploy.skip_activate_if_pending`, `activate.status()` is read before the first write; if not ACTIVE nothing
  is activated (other admins' pending changes would go live too), the run exits 1 and says "not activated" everywhere.
- **Drift:** a DEPLOYED version whose message is not PAC Sync's (or, in git modes, is not the recorded one) pauses the region
  ("manual change in ZIA") until `resume` or `run --force`.
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
