# PAC Sync

Builds one PAC file per region from ServiceNow, keeps every version in git, and deploys it to Zscaler ZIA with
`zscaler-sdk-python`. Designed to run on a schedule (every 15 minutes is a good default).

```
ServiceNow (u_pac_region, u_pac_proxy_profile, u_pac_rule)
   │ Table API, read
   ▼
normalize + validate ──► rejected rules → u_deploy_status=failed in ServiceNow
   │ per region, sorted by priority
   ▼
render (templates/proxy.pac.j2) ──► git: regions/<CODE>/proxy.pac + rules.json, one commit per change
   │
   ▼
ZIA: clone as STAGE ─► activate ─► (auto mode) DEPLOY ─► activate ──► git: state.json + tag <CODE>-v<N>
   │
   ▼
write-back: u_pac_rule status, u_pac_deployment log row, webhook notification
```

The ServiceNow side (tables, workflow, validation, integration account) is specified in the
**PAC Sync – ServiceNow Implementation Requirements** document. The field names there are the contract with this code.

For local development without a ServiceNow instance, run the PHP mock in [`../snow-mock`](../snow-mock/README.md)
and use `config/config.mock.yaml` (Table API over OAuth, regions paused so ZIA is never called).

## Layout

| Path | Role |
| --- | --- |
| `pac_sync/snow_client.py` | Table API client (OAuth or basic, paging, retries) and the offline `file` source |
| `pac_sync/normalizer.py` | Validation (host, domain, CIDR, URL pattern, injection characters), scope, validity dates, duplicate/conflict detection |
| `pac_sync/renderer.py` | Deterministic Jinja2 rendering + SHA-256 |
| `pac_sync/history.py` | Git repository: content, rule snapshot, ZIA state, tags |
| `pac_sync/deployer.py` | ZIA `pac_files` stage / deploy / rollback, `activate` |
| `pac_sync/writeback.py` | Status updates for ServiceNow |
| `pac_sync/main.py` | Orchestration, guardrails, lock, CLI |
| `config/config.example.yaml` | Every setting, commented |
| `config/servicenow_sample_data.json` | Sample ServiceNow records covering every case (valid, expired, future, retire, invalid, conflict) |
| `templates/proxy.pac.j2` | PAC template |

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate      # Linux: . .venv/bin/activate
pip install -e ".[dev]"                                # Python >= 3.10 (required by zscaler-sdk-python)
cp config/config.example.yaml config/config.yaml
pytest
```

Environment variables (names configurable in `config.yaml`):

| Variable | Purpose |
| --- | --- |
| `SNOW_CLIENT_ID`, `SNOW_CLIENT_SECRET` | ServiceNow OAuth client credentials (or `SNOW_USERNAME` / `SNOW_PASSWORD` with `auth: basic`) |
| `ZSCALER_CLIENT_ID`, `ZSCALER_CLIENT_SECRET`, `ZSCALER_VANITY_DOMAIN` | Zscaler OneAPI credentials with ZIA PAC file + activation permissions |
| `PAC_SYNC_WEBHOOK_URL` | Optional Teams / Slack incoming webhook |
| `PAC_SYNC_CONFIG` | Optional path to the config file |

**Bootstrap, once per region:** create the PAC file in the ZIA admin portal with the exact name in
`u_pac_region.u_zia_pac_name` and deploy a first version. That keeps the hosted PAC URL stable; PAC Sync only adds
versions to it, so ZCC forwarding profiles and app profiles never need to change.

## Commands

```bash
pac-sync run --dry-run              # render + git diff --stat, then discard: no commit, no ZIA, no write-back
pac-sync run                        # the scheduled job
pac-sync render --region EMEA       # print a region's PAC to stdout
pac-sync promote --region NA        # deploy the STAGE version you reviewed (refused if the render changed since)
pac-sync rollback --region EMEA --version 41   # redeploy an older ZIA version and pause the region
pac-sync resume --region EMEA       # clear the pause once ServiceNow is fixed
```

Exit codes: `0` ok, `1` at least one region failed or was blocked, `2` aborted by a guardrail, `3` unexpected error.

## Deploy modes: staged first, automatic once stable

Set per region in `config.yaml`:

| Mode | What a run does |
| --- | --- |
| `stage` | Creates a ZIA STAGE version (replacing any older staged one). Production is untouched. Test, then `pac-sync promote --region X`. |
| `auto` | Stages, activates, deploys, activates in the same run. |
| `paused` | Renders and commits to git only. |

Suggested rollout:

1. All regions on `stage`. ServiceNow team loads the 20 sample rules in TEST; check each staged PAC in ZIA, then promote.
2. After two weeks of clean promotions in a region (no manual fix, no rollback), switch it to `auto`.
3. Keep the guardrail and webhook on in `auto`; a blocked run never deploys.

## History

The git repository at `history.repo_path` is the history of record, because ZIA keeps only the last 10 versions of a
PAC file (the SDK evicts the oldest on each clone).

```
regions/EMEA/proxy.pac     exact content sent to ZIA
regions/EMEA/rules.json    rules behind it (PACR number, action, value, profile, regions, priority)
regions/EMEA/state.json    pac_id, staged {version, hash, commit}, deployed {version, hash, commit}, last_error, paused_reason
```

- Each run that changes a PAC makes one commit, with the rules added and removed per region by PACR number.
- Each ZIA deployment adds a tag `<CODE>-v<version>`; rollbacks add `<CODE>-v<version>-rollback`.
- Useful queries: `git log -p regions/EMEA/proxy.pac`, `git log -S "portal.partner-example.com"` (when was a host added or removed), `git diff EMEA-v41 EMEA-v42`.
- Set `history.push: true` and add a remote (GitLab, GitHub, Azure DevOps) so the history survives the host.

## Safety

- **Validation:** every value is checked against its match type, and quotes, `;`, `\` and whitespace are refused, so a ServiceNow value can never inject JavaScript into the PAC. Rejected rules are reported back to ServiceNow.
- **Determinism:** rules are sorted by priority, action, match type, value, so unchanged input gives a byte-identical PAC and no redeploy.
- **Guardrails:** a run that would remove more than 20% of a region's rules (once it has 10 or more) blocks that region; 0 rules from ServiceNow while git has rules aborts the run.
- **Isolation:** one region failing never stops the others.
- **Lock:** a lock file in the history repo prevents overlapping runs.
- **ZIA validation:** `clone_pac_file` calls ZIA's `/pacFiles/validate` before creating any version.

## Scheduling

- **Linux cron:** `*/15 * * * * cd /opt/pac-sync && .venv/bin/pac-sync run >> /var/log/pac-sync.log 2>&1`
- **Windows Task Scheduler:** action `C:\pac-sync\.venv\Scripts\pac-sync.exe`, arguments `run`, start in `C:\pac-sync`, repeat every 15 minutes.
- **Kubernetes:** a `CronJob` with `concurrencyPolicy: Forbid`, the history repo cloned from its remote at start, `history.push: true`.

## Verify before production

`tests/test_deployer_sdk.py` runs `ZiaDeployer` through the real SDK (URL building, body serialization, response
models) with only the OAuth token and the HTTP send replaced. That proves what the code sends, not how ZIA answers.
Check these in a test tenant:

1. `update_pac_file(..., pac_version_action="DEPLOY")` and `"UNSTAGE"` accept the body `deployer._action` sends.
2. Whether ZIA allows more than one STAGE version per PAC file (the code unstages the old one first).
3. Whether `clone_pac_file` returns the new `pac_version` (there is a fallback that re-reads the versions).
4. Whether an `activate()` is needed between staging and deploying (the code does both).
