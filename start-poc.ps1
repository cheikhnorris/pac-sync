<#
.SYNOPSIS
  One-step PAC Sync proof of concept: checks prerequisites, prepares .venv, config and .env, starts the
  ServiceNow mock, runs doctor and a first sync, opens the portal.
.DESCRIPTION
  Usually started through start-poc.cmd (no ExecutionPolicy change needed). Each step prints one line and the
  script stops at the first failure with a remedy.

  Profile demo (the only one): the local ServiceNow mock against a REAL ZIA tenant, every PAC file named with a prefix
  (config\config.demo.yaml); `pac-sync setup --profile demo` asks for the ZIA credentials when config\config.yaml is
  missing, doctor must pass, regions whose PAC file does not exist and is not created by a run get a printed
  `bootstrap --region X --yes` command (never run for you).

  Config: -Config (default config\config.yaml). When that file is missing it is created from the demo profile.
.EXAMPLE
  .\start-poc.cmd
  .\start-poc.cmd -Config config\config.demo.local.yaml -NoBrowser
  .\start-poc.cmd -Reset        # fresh demo: mock database and the demo state file are deleted
  .\start-poc.cmd -Offline      # no internet for Python: install from the wheels\ folder (Python 3.11, 64-bit Windows)
#>
param(
    [string]$Config = "config\config.yaml",
    [ValidateSet("demo")][string]$Profile = "demo",
    [switch]$Reset,
    [switch]$NoBrowser,
    [switch]$SkipSync,
    [switch]$Offline,
    [string]$MockPath = (Join-Path $PSScriptRoot "..\snow-mock"),
    [string]$SdkPath = (Join-Path $PSScriptRoot "..\zscaler-sdk-python")
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$MockUrl = "http://127.0.0.1:8090"

function Step([string]$Message) { Write-Host "==> $Message" }
function Info([string]$Message) { Write-Host "    $Message" }
function Fail([string]$Message, [string]$Remedy) {
    Write-Host "FAILED: $Message" -ForegroundColor Red
    if ($Remedy) { Write-Host "  Fix: $Remedy" -ForegroundColor Yellow }
    exit 1
}
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # Native tools log to stderr: do not let PowerShell turn that into terminating errors.
    $ErrorActionPreference = "Continue"
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}
function Get-PythonVersion([string]$Exe, [string[]]$Prefix) {
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @($Prefix + @("-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])")) 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [int]("$out".Trim()) }
    } catch { }
    return 0
}
function Remove-DemoPath([string]$Path) {
    # Never touches the owner's pac-history clone, whatever the arguments say.
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ((Split-Path $full -Leaf) -eq "pac-history") { Fail "refusing to delete $full" "this is the GitHub-backed history" }
    Remove-Item -LiteralPath $full -Recurse -Force -Confirm:$false
    Info "deleted $full"
}

# 1. git -------------------------------------------------------------------------------------------------------
Step "Checking git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not found" "winget install Git.Git, then open a new terminal" }

# 2. Python 3.10 - 3.12 and the virtual environment ----------------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $v = Get-PythonVersion $venvPy @()
    if ($v -lt 310 -or $v -gt 312) { Fail ".venv uses Python $v (need 3.10 - 3.12)" "delete the .venv folder and run start-poc again" }
    Step "Python virtual environment .venv (Python $([int]($v / 100)).$($v % 100))"
} else {
    Step "Creating .venv"
    $candidates = @(
        @{ Exe = "py"; Prefix = @("-3.11") }, @{ Exe = "py"; Prefix = @("-3.12") }, @{ Exe = "py"; Prefix = @("-3.10") },
        @{ Exe = "python"; Prefix = @() }, @{ Exe = "python3"; Prefix = @() }
    )
    $found = $null
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $v = Get-PythonVersion $c.Exe $c.Prefix
        if ($v -ge 310 -and $v -le 312) { $found = $c; break }
    }
    if (-not $found) { Fail "no Python 3.10 - 3.12 found" "winget install Python.Python.3.11, then open a new terminal" }
    $rc = Invoke-Native $found.Exe ($found.Prefix + @("-m", "venv", ".venv"))
    if ($rc -ne 0 -or -not (Test-Path $venvPy)) { Fail "python -m venv failed" "check the Python installation" }
}

# 3. Dependencies (skipped when pyproject.toml and the SDK source did not change) -------------------------------
$useLocalSdk = Test-Path (Join-Path $SdkPath "pyproject.toml")
$wheels = Join-Path $PSScriptRoot "wheels"
if ($Offline -and -not (Test-Path $wheels)) { Fail "-Offline needs the wheels folder ($wheels)" "copy it from the pac-sync repo" }
$sdkSource = if ($Offline) { "offline:wheels" } elseif ($useLocalSdk) { "editable:" + (Resolve-Path $SdkPath).Path } else { "pypi:zscaler-sdk-python>=1.11,<2" }
$stampFile = Join-Path $PSScriptRoot ".venv\.install-stamp"
$stamp = (Get-FileHash (Join-Path $PSScriptRoot "pyproject.toml") -Algorithm SHA256).Hash + "|" + $sdkSource
if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $stamp)) {
    Step "Dependencies up to date"
} else {
    Step "Installing dependencies ($sdkSource)"
    if ($Offline) {
        # --no-index: never reach PyPI; the SDK, its dependencies and the build tools all come from the wheels folder
        $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "--no-index", "--find-links", $wheels, "-e", ".[dev]")
        if ($rc -ne 0) { Fail "offline install from the wheels folder failed" "the wheels are built for Python 3.11 64-bit: delete .venv, install Python 3.11, run start-poc -Offline again" }
    } else {
        $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "--upgrade", "pip")
        if ($useLocalSdk) {
            $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "-e", (Resolve-Path $SdkPath).Path)
        } else {
            $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "zscaler-sdk-python>=1.11,<2")
        }
        if ($rc -ne 0) { Fail "installing zscaler-sdk-python failed" "check the network / proxy, then run start-poc again (or start-poc -Offline when Python cannot reach the internet)" }
        $rc = Invoke-Native $venvPy @("-m", "pip", "install", "-q", "-e", ".[dev]")
        if ($rc -ne 0) { Fail "installing pac-sync failed" "see the pip output above" }
    }
    Set-Content -Path $stampFile -Value $stamp -Encoding ascii
}

# 4. Config and credentials ------------------------------------------------------------------------------------
if (-not (Test-Path $Config)) {
    if ($PSBoundParameters.ContainsKey("Config")) { Fail "config $Config not found" "omit -Config to create config\config.yaml from the $Profile profile" }
    Step "Creating $Config and .env from the demo profile (answer the questions; secrets are typed hidden)"
    $rc = Invoke-Native $venvPy @("-m", "pac_sync", "--config", $Config, "setup", "--profile", "demo", "--no-doctor")
    if ($rc -ne 0) { Fail "pac-sync setup failed" "see the message above" }
} else {
    Step "Using config $Config"
}
$ErrorActionPreference = "Continue"
$settings = & $venvPy -c "import sys, yaml; c = yaml.safe_load(open(sys.argv[1], encoding='utf-8')); h = c['history']; z = c.get('zscaler') or {}; print(c['servicenow'].get('instance_url', '')); print(h.get('repo_path', '')); print(bool(h.get('push'))); print(h.get('mode') or 'git'); print(z.get('target') or 'live'); print(h.get('state_file') or './pac-state.json'); print(z.get('pac_name_prefix') or '')" $Config 2>$null
$ErrorActionPreference = "Stop"
if (-not $settings -or $settings.Count -lt 7) { Fail "cannot read $Config" "run .venv\Scripts\pac-sync --config $Config doctor for details" }
$instanceUrl, $repoPath, $push, $histMode, $zTarget, $stateFile, $prefix = $settings[0], $settings[1], $settings[2], $settings[3], $settings[4], $settings[5], $settings[6]
$usesMock = $instanceUrl -match '^https?://(127\.0\.0\.1|localhost)(:|/|$)'
Info "ServiceNow $instanceUrl, ZIA $zTarget, history $histMode ($repoPath), push $push"
if ($Profile -eq "demo" -and ($zTarget -ne "live" -or -not $prefix)) { Fail "$Config is not a demo config (needs zscaler.target: live and a zscaler.pac_name_prefix)" "create one: .venv\Scripts\pac-sync --config config\config.demo.local.yaml setup --profile demo, then start-poc -Config config\config.demo.local.yaml" }
if ($usesMock -and -not (Test-Path ".env") -and -not $env:SNOW_CLIENT_ID) {
    Info "no .env: using the mock's public demo OAuth client for this session"
    $env:SNOW_CLIENT_ID = "pac-sync"
    $env:SNOW_CLIENT_SECRET = "pac-sync-secret"
}

# 5. Reset (optional) ------------------------------------------------------------------------------------------
if ($Reset) {
    Step "Resetting demo data"
    # ALLOWLIST: only things this script knows to be disposable demo data are deleted, and only after every one of them
    # has been checked. Everything else is refused with a reason. pac-history is never in the list.
    $root = (Resolve-Path $PSScriptRoot).Path
    function Resolve-InProject([string]$Path) {
        $full = [System.IO.Path]::GetFullPath((Join-Path $root $Path))
        if (-not $full.StartsWith($root + "\", [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
        return $full
    }
    function Refuse([string]$Why) {
        Fail "-Reset refused: $Why" "nothing was deleted. Reset only works for a demo config (history.mode: zia, state file pac-state*.json)"
    }
    $planned = @()
    if ($histMode -eq "zia") {
        $full = Resolve-InProject $stateFile
        if (-not $full -or (Split-Path $full -Leaf) -notmatch '^pac-state[A-Za-z0-9._-]*\.json$') {
            Refuse "history.state_file '$stateFile' is not a pac-state*.json file inside the project"
        }
        $planned += $full
    } else {
        Refuse "$Config does not use history.mode: zia: state.json would keep PAC ids this reset cannot delete"
    }
    $stop = Join-Path $MockPath "stop.ps1"
    if (Test-Path $stop) { & $stop }
    Get-ChildItem -Path (Join-Path $MockPath "data") -Filter "snow.sqlite*" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-DemoPath $_.FullName }
    foreach ($full in $planned) { Remove-DemoPath $full }
    Write-Warning "The ZIA tenant keeps its PAC files ($prefix*). Remove them with: .venv\Scripts\pac-sync --config $Config demo-cleanup --yes"
}

# 6. ServiceNow mock -------------------------------------------------------------------------------------------
if ($usesMock) {
    Step "Starting the ServiceNow mock ($MockUrl)"
    $findPhp = Join-Path $MockPath "tools\find-php.ps1"
    if (-not (Test-Path $findPhp)) { Fail "snow-mock not found at $MockPath" "pass -MockPath <folder of snow-mock>" }
    . $findPhp
    try { $null = Find-Php } catch {
        $answer = ""
        try { $answer = Read-Host "PHP 8.4 is needed for the mock. Install it now with winget? [Y/n]" } catch { }
        if ($answer -notmatch '^(|y|yes)$') { Fail "PHP not found" "winget install PHP.PHP.8.4" }
        $rc = Invoke-Native "winget" @("install", "--id", "PHP.PHP.8.4", "-e", "--accept-source-agreements", "--accept-package-agreements")
        try { $null = Find-Php } catch { Fail "PHP still not found after winget" "winget install PHP.PHP.8.4, then open a new terminal" }
    }
    $global:LASTEXITCODE = 0
    try { & (Join-Path $MockPath "start.ps1") -WaitReady } catch { Fail "the mock did not start: $_" "see $MockPath\logs\server.log" }
    if ($LASTEXITCODE -ne 0) { Fail "the mock did not answer" "see $MockPath\logs\server.log" }
} else {
    Step "ServiceNow at $instanceUrl (mock not started)"
}

# 7. Doctor, then the first run ----------------------------------------------------------------------------------
function Invoke-Doctor {
    $ErrorActionPreference = "Continue"
    $out = & $venvPy -m pac_sync --config $Config doctor 2>&1 | ForEach-Object { "$_" }
    $script:doctorRc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    $out | Out-Host
    return ,@($out)
}
function Get-MissingRegions($lines) {
    # Regions whose PAC file is missing and is not created by a run (stage mode): doctor names the command.
    return @($lines | Select-String -Pattern 'pac-sync bootstrap --region (\w+)' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique)
}

Step "pac-sync doctor"
$doctorOut = Invoke-Doctor
if ($doctorRc -ne 0) {
    $missing = Get-MissingRegions $doctorOut
    if ($missing.Count -gt 0) {
        Write-Host ""
        Write-Host "These regions have no PAC file in ZIA yet and a run does not create them. Create them explicitly:" -ForegroundColor Yellow
        foreach ($r in $missing) { Write-Host "  .venv\Scripts\pac-sync --config $Config bootstrap --region $r --yes" }
    }
    Fail "doctor reported failures" "fix the [FAIL] lines above, then run start-poc again"
}

if ($SkipSync) {
    Step "Skipping the sync (-SkipSync)"
} else {
    Step "pac-sync run"
    $rc = Invoke-Native $venvPy @("-m", "pac_sync", "--config", $Config, "run")
    if ($rc -eq 1) {
        Write-Warning "the run finished with a failed, blocked or not-activated region (exit 1): see the log above"
    } elseif ($rc -ne 0) {
        Fail "pac-sync run failed (exit $rc)" "see the log above; .venv\Scripts\pac-sync --config $Config doctor"
    }
}

# 8. Browser and next steps ----------------------------------------------------------------------------------------
if ($usesMock -and -not $NoBrowser) {
    Step "Opening $MockUrl"
    Start-Process $MockUrl
}
$cfgArg = if ($Config -eq "config\config.yaml" -or $Config -eq "config/config.yaml") { "" } else { " --config $Config" }
Write-Host ""
Write-Host "Ready. Next steps:" -ForegroundColor Green
Write-Host "  1. In the portal, act as jdoe (top right) and submit a 'Request PAC exception' for region EMEA."
Write-Host "  2. Approve it as mmanager, then nsec (and cab for a global rule)."
Write-Host "  3. .venv\Scripts\pac-sync$cfgArg run      -> the request closes, the rule shows Deployed."
Write-Host "  4. ZIA admin portal > Hosted PAC Files (menu name may differ): open ${prefix}emea-proxy.pac > Versions."
Write-Host "     Or from here: .venv\Scripts\pac-sync$cfgArg versions --region EMEA  |  diff --region EMEA --from deployed --to render"
Write-Host "  5. Promote NA: .venv\Scripts\pac-sync$cfgArg promote --region NA;  roll back: rollback --region EMEA --to lkg"
Write-Host "  6. Mock portal /pacsync shows the latest ZIA versions per region. After the demo: demo-cleanup --yes"
Write-Host "  .venv\Scripts\pac-sync$cfgArg status | doctor | render --region EMEA"
exit 0
