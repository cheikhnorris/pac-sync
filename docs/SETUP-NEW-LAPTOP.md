# Set up PAC Sync on a new laptop

About 15 minutes. Windows 10 or 11, PowerShell, internet access for the installs. No admin rights are needed apart
from the installers.

The demo runs the ServiceNow mock against a real ZIA tenant, so prepare the ZIA side (step 3) before starting.

---

### 1. Install the tools (one time)

Open **PowerShell** and run:

```powershell
winget install Git.Git
winget install GitHub.cli
winget install Python.Python.3.11
winget install PHP.PHP.8.4
```

Close PowerShell and open a new one so the new commands are found. Then check them:

```powershell
git --version; gh --version; py -3.11 --version; php --version
```

`php` may not be on the PATH yet; that's fine. The scripts find PHP's winget folder themselves.

### 2. Get the code

Both repositories are private, so sign in to GitHub first (a browser window opens):

```powershell
gh auth login --web
mkdir C:\pac-demo; cd C:\pac-demo
gh repo clone cheikhnorris/pac-sync
gh repo clone cheikhnorris/snow-mock
```

Keep the two folders side by side (`C:\pac-demo\pac-sync` and `C:\pac-demo\snow-mock`), because the launcher looks
for `..\snow-mock`. Any parent folder works.

The demo writes **only** to PAC files whose name starts with `demo-`, so production PAC files are never touched.
It never activates other admins' pending changes, and it pushes nothing to git.

### 3. Prepare in ZIA (with the ZIA admin)

- **API client.** In ZIdentity, create an API client (OneAPI is recommended) with a role that can manage **Hosted PAC
  Files** and **activate** changes, and nothing else. Note four values:
  - the client ID;
  - the client secret, or a private key;
  - the vanity domain: the `<name>` in `<name>.zslogin.net`;
  - the cloud, if it isn't production.
- **PAC domain.** The domain ZIA asks for when it creates a PAC file.
- **Prefix.** Check that no existing PAC file or Client Connector forwarding profile uses `demo-`. If one does, pick
  another prefix.
- **Pending changes.** The ZIA portal must show no pending changes before the demo. If it does, PAC Sync still saves
  its versions but won't activate them.

### 3b. Behind a corporate proxy (optional)

Add to `.env` (placeholders; percent-encode special characters in the user and password):

```
HTTPS_PROXY=http://user:password@proxy.example.com:8080
NO_PROXY=localhost,127.0.0.1
REQUESTS_CA_BUNDLE=C:\certs\bundle.pem
```

`REQUESTS_CA_BUNDLE` is only needed when the proxy inspects TLS. Find the proxy with `netsh winhttp show proxy`, with
`ProxyServer` / `AutoConfigURL` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`, or ask IT.
`start-poc` installs packages with pip, which reads the terminal's environment, not `.env`: in PowerShell run
`$env:HTTPS_PROXY = "http://user:password@proxy.example.com:8080"` first (or use `.\start-poc.cmd -Offline`).
A PAC/WPAD setup is not auto-discovered by Python. `pac-sync doctor` prints the proxy line (credentials masked).

### 4. Start everything

```powershell
cd C:\pac-demo\pac-sync
.\start-poc.cmd
```

The first start takes 2–3 minutes. In order, it:

1. checks Python and PHP;
2. creates `.venv` and installs pac-sync and the Zscaler SDK (from PyPI);
3. writes `config\config.yaml` and `.env` from the demo profile (it asks for the ZIA values from step 3 and an optional HTTPS proxy, typed hidden);
4. starts the ServiceNow mock at http://127.0.0.1:8090;
5. runs `pac-sync doctor` and a first `pac-sync run`;
6. opens the portal.

Every line should be OK; the script stops and says what to fix if one isn't.

### 5. Run the demo

1. Portal, **Acting as** menu (top right): **jdoe**. Open **Request PAC exception**, then create a new rule for region
   EMEA, for example DIRECT for `partner.example`.
2. Approve it as **mmanager**, then as **nsec**.
3. Back in PowerShell:
   ```powershell
   .venv\Scripts\pac-sync run
   ```
4. The request closes and the rule shows `deployed` with its ZIA version. The **PAC Sync** menu shows the versions per
   region.
5. Try the version commands:
   ```powershell
   .venv\Scripts\pac-sync versions --region EMEA
   .venv\Scripts\pac-sync diff --region EMEA --from 1 --to deployed
   .venv\Scripts\pac-sync promote --region NA        # NA is a review-first (stage) region
   .venv\Scripts\pac-sync rollback --region EMEA --to lkg
   .venv\Scripts\pac-sync resume --region EMEA
   ```

**Start over:** `.\start-poc.cmd -Reset` deletes only the demo data (mock database and the demo state file).
**Stop the mock:** `powershell -ExecutionPolicy Bypass -File ..\snow-mock\stop.ps1`.

### 6. Use a separate config file (optional)

```powershell
cd C:\pac-demo\pac-sync
.venv\Scripts\pac-sync --config config\config.demo.local.yaml setup --profile demo
```

Answer the questions. Secrets are typed hidden and saved only in `.env`, which git ignores. For ServiceNow, keep the
defaults: `api`, `http://127.0.0.1:8090`, `oauth`, client ID `pac-sync`, secret `pac-sync-secret`.

### 7. Check, then run with that file

```powershell
.\start-poc.cmd -Config config\config.demo.local.yaml     # starts the mock, runs doctor
.venv\Scripts\pac-sync --config config\config.demo.local.yaml doctor --write-probe --region EMEA
.venv\Scripts\pac-sync --config config\config.demo.local.yaml bootstrap --region NA --yes
.venv\Scripts\pac-sync --config config\config.demo.local.yaml run
```

- `doctor` checks credentials, permissions, PAC names and the activation status, without writing anything.
- `doctor --write-probe` makes one small real change on `demo-emea-proxy.pac` (stage, then unstage) to prove the write
  permissions. It refuses to run while ZIA has pending changes.
- `bootstrap` creates the NA PAC file. EMEA and APAC are created by the first run.

Run the demo from step 5, adding `--config config\config.demo.local.yaml` to each command. In the ZIA portal, open
**Hosted PAC Files**, then `demo-emea-proxy.pac`, then its versions: each version's message names the run and the
rules it changed.

### 8. After the demo

```powershell
.venv\Scripts\pac-sync --config config\config.demo.local.yaml demo-cleanup --yes   # deletes only demo- PAC files
del pac-state.json
```

Then rotate or delete the API client secret in ZIdentity, and clear the ZIA values from `.env`.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no Python 3.10 - 3.12 found` | `winget install Python.Python.3.11`, then open a new PowerShell. |
| `PHP not found` | `winget install PHP.PHP.8.4`, then open a new PowerShell. |
| Port 8090 in use | Stop the other program, or run `..\snow-mock\stop.ps1` for an old mock. |
| `gh repo clone` asks for access | The repositories are private: the GitHub account needs access (ask the owner). |
| Run exits 1 with "not activated" | ZIA had pending changes. Review and activate them in the portal; the next run confirms. |
| A region says "paused" | Someone changed the PAC file in the portal, or there was a rollback. Review it, then `pac-sync resume --region X`. |
| Doctor FAIL on a ZIA permission | The API client's role is missing Hosted PAC Files or activation rights. |
| `WinError 10013` or `ProxyError` | Set `HTTPS_PROXY` in `.env` (step 3b). If `python.exe` itself is blocked, ask IT. |
| `407 Proxy Authentication Required` | Wrong proxy credentials, or an NTLM-only proxy (use a local relay such as px or cntlm). |
| `CERTIFICATE_VERIFY_FAILED` | Set `REQUESTS_CA_BUNDLE` to a PEM with the public roots plus the corporate root. |
| Go back to a direct connection | Delete the `HTTPS_PROXY` line from `.env` (the wizard cannot remove it). |

More detail is in the repository README: commands, configuration, safety, versioning.
