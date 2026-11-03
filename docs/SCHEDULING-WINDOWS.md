# Run PAC Sync every 5 minutes on a Windows demo laptop

Two Windows Task Scheduler tasks:

| Task | When | What |
| --- | --- | --- |
| `PAC Sync - ServiceNow mock` | at logon | starts the local ServiceNow mock (`snow-mock\start.ps1`) on http://127.0.0.1:8090 |
| `PAC Sync - run` | every 5 minutes | `pac-sync run`: reads the mock, renders the PAC files, deploys to ZIA, writes the status back |

Before you start, the manual run must already work from the `pac-sync` folder: `.venv\Scripts\pac-sync doctor` passes and
`.venv\Scripts\pac-sync run` succeeds. The scheduled run uses the same `config\config.yaml` and `.env`, so it also gets
the ZIA credentials and the proxy (`HTTPS_PROXY`).

## 1. Create the tasks

Run this once in PowerShell. You don't need an administrator prompt. Set `$root` to the folder that holds `pac-sync`
and `snow-mock`.

```powershell
$root = "C:\pac-demo"
New-Item -ItemType Directory -Force "$root\logs" | Out-Null

# ServiceNow mock at logon (start.ps1 does nothing if the mock is already running)
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\snow-mock\start.ps1`" -WaitReady"
Register-ScheduledTask "PAC Sync - ServiceNow mock" -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

# pac-sync run every 5 minutes, output appended to a log outside the repo
$a = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c .venv\Scripts\pac-sync.exe run >> `"$root\logs\pac-sync.log`" 2>&1" -WorkingDirectory "$root\pac-sync"
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -StartWhenAvailable
Register-ScheduledTask "PAC Sync - run" -Action $a -Trigger $t -Settings $s
```

- **No overlapping runs:** `-MultipleInstances IgnoreNew` skips a run while the previous one is still going. PAC Sync's
  own run lock prevents overlap too.
- **Logged-on user only:** the tasks run only while you are logged on, so no password is stored. A console window
  flashes briefly every 5 minutes. To run hidden, edit the task in Task Scheduler and choose "Run whether user is
  logged on or not"; Windows then asks for your password.

## 2. Start and check

```powershell
Start-ScheduledTask "PAC Sync - ServiceNow mock"
Start-ScheduledTask "PAC Sync - run"
Get-Content C:\pac-demo\logs\pac-sync.log -Tail 20
Get-ScheduledTaskInfo "PAC Sync - run" | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

| `LastTaskResult` | Meaning |
| --- | --- |
| `0` | clean run (changes deployed, or nothing to do) |
| `1` | look at the log: a region failed, write-back failed, or changes were saved but **not activated** because ZIA had other pending changes (`deploy.skip_activate_if_pending`) |
| `3` | configuration error (for example a missing env variable); run `.venv\Scripts\pac-sync doctor` |
| `267009` | the task is running right now |

Then submit and approve a rule in the mock portal (http://127.0.0.1:8090). Within 5 minutes the log shows the
deployment and the rule shows Deployed.

## 3. Pause, resume, remove

```powershell
Disable-ScheduledTask "PAC Sync - run"     # pause
Enable-ScheduledTask  "PAC Sync - run"     # resume
Unregister-ScheduledTask "PAC Sync - run" -Confirm:$false
Unregister-ScheduledTask "PAC Sync - ServiceNow mock" -Confirm:$false
.\..\snow-mock\stop.ps1                    # stop the mock (from the pac-sync folder)
```

## Notes

- Each run that deploys also **activates** the tenant, unless `deploy.skip_activate_if_pending` holds it back
  because another admin has pending changes.
- The log grows with every run. Delete `C:\pac-demo\logs\pac-sync.log` from time to time.
- For production, use a service account with "Run whether user is logged on or not", a real ServiceNow instance
  instead of the mock, and the 15-minute interval from the README "Scheduling" section.
