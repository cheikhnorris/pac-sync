# PAC Sync: high-level overview

## What it does

PAC Sync turns approved ServiceNow requests into Zscaler ZIA PAC files: one PAC file per region. Each change gets a
version in ZIA, and the result is reported back to the ServiceNow request.

Today, changing a PAC file is a manual edit in the ZIA portal. With PAC Sync:

- users ask for a PAC exception through a ServiceNow catalog item;
- managers and security approve it;
- PAC Sync applies it to the right region's PAC file, with a full history and a one-command rollback.

## Components

| Component | Role |
| --- | --- |
| **ServiceNow** (or the local mock `snow-mock`) | Catalog item "Request PAC exception", approval workflow, four tables: regions, proxy profiles, rules, deployments. Validation rules V1–V12 run on submit. |
| **pac-sync** (Python service) | Reads approved rules, validates them again, renders one PAC file per region, deploys it to ZIA, and writes the status back to ServiceNow. Runs on a schedule or on demand. |
| **Zscaler ZIA** (real tenant) | Hosted PAC files with up to 10 versions each: STAGE, DEPLOYED, LKG (last known good). |
| **History** | The ZIA PAC versions themselves, plus a git repository (private GitHub `pac-history`) when configured. |

```
 Requester ──► ServiceNow catalog item ──► approvals (manager, security, CAB for global rules)
                                                │ rule = approved
                                                ▼
                        pac-sync run  (schedule or manual)
     read rules ─► validate ─► render PAC per region ─► ZIA: new version (STAGE) ─► DEPLOY ─► activate
                                                │
                                                ▼
     ServiceNow: rule = deployed (with ZIA version), request closed, deployment log row
```

## How a change flows

1. **Request.** The requester picks an action: DIRECT or through a proxy profile. Then a match (host, domain, IP network
   or URL), the regions, and optionally an end date.
2. **Approval.** The owner group's manager approves first, then network security. Rules that apply to every region
   also need the CAB.
3. **Sync.** pac-sync renders every region and compares the result with what is live.
   - Unchanged regions are left alone.
   - Each changed region gets a new ZIA version. Its commit message names the run, the region and the rules added or
     removed, for example `PAC Sync 20261101T184500Z EMEA +PACR0001011 sha256:145415c0d490`.
4. **Deploy mode per region.**
   - `stage`: the version is saved for review; `pac-sync promote` makes it live.
   - `auto`: staged, deployed and activated in the same run.
   - `paused`: rendered, but ZIA is not touched.

   Regions start in `stage` and move to `auto` once they are stable.
5. **Write-back.** The rule is marked `deployed` with its ZIA version, and the request closes. A deployment row
   records the run. Rules that are retired or past their end date are removed from the PAC file and marked `retired`.

## Versioning and rollback

- **The version store is ZIA's own PAC file versioning.**
  - `pac-sync versions` lists the versions with their status and commit message.
  - `pac-sync diff` compares any two versions.
  - `pac-sync rollback` redeploys an older version or the LKG, then pauses the region until `pac-sync resume`.
- **10-version limit.** ZIA keeps 10 versions per PAC file. pac-sync chooses which version to delete itself and never
  deletes the live, staged or LKG version. The SDK on its own would delete the oldest version, even if it is the live
  one.
- **Optional git history** (`history.mode: git` or `both`) keeps every rendered file and a tag per deployed version.

## Safety controls

| Risk | Control |
| --- | --- |
| A bad or malicious rule reaches a PAC file | Validation in ServiceNow (V1–V12) **and again** in pac-sync. Quotes, scripts, public suffixes and malformed values are refused. The rule number format is checked because it is written into the PAC file as a comment. |
| An empty or truncated ServiceNow answer wipes a PAC file | A region is not deployed if a run would remove more than 20% of its rules (override with `--force`). |
| PAC Sync activates another admin's pending ZIA changes | Before the first write, PAC Sync reads the activation status. If changes are already pending, it saves its versions but never activates them. The API session is also left to expire instead of being closed, because closing it would activate the pending changes. |
| Someone edits the PAC file by hand in the portal | Drift detection pauses that region until someone reviews it (`resume` or `run --force`). |
| A demo touches production PAC files | The demo profile prefixes every file name (`demo-`), and a guard refuses any write to a file without that prefix before a request is sent. |
| One person pushes a rule through alone | Separation of duties in the approval workflow: nobody approves their own request or two steps of the same request, admins included. |
| ServiceNow is down during the write-back | The run stops writing at the first unreachable error, exits 1 and notifies; the next run re-sends the rule statuses. |
| Two runs overlap | A lock file per history folder. The mock's backup job uses the same lock. |
| Secrets leak | Credentials live only in a git-ignored `.env` or the environment. Logs show variable names and lengths, never values. |

## Status

| Area | State |
| --- | --- |
| ServiceNow mock: catalog, approvals, tables, Table API with OAuth | Done |
| pac-sync with the mock: all commands | Done |
| Full demo flow against the mock (bootstrap, run, promote, versions, diff, rollback, resume) | Rehearsed end to end on the demo profile |
| Real ZIA tenant | Built against the SDK source and checked call by call. **Not yet run against a tenant**: see below. |

**To confirm on the first real-tenant run** (`pac-sync doctor --write-probe`, then a demo run on `demo-` files):

- the exact admin role permissions needed;
- that ZIA deletes the version pac-sync names when a PAC file is at the 10-version limit;
- the commit message length limit (pac-sync cuts messages at 255 characters);
- that PAC content comes back from ZIA byte for byte;
- whether an API session that expires (instead of being closed) leaves pending changes unactivated.

## What comes next

- **Phase 2 (done):** write-back hardening, validation parity tests between ServiceNow and pac-sync, separation
  of duties, a Docker image for scheduled runs (README, Scheduling).
- **Phase 3:**
  - OS keyring for credentials;
  - a ZIA verification command;
  - production hardening.
- **Side project:** `zia-threat-shield`, an API with a Swagger UI for ZIA threat protection settings and threat
  playbooks. In progress.
